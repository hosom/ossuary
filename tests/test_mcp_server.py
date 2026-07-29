"""The MCP server -- the whole of Ossuary's agent-facing surface.

Nothing here calls a model. The host agent is on the other side of this
boundary, so what is worth pinning down is the contract it sees: which tools
exist, that they are documented well enough to be used without a manual, that
reads are redacted and labelled, and that findings only reach disk when asked
for.
"""

from __future__ import annotations

import json

import anyio
import pytest

from ossuary.mcp_server import MAX_EVENT_SPAN, _State, build_server
from ossuary.pipeline import read_manifest

EXPECTED_TOOLS = {
    "ossuary_sources",
    "ossuary_outline",
    "ossuary_read_events",
    "ossuary_search_session",
    "ossuary_read_event_slice",
    "ossuary_tool_stats",
    "ossuary_report_issue",
    "ossuary_propose_cluster",
    "ossuary_known_clusters",
    "ossuary_write_run",
}

AN_ISSUE = {
    "session_id": "sess-golden",
    "title": "Bash output capped at 30000 bytes",
    "description": "Cut mid-line with exit code 0 and no indication.",
    "severity": "high",
    "phase": "harness",
    "evidence_event_indices": [5, 5, 4],
    "confidence": 0.9,
}


@pytest.fixture
def claude_root(golden_root):
    return [golden_root / "claude-code"]


@pytest.fixture
def server(claude_root):
    return build_server(claude_root, redact=True)


def call(server, tool, /, **arguments) -> str:
    """Invoke a tool the way a host agent would, through the MCP dispatch.

    Positional-only up front so a tool argument named `name` -- which
    `ossuary_propose_cluster` has -- cannot collide with the helper's own.
    """
    result = anyio.run(lambda: server.call_tool(tool, arguments))
    # mcp 1.x hands back the content blocks (sometimes in a tuple with the
    # structured result); 2.x wraps them in a result object.
    blocks = getattr(result, "content", result)
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    return "\n".join(getattr(block, "text", str(block)) for block in blocks)


class TestToolSurface:
    def test_every_tool_is_exposed(self, server):
        assert {t.name for t in anyio.run(server.list_tools)} == EXPECTED_TOOLS

    def test_every_tool_carries_usable_guidance(self, server):
        """A description is the only instruction manual the host agent gets."""
        for tool in anyio.run(server.list_tools):
            assert tool.description and len(tool.description.strip()) > 40, tool.name

    def test_schemas_are_serializable(self, server):
        for tool in anyio.run(server.list_tools):
            # `inputSchema` on mcp 1.x, `input_schema` on 2.x.
            schema = getattr(tool, "inputSchema", None) or tool.input_schema
            assert json.loads(json.dumps(schema))["type"] == "object"


class TestReads:
    def test_sources_lists_the_corpus_and_states_the_redaction_setting(self, server):
        out = call(server, "ossuary_sources")
        assert "sess-golden-0001" in out
        assert "Redaction is on" in out

    def test_outline_covers_every_event(self, server, claude_session):
        out = call(server, "ossuary_outline", session_id="sess-golden")
        assert str(len(claude_session.events) - 1) in out

    def test_session_ids_resolve_by_prefix(self, claude_root):
        assert _State(claude_root, redact=True).resolve("sess-golden") == "sess-golden-0001"

    def test_an_unknown_session_says_where_to_look(self, claude_root):
        with pytest.raises(ValueError, match="ossuary_sources"):
            _State(claude_root, redact=True).resolve("nope")

    def test_an_oversized_span_is_capped_and_labelled(self, server):
        """Never silently shorten: that marker is what the agent's reasoning rests on."""
        out = call(server, "ossuary_read_events", session_id="sess-golden", start=0, end=500)
        assert "ossuary:elided" in out
        assert f"again from {MAX_EVENT_SPAN}" in out

    def test_tool_stats_without_a_name_surveys_the_corpus(self, server):
        assert "Bash" in call(server, "ossuary_tool_stats")

    def test_an_unknown_tool_name_suggests_real_ones(self, server):
        out = call(server, "ossuary_tool_stats", tool_name="NoSuchTool")
        assert "No corpus statistics" in out and "Bash" in out


