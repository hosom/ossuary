"""Discovery routing and the deterministic CLI."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ossuary.adapters import get_adapter
from ossuary.cli import app
from ossuary.store import SessionStore

GOLDEN = Path(__file__).parent / "golden"
runner = CliRunner()


class TestAdapterClaiming:
    """Regression: an explicit path once made every adapter claim every file."""

    def test_each_fixture_is_claimed_by_exactly_one_adapter(self):
        store = SessionStore()
        refs = store.discover(roots=[GOLDEN])
        by_path: dict[str, list[str]] = {}
        for ref in refs:
            by_path.setdefault(ref.path, []).append(ref.source)
        for path, sources in by_path.items():
            assert len(sources) == 1, f"{path} claimed by {sources}"

    def test_every_fixture_is_found(self):
        refs = SessionStore().discover(roots=[GOLDEN])
        assert {r.source for r in refs} == {"claude-code", "codex", "copilot", "pi"}
        assert len(refs) == 6

    def test_codex_does_not_claim_a_claude_code_file(self):
        path = next((GOLDEN / "claude-code").rglob("*.jsonl"))
        assert get_adapter("claude-code").claims(path)
        assert not get_adapter("codex").claims(path)

    def test_claude_code_does_not_claim_a_codex_rollout(self):
        path = next((GOLDEN / "codex").rglob("*.jsonl"))
        assert get_adapter("codex").claims(path)
        assert not get_adapter("claude-code").claims(path)

    def test_an_explicit_single_source_bypasses_the_sniff(self):
        """Naming a source is an instruction, so forcing must still work."""
        codex_file = next((GOLDEN / "codex").rglob("*.jsonl"))
        refs = SessionStore().discover(["claude-code"], roots=[codex_file])
        assert len(refs) == 1, "an explicit --source must be able to force a parse"

    def test_a_claimed_file_still_parses(self):
        store = SessionStore()
        for ref in store.discover(roots=[GOLDEN]):
            session = store.load(ref)
            assert session.events, f"{ref.path} produced no events"

class TestCli:
    """The commands that sit around an investigation, none of which call a model."""

    def test_sources_lists_every_adapter(self):
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0
        for source in ("claude-code", "codex", "copilot", "pi"):
            assert source in result.output
        assert "session(s) total" in result.output

    def test_sources_reports_absent_locations_honestly(self):
        result = runner.invoke(app, ["sources", "--source", "codex"])
        assert result.exit_code == 0
        assert "searched:" in result.output

    def test_sources_with_explicit_path(self):
        result = runner.invoke(app, ["sources", str(GOLDEN)])
        assert result.exit_code == 0
        assert "claude-code: 1 session(s)" in result.output

    def test_unknown_source_is_rejected(self):
        result = runner.invoke(app, ["sources", "--source", "nonsense"])
        assert result.exit_code == 1
        assert "unknown source" in result.output

    def test_outline_command_is_deterministic_and_needs_no_model(self):
        result = runner.invoke(app, ["outline", str(GOLDEN / "claude-code")])
        assert result.exit_code == 0
        assert "SESSION sess-golden-0001" in result.output
        assert "idx" in result.output

    def test_report_without_artifacts_points_at_the_plugin(self, tmp_path, monkeypatch):
        """The fix is to go and investigate something, so the error has to say so."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["report", "--no-open"])
        assert result.exit_code == 1
        assert "ossuary_write_run" in result.output

    def test_investigate_then_report_end_to_end(self, tmp_path, monkeypatch):
        """What the plugin actually does: the agent records, then `report` renders."""
        monkeypatch.chdir(tmp_path)
        _record_a_finding(tmp_path)

        result = runner.invoke(app, ["report", "--no-open"])
        assert result.exit_code == 0, result.output
        html = (tmp_path / "report.html").read_text(encoding="utf-8")
        assert html.startswith("<!doctype html>")
        assert "Run summary" in html
        assert "Bash output capped" in html

    def test_export_writes_jsonl(self, tmp_path, monkeypatch):
        import json

        monkeypatch.chdir(tmp_path)
        _record_a_finding(tmp_path)

        result = runner.invoke(app, ["export", "--out", "issues.jsonl"])
        assert result.exit_code == 0
        rows = [json.loads(l) for l in (tmp_path / "issues.jsonl").read_text().splitlines()]
        assert rows and "issue_id" in rows[0] and "run_id" in rows[0]

    def test_taxonomy_shows_then_clears(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _record_a_finding(tmp_path)

        shown = runner.invoke(app, ["taxonomy"])
        assert shown.exit_code == 0
        assert "Tool output capped" in shown.output

        cleared = runner.invoke(app, ["taxonomy", "--clear"])
        assert cleared.exit_code == 0
        assert "No stored taxonomy" in runner.invoke(app, ["taxonomy"]).output


def _record_a_finding(cwd: Path) -> None:
    """Drive the MCP server the way a host agent would, leaving `.ossuary/` behind."""
    import anyio

    from ossuary.mcp_server import build_server

    server = build_server([GOLDEN / "claude-code"], redact=True)

    def call(tool, /, **arguments):
        # Return shape differs between mcp majors; nothing here reads it.
        return anyio.run(lambda: server.call_tool(tool, arguments))

    call(
        "ossuary_report_issue",
        session_id="sess-golden",
        title="Bash output capped at 30000 bytes",
        description="Cut mid-line with exit code 0 and no indication.",
        severity="high",
        phase="harness",
        evidence_event_indices=[4, 5],
        confidence=0.9,
    )
    call(
        "ossuary_propose_cluster",
        name="Tool output capped without indication",
        summary="Results stop at a fixed byte count with no marker.",
        member_issue_ids=[],
    )
    call("ossuary_write_run", investigator="claude-code / opus")
