"""Corpus aggregates and the HTML report."""

from __future__ import annotations

from datetime import datetime, timezone

from ossuary.aggregate import compute_tool_stats, render_tool_stats
from ossuary.models import RunManifest, Session, SessionScan, StoredIssue
from ossuary.pipeline import issue_id_for, unclustered_issues
from ossuary.report import build_context, render_html


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
            investigator="claude-code / opus",
            session_count=1, event_count=len(claude_session.events), issue_count=1,
            sources={"claude-code": 1},
            scans=[SessionScan(
                session_id=claude_session.session_id, source="claude-code",
                path=claude_session.path, content_hash=claude_session.content_hash,
                issues=[issue],
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
                        "Tool statistics", "Sessions examined"):
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
