"""The plugin packages.

The method now lives in prompts rather than in Python, which means the things
that used to be enforced by a `for` loop are enforced by what those prompts say
and by which tools each agent is granted. Two of those are load-bearing enough
to pin down here:

  * Every tool a plugin names must actually exist. A typo in a frontmatter tool
    list fails silently at run time -- the agent simply never gets the tool.
  * A per-session investigator must not be able to list the corpus or end the
    run. That grant is what keeps one transcript per context window, which is
    what keeps findings in one session independent of findings in another.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import anyio
import pytest

from ossuary.mcp_server import build_server

PLUGINS = Path(__file__).parent.parent / "plugins"
CLAUDE = PLUGINS / "claude-code" / "ossuary"
COPILOT = PLUGINS / "copilot" / "ossuary"
MARKETPLACE = Path(__file__).parent.parent / ".claude-plugin" / "marketplace.json"

#: Tools that let an agent widen its own scope beyond the one session it was
#: given, or declare the whole run finished.
CORPUS_TOOLS = {"ossuary_sources", "ossuary_propose_cluster", "ossuary_known_clusters"}
TERMINAL_TOOLS = {"ossuary_write_run"}


@pytest.fixture(scope="module")
def server_tools() -> set[str]:
    return {t.name for t in anyio.run(build_server(redact=True).list_tools)}


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    block = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"^([a-zA-Z_-]+):\s*(.*)$", line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def tool_names(path: Path) -> set[str]:
    """Read a `tools:` list in either the Claude Code or Copilot spelling."""
    raw = frontmatter(path).get("tools", "")
    if raw.startswith("["):
        names = ast.literal_eval(raw)
    else:
        names = [n.strip() for n in raw.split(",") if n.strip()]
    # Claude Code namespaces MCP tools as mcp__<server>__<tool>.
    return {n.rsplit("__", 1)[-1] if n.startswith("mcp__") else n for n in names}


AGENT_FILES = [
    CLAUDE / "agents" / "session-investigator.md",
    COPILOT / "agents" / "session-investigator.agent.md",
]

SKILL_FILES = [
    CLAUDE / "skills" / "investigate" / "SKILL.md",
    CLAUDE / "skills" / "report" / "SKILL.md",
    COPILOT / "skills" / "investigate" / "SKILL.md",
]


class TestManifests:
    @pytest.mark.parametrize(
        "path",
        [
            CLAUDE / ".claude-plugin" / "plugin.json",
            CLAUDE / ".mcp.json",
            COPILOT / "plugin.json",
            COPILOT / ".mcp.json",
            MARKETPLACE,
        ],
        ids=lambda p: str(p.relative_to(Path(__file__).parent.parent)),
    )
    def test_manifest_is_valid_json(self, path: Path):
        assert json.loads(path.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "path", [CLAUDE / ".mcp.json", COPILOT / ".mcp.json"], ids=["claude", "copilot"]
    )
    def test_mcp_manifest_starts_the_published_entry_point(self, path: Path):
        """`ossuary-mcp` is the console script; a rename here breaks every install."""
        server = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["ossuary"]
        assert "ossuary-mcp" in server["args"]

    @pytest.mark.parametrize(
        "path", [CLAUDE / ".mcp.json", COPILOT / ".mcp.json"], ids=["claude", "copilot"]
    )
    def test_mcp_manifest_source_resolves_to_this_package(self, path: Path):
        """The `--from` argument has to name something that is actually this project.

        This has now been got wrong twice, in two different ways, and both times
        the only symptom was a plugin whose tools never appeared:

          * `--from ossuary` installed an unrelated PyPI project -- the name is
            taken by a dice analysis toolkit.
          * `--from git+.../hosom/ossuary` pointed at the default branch, which
            did not yet contain the package at all.

        So this does not check the spelling, it resolves the thing. The plugin
        ships inside the repository that provides the package, so substituting
        `${CLAUDE_PLUGIN_ROOT}` must land on a real pyproject declaring the
        `ossuary-mcp` entry point the manifest goes on to invoke.
        """
        server = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["ossuary"]
        args = server["args"]
        source = args[args.index("--from") + 1]

        assert source != "ossuary", (
            "a bare distribution name resolves to an unrelated PyPI project"
        )
        assert "${CLAUDE_PLUGIN_ROOT}" in source, (
            "point at the bundled repository rather than the network: a remote "
            "source can be unreachable, stale, or not yet published"
        )

        resolved = Path(source.replace("${CLAUDE_PLUGIN_ROOT}", str(path.parent))).resolve()
        pyproject = resolved / "pyproject.toml"
        assert pyproject.exists(), f"{source} resolves to {resolved}, which is not a project"

        text = pyproject.read_text(encoding="utf-8")
        assert 'name = "ossuary"' in text, f"{resolved} is some other project"
        entry_point = args[-1]
        assert f"{entry_point} =" in text, (
            f"{resolved} declares no {entry_point!r} script for the manifest to run"
        )

    def test_marketplace_points_at_a_real_plugin(self):
        entry = json.loads(MARKETPLACE.read_text(encoding="utf-8"))["plugins"][0]
        source = (MARKETPLACE.parent.parent / entry["source"]).resolve()
        assert (source / ".claude-plugin" / "plugin.json").exists()


class TestPromptsAreWellFormed:
    @pytest.mark.parametrize("path", AGENT_FILES + SKILL_FILES, ids=lambda p: p.parent.name)
    def test_has_a_description(self, path: Path):
        assert frontmatter(path).get("description", "").strip(" '\"")

    @pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.parent.parent.name)
    def test_every_named_tool_exists(self, path: Path, server_tools: set[str]):
        """A typo here does not raise -- the agent just never gets the tool."""
        unknown = tool_names(path) - server_tools
        assert not unknown, f"{path.name} names tools the server does not expose: {unknown}"


class TestPerSessionIsolation:
    """One transcript per context window, enforced by what each agent is granted."""

    @pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.parent.parent.name)
    def test_a_session_investigator_cannot_widen_its_own_scope(self, path: Path):
        granted = tool_names(path)
        assert not granted & CORPUS_TOOLS, (
            f"{path.name} can reach beyond its one session; that is what lets "
            f"findings in one transcript colour findings in another"
        )

    @pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.parent.parent.name)
    def test_a_session_investigator_cannot_end_the_run(self, path: Path):
        granted = tool_names(path)
        assert not granted & TERMINAL_TOOLS, (
            f"{path.name} can write the run, so one subagent finishing would "
            f"publish everyone else's partial findings"
        )

    @pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.parent.parent.name)
    def test_a_session_investigator_can_still_read_and_report(self, path: Path):
        granted = tool_names(path)
        assert {"ossuary_outline", "ossuary_read_events", "ossuary_report_issue"} <= granted
        assert "ossuary_tool_stats" in granted, "needed to tell a one-off from the norm"

    @pytest.mark.parametrize(
        "path",
        [CLAUDE / "skills" / "investigate" / "SKILL.md", COPILOT / "skills" / "investigate" / "SKILL.md"],
        ids=["claude", "copilot"],
    )
    def test_the_coordinating_skill_instructs_delegation(self, path: Path):
        """The subagent existing is not enough; something has to say to use it."""
        # Collapse wrapping: these are prose files, and where a line happens to
        # break is not the thing under test.
        text = " ".join(path.read_text(encoding="utf-8").lower().split())
        assert "session-investigator" in text, "the skill must name the agent to delegate to"
        assert "one session per context window" in text
        assert "per session" in text, "the skill must say how many agents to spawn"
