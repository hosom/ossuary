"""Aggregates, the agents wired to a stub model, and the report."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from ossuary.aggregate import compute_tool_stats, render_tool_stats
from ossuary.cache import Cache
from ossuary.config import load_config
from ossuary.models import RunManifest, Session, SessionScan, StoredIssue
from ossuary.pipeline import issue_id_for, unclustered_issues
from ossuary.report import build_context, render_html

CONFIG = load_config(Path(__file__).parent.parent / "agents.yaml")


class TestAggregates:
    def test_counts_calls_per_tool(self, claude_session: Session):
        stats = {s.tool_name: s for s in compute_tool_stats([claude_session])}
        assert "Bash" in stats
        assert stats["Bash"].call_count >= 3

    def test_byte_identical_repeats_are_counted_as_duplicates(self, claude_session: Session):
        stats = {s.tool_name: s for s in compute_tool_stats([claude_session])}
        assert stats["Bash"].duplicate_result_count >= 1
        assert stats["Bash"].duplicate_rate > 0

    def test_round_byte_counts_are_counted(self, claude_session: Session):
        stats = {s.tool_name: s for s in compute_tool_stats([claude_session])}
        assert stats["Bash"].round_number_count >= 1

    def test_orphan_results_get_their_own_bucket(self, claude_session: Session):
        """Never attributed to a neighbouring tool -- that would corrupt the stats."""
        names = {s.tool_name for s in compute_tool_stats([claude_session])}
        assert "<unknown>" in names

    def test_session_count_spans_the_corpus(self, claude_session: Session):
        other = claude_session.model_copy(deep=True)
        other.session_id = "sess-two"
        stats = {s.tool_name: s for s in compute_tool_stats([claude_session, other])}
        assert stats["Bash"].session_count == 2

    def test_rendering_is_readable_and_complete(self, claude_session: Session):
        text = render_tool_stats(compute_tool_stats([claude_session]))
        assert "Bash" in text and "calls=" in text and "bytes:" in text

    def test_empty_corpus_renders_without_crashing(self):
        assert "No tool results" in render_tool_stats([])


class TestScannerAgent:
    """Agent A through the backend seam, driven by a scripted model.

    The Pydantic AI backend is the one that can be exercised without a
    credential, so it stands in for all three. What is being tested here is the
    tool wiring and the deps that every backend shares, not Pydantic AI itself.
    """

    def _backend(self, script, **overrides):
        from ossuary.backends import BackendConfig
        from ossuary.backends.pydantic_ai_backend import PydanticAIBackend

        config = BackendConfig(
            model="stub",
            max_turns=overrides.pop("max_turns", CONFIG.scanner.max_turns),
            extra={"model_object": FunctionModel(script)},
            **overrides,
        )
        return PydanticAIBackend(config)

    def _run(self, store, session, script, **overrides):
        from ossuary.agents.deps import ScannerDeps
        from ossuary.agents.scanner import scanner_prompt
        from ossuary.agents.tools import scanner_tools

        deps = ScannerDeps(
            store=store,
            session_id=session.session_id,
            session_content_hash=session.content_hash,
            tool_stats=compute_tool_stats([session]),
        )
        result = self._backend(script, **overrides).run(
            instructions=CONFIG.scanner.prompt,
            prompt=scanner_prompt(store.outline(session.session_id)),
            tools=scanner_tools(deps),
        )
        return deps, result

    def test_tools_are_callable_and_issues_are_collected(self, loaded_store, claude_session):
        def script(messages, info):
            n = len([m for m in messages if m.kind == "response"])
            if n == 0:
                return ModelResponse(parts=[ToolCallPart("read_events", {"start": 0, "end": 4})])
            if n == 1:
                return ModelResponse(parts=[ToolCallPart("search_session", {"pattern": "commit"})])
            if n == 2:
                return ModelResponse(parts=[ToolCallPart("tool_stats", {"tool_name": "Bash"})])
            if n == 3:
                return ModelResponse(parts=[ToolCallPart("report_issue", {
                    "title": "Bash output capped at 30000 bytes",
                    "description": "Cut mid-line with exit code 0 and no indication.",
                    "severity": "high", "phase": "harness",
                    "evidence_event_indices": [5, 5, 4], "confidence": 0.9})])
            return ModelResponse(parts=[TextPart("done")])

        deps, result = self._run(loaded_store, claude_session, script)
        assert len(deps.collected) == 1
        issue = deps.collected[0]
        assert issue.severity == "high" and issue.phase == "harness"
        assert issue.evidence_event_indices == [4, 5], "deduped and sorted"
        assert result.text == "done"
        assert not result.hit_turn_cap

    def test_all_five_tools_are_registered(self, loaded_store, claude_session):
        seen = []

        def script(messages, info):
            if not seen:
                seen.extend(t.name for t in info.function_tools)
            return ModelResponse(parts=[TextPart("done")])

        self._run(loaded_store, claude_session, script)
        assert set(seen) == {
            "read_events", "search_session", "read_event_slice", "tool_stats", "report_issue",
        }

    def test_confidence_is_clamped(self, loaded_store, claude_session):
        def script(messages, info):
            n = len([m for m in messages if m.kind == "response"])
            if n == 0:
                return ModelResponse(parts=[ToolCallPart("report_issue", {
                    "title": "t", "description": "d", "severity": "low", "phase": "tool",
                    "evidence_event_indices": [], "confidence": 4.2})])
            return ModelResponse(parts=[TextPart("done")])

        deps, _ = self._run(loaded_store, claude_session, script)
        assert deps.collected[0].confidence == 1.0

    def test_read_events_span_is_capped_with_a_marker(self, loaded_store, claude_session):
        """An oversized span must say what it withheld, never silently shorten."""
        captured = {}

        def script(messages, info):
            n = len([m for m in messages if m.kind == "response"])
            if n == 0:
                return ModelResponse(parts=[ToolCallPart("read_events", {"start": 0, "end": 500})])
            captured["reply"] = messages[-1].parts[0].content
            return ModelResponse(parts=[TextPart("done")])

        self._run(loaded_store, claude_session, script)
        assert "ossuary:elided" in captured["reply"]
        assert "read_events again" in captured["reply"]

    def test_a_failing_tool_returns_a_marked_result_instead_of_raising(
        self, loaded_store, claude_session
    ):
        """A malformed tool call is a tool result, not a dead scan.

        Backends disagree about what a raised exception means -- retry, abort,
        swallow -- and that disagreement would otherwise show up as different
        findings from the same transcript.
        """
        captured = {}

        def script(messages, info):
            n = len([m for m in messages if m.kind == "response"])
            if n == 0:
                return ModelResponse(parts=[ToolCallPart("tool_stats", {})])
            captured["reply"] = messages[-1].parts[0].content
            return ModelResponse(parts=[TextPart("done")])

        _, result = self._run(loaded_store, claude_session, script)
        assert "ossuary:tool-error" in captured["reply"]
        assert "tool_stats" in captured["reply"]
        assert result.text == "done", "the run continues past a tool failure"

    def test_partial_results_survive_a_turn_cap(self, loaded_store, claude_session):
        """Incremental reporting is what makes a cutoff yield partial results."""
        def script(messages, info):
            n = len([m for m in messages if m.kind == "response"])
            return ModelResponse(parts=[ToolCallPart("report_issue", {
                "title": f"issue {n}", "description": "d", "severity": "low",
                "phase": "tool", "evidence_event_indices": [0], "confidence": 0.5})])

        deps, result = self._run(loaded_store, claude_session, script, max_turns=3)
        assert result.hit_turn_cap, "a cap hit is an outcome, not an exception"
        assert len(deps.collected) >= 2, "work done before the cap must not be lost"

    def test_tool_responses_are_cached_by_file_content(self, loaded_store, claude_session, tmp_path):
        from ossuary.agents.deps import ScannerDeps

        cache = Cache(tmp_path)
        deps = ScannerDeps(
            store=loaded_store, session_id=claude_session.session_id,
            session_content_hash=claude_session.content_hash, tool_stats=[], cache=cache,
        )
        first = deps.cached_or("read_events", {"start": 0, "end": 2},
                               lambda: loaded_store.read_events(claude_session.session_id, 0, 2))
        assert cache.writes == 1
        second = deps.cached_or("read_events", {"start": 0, "end": 2},
                                lambda: pytest.fail("should not recompute"))
        assert second == first and cache.hits == 1


class TestReport:
    def _manifest(self, claude_session: Session) -> RunManifest:
        from ossuary.models import Cluster

        issue = StoredIssue(
            issue_id=issue_id_for(claude_session.session_id, 0, "Capped output"),
            session_id=claude_session.session_id,
            source="claude-code",
            session_path=claude_session.path,
            title="Bash output capped at 30000 bytes",
            description="Cut mid-line, exit code 0, no indication to the agent.",
            severity="high", phase="harness",
            evidence_event_indices=[4, 5], confidence=0.9,
        )
        return RunManifest(
            run_id="run-test", started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            scanner_model="anthropic:claude-haiku-4-5",
            clusterer_model="anthropic:claude-sonnet-5",
            session_count=1, event_count=len(claude_session.events), issue_count=1,
            sources={"claude-code": 1},
            scans=[SessionScan(
                session_id=claude_session.session_id, source="claude-code",
                path=claude_session.path, content_hash=claude_session.content_hash,
                issues=[issue], turns_used=6,
            )],
            tool_stats=compute_tool_stats([claude_session]),
            clusters=[Cluster(
                cluster_id="capped-output-abc123", name="Tool output capped without indication",
                summary="Results stop at a fixed byte count with no marker.",
                member_issue_ids=[issue.issue_id],
                affected_sessions=[claude_session.session_id],
                first_seen_run="run-test", is_new_this_run=True,
            )],
        )

    def test_renders_a_self_contained_document(self, claude_session, loaded_store):
        html = render_html(self._manifest(claude_session), loaded_store)
        assert html.startswith("<!doctype html>")
        assert "</html>" in html
        # No CDN links: it must work as a ticket attachment with no network.
        for marker in ("http://", "https://", "<script src", "<link rel=\"stylesheet\""):
            assert marker not in html, f"external reference {marker!r} in report"

    def test_shows_the_key_sections(self, claude_session, loaded_store):
        html = render_html(self._manifest(claude_session), loaded_store)
        for section in ("Run summary", "New issue types this run", "Clusters",
                        "Tool statistics", "Sessions scanned"):
            assert section in html

    def test_evidence_excerpts_are_included_and_marked(self, claude_session, loaded_store):
        context = build_context(self._manifest(claude_session), loaded_store)
        issue_id = context["issues"][0].issue_id
        excerpts = context["evidence"][issue_id]
        assert excerpts
        capped = [e for e in excerpts if e["shape"] and e["shape"]["byte_length"] == 30000]
        assert capped, "the capped payload should be among the evidence"
        assert "ossuary:elided" in capped[0]["text"], "a shortened excerpt must be labelled"

    def test_renders_without_a_store(self, claude_session):
        html = render_html(self._manifest(claude_session), None)
        assert "Clusters" in html

    def test_renders_an_empty_run(self):
        manifest = RunManifest(run_id="empty", started_at=datetime.now(timezone.utc))
        html = render_html(manifest, None)
        assert "No issues were found" in html or "No clusters" in html

    def test_unclustered_issues_are_surfaced(self, claude_session, loaded_store):
        manifest = self._manifest(claude_session)
        manifest.clusters[0].member_issue_ids = []
        html = render_html(manifest, loaded_store)
        assert "Unclustered issues" in html

    def test_html_is_escaped(self, claude_session, loaded_store):
        manifest = self._manifest(claude_session)
        manifest.scans[0].issues[0].title = "<script>alert('x')</script>"
        html = render_html(manifest, loaded_store)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_disabled_redaction_is_stated_in_the_report(self, claude_session, loaded_store):
        manifest = self._manifest(claude_session)
        manifest.redaction_enabled = False
        assert "redaction disabled" in render_html(manifest, loaded_store).lower()


def test_unclustered_issues_helper():
    from ossuary.models import Cluster

    issues = [
        StoredIssue(issue_id="a", session_id="s", source="claude-code", title="t",
                    description="d", severity="low", phase="tool", confidence=0.5),
        StoredIssue(issue_id="b", session_id="s", source="claude-code", title="t",
                    description="d", severity="low", phase="tool", confidence=0.5),
    ]
    clusters = [Cluster(cluster_id="c", name="n", summary="s", member_issue_ids=["a"])]
    assert [i.issue_id for i in unclustered_issues(issues, clusters)] == ["b"]
