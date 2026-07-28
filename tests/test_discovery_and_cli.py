"""Discovery routing and CLI smoke tests."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ossuary.adapters import get_adapter
from ossuary.cache import Cache
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

    def test_all_four_fixtures_are_found(self):
        refs = SessionStore().discover(roots=[GOLDEN])
        assert {r.source for r in refs} == {"claude-code", "codex", "copilot"}
        assert len(refs) == 4

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


def test_issue_cache_key_separates_sources():
    """Regression: one file read by two adapters shared a cache entry."""
    args = dict(schema_version=1, redacted=True)
    assert Cache.issues_key("h", "p", "m", source="claude-code", **args) != Cache.issues_key(
        "h", "p", "m", source="codex", **args
    )


class TestCli:
    def test_sources_lists_every_adapter(self):
        result = runner.invoke(app, ["sources"])
        assert result.exit_code == 0
        for source in ("claude-code", "codex", "copilot"):
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

    def test_agents_show(self, tmp_path, monkeypatch):
        monkeypatch.chdir(Path(__file__).parent.parent)
        result = runner.invoke(app, ["agents", "show"])
        assert result.exit_code == 0
        assert "scanner" in result.output and "clusterer" in result.output
        assert "prompt_version" in result.output

    def test_agents_test_dry_run_spends_nothing(self, monkeypatch):
        monkeypatch.chdir(Path(__file__).parent.parent)
        result = runner.invoke(app, ["agents", "test", "scanner", "--fixture", str(GOLDEN)])
        assert result.exit_code == 0
        assert "Prompt assembly OK" in result.output
        assert "tokens" in result.output

    def test_agents_test_rejects_unknown_agent(self, monkeypatch):
        monkeypatch.chdir(Path(__file__).parent.parent)
        result = runner.invoke(app, ["agents", "test", "nope", "--fixture", str(GOLDEN)])
        assert result.exit_code == 1
        assert "unknown agent" in result.output

    def test_report_without_a_scan_fails_clearly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["report", "--no-open"])
        assert result.exit_code == 1
        assert "ossuary scan" in result.output

    def test_scan_then_report_end_to_end(self, tmp_path, monkeypatch):
        """Full pipeline against a stub model: scan writes artifacts, report renders them."""
        import shutil

        shutil.copy(Path(__file__).parent.parent / "agents.yaml", tmp_path / "agents.yaml")
        monkeypatch.chdir(tmp_path)

        scan = runner.invoke(
            app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster"]
        )
        assert scan.exit_code == 0, scan.output
        assert (tmp_path / ".ossuary" / "run.json").exists()

        report = runner.invoke(app, ["report", "--no-open"])
        assert report.exit_code == 0, report.output
        html = (tmp_path / "report.html").read_text(encoding="utf-8")
        assert html.startswith("<!doctype html>")
        assert "Run summary" in html

    def test_rescan_is_served_from_cache(self, tmp_path, monkeypatch):
        import shutil

        shutil.copy(Path(__file__).parent.parent / "agents.yaml", tmp_path / "agents.yaml")
        monkeypatch.chdir(tmp_path)

        first = runner.invoke(app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster"])
        assert "0 served from cache" in first.output
        second = runner.invoke(app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster"])
        assert "4 served from cache" in second.output, "an unchanged session must cost nothing"

    def test_no_cache_flag_bypasses_the_cache(self, tmp_path, monkeypatch):
        import shutil

        shutil.copy(Path(__file__).parent.parent / "agents.yaml", tmp_path / "agents.yaml")
        monkeypatch.chdir(tmp_path)

        runner.invoke(app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster"])
        again = runner.invoke(
            app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster", "--no-cache"]
        )
        assert "0 served from cache" in again.output

    def test_no_redact_warns_loudly(self, tmp_path, monkeypatch):
        import shutil

        shutil.copy(Path(__file__).parent.parent / "agents.yaml", tmp_path / "agents.yaml")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["scan", str(GOLDEN), "--model", "test", "--no-cluster", "--no-redact"],
        )
        assert result.exit_code == 0
        assert "redaction disabled" in result.output

    def test_limit_caps_the_session_count(self, tmp_path, monkeypatch):
        import shutil

        shutil.copy(Path(__file__).parent.parent / "agents.yaml", tmp_path / "agents.yaml")
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster", "--limit", "1"]
        )
        assert "Parsed 1 session(s)" in result.output

    def test_export_writes_jsonl(self, tmp_path, monkeypatch):
        import json
        import shutil

        shutil.copy(Path(__file__).parent.parent / "agents.yaml", tmp_path / "agents.yaml")
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["scan", str(GOLDEN), "--model", "test", "--no-cluster"])
        result = runner.invoke(app, ["export", "--out", "issues.jsonl"])
        assert result.exit_code == 0
        rows = [json.loads(l) for l in (tmp_path / "issues.jsonl").read_text().splitlines()]
        assert rows and "issue_id" in rows[0] and "run_id" in rows[0]