class TestRecording:
    def test_nothing_reaches_disk_until_asked(self, server, tmp_path, monkeypatch):
        """An abandoned exploration must not leave artifacts `report` would render."""
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **AN_ISSUE)
        assert not (tmp_path / ".ossuary").exists()

    def test_write_run_produces_a_manifest_report_can_read(self, server, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **AN_ISSUE)
        call(
            server,
            "ossuary_propose_cluster",
            name="Tool output capped without indication",
            summary="Results stop at a fixed byte count with no marker.",
            member_issue_ids=[],
        )
        assert "run.json" in call(
            server, "ossuary_write_run", investigator="claude-code / opus"
        )

        manifest = read_manifest(tmp_path)
        assert manifest.issue_count == 1
        assert manifest.investigator == "claude-code / opus"
        assert manifest.redaction_enabled is True
        assert len(manifest.clusters) == 1 and manifest.clusters[0].is_new_this_run
        assert manifest.scans[0].issues[0].evidence_event_indices == [4, 5], "deduped and sorted"

    def test_confidence_is_clamped(self, server, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **{**AN_ISSUE, "confidence": 4.2})
        call(server, "ossuary_write_run")
        assert read_manifest(tmp_path).scans[0].issues[0].confidence == 1.0

    def test_a_second_run_reuses_the_stored_taxonomy(self, server, tmp_path, monkeypatch):
        """Stable names between runs are what make 'new this run' mean anything."""
        monkeypatch.chdir(tmp_path)
        cluster = {
            "name": "Tool output capped without indication",
            "summary": "s",
            "member_issue_ids": [],
        }
        call(server, "ossuary_propose_cluster", **cluster)
        call(server, "ossuary_write_run")

        assert "Tool output capped" in call(server, "ossuary_known_clusters")

        call(server, "ossuary_propose_cluster", **cluster)
        call(server, "ossuary_write_run")

        clusters = read_manifest(tmp_path).clusters
        assert len(clusters) == 1
        assert not clusters[0].is_new_this_run, "a second sighting is not a new failure mode"

    def test_writing_a_run_clears_the_buffer(self, server, tmp_path, monkeypatch):
        """Otherwise a second write would double-count the first run's findings."""
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **AN_ISSUE)
        call(server, "ossuary_write_run")
        call(server, "ossuary_write_run", allow_empty=True)
        assert read_manifest(tmp_path).issue_count == 0


class TestProtectingAFinishedRun:
    """Findings live in memory until they are written, so a server that restarts
    mid-investigation comes back empty with no way to know it ever held anything.
    The next write must not turn a finished investigation into a blank one."""

    def test_an_empty_run_will_not_overwrite_findings(self, server, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **AN_ISSUE)
        call(server, "ossuary_write_run")

        with pytest.raises(Exception, match="Refusing to write a run with no issues"):
            call(server, "ossuary_write_run")

        assert read_manifest(tmp_path).issue_count == 1, "the good run is untouched"

    def test_the_refusal_says_where_the_previous_run_is(self, server, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **AN_ISSUE)
        call(server, "ossuary_write_run")
        run_id = read_manifest(tmp_path).run_id

        with pytest.raises(Exception, match=f"runs/{run_id}.json"):
            call(server, "ossuary_write_run")
        assert (tmp_path / ".ossuary" / "runs" / f"{run_id}.json").exists()

    def test_finding_nothing_is_still_a_result(self, server, tmp_path, monkeypatch):
        """The first run of a clean corpus has nothing to destroy."""
        monkeypatch.chdir(tmp_path)
        assert "run.json" in call(server, "ossuary_write_run")
        assert read_manifest(tmp_path).issue_count == 0

    def test_an_empty_run_can_be_written_deliberately(self, server, tmp_path, monkeypatch):
        """A corpus that was dirty and is now clean is a real thing to record."""
        monkeypatch.chdir(tmp_path)
        call(server, "ossuary_report_issue", **AN_ISSUE)
        call(server, "ossuary_write_run")
        assert "run.json" in call(server, "ossuary_write_run", allow_empty=True)
        assert read_manifest(tmp_path).issue_count == 0


class TestRedaction:
    def test_disabling_redaction_is_stated_to_the_agent(self, claude_root):
        """The host agent is a model too -- it must be told what it is looking at."""
        assert "OFF" in call(build_server(claude_root, redact=False), "ossuary_sources")
