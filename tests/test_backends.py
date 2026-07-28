"""Backend routing, the tool contract, and the MCP server.

Nothing here calls a model. What is being pinned down is the seam: which
backend a model string selects, that the tool surface is byte-identical across
backends, and that a tool failure is a result rather than an exception.
"""

from __future__ import annotations

import json

import pytest

from ossuary.agents.deps import ClustererDeps, ScannerDeps
from ossuary.agents.tools import clusterer_tools, scanner_tools
from ossuary.backends import (
    ClaudeAgentBackend,
    CopilotBackend,
    PydanticAIBackend,
    ToolSpec,
    backend_name,
    build_backend,
    needs_api_key,
    split_spec,
)
from ossuary.store import SessionStore


class TestSpecRouting:
    @pytest.mark.parametrize(
        "spec,expected_backend,expected_model",
        [
            ("claude-code:haiku", "claude-code", "haiku"),
            ("claude-code:", "claude-code", ""),
            ("copilot:gpt-5", "copilot", "gpt-5"),
            ("anthropic:claude-haiku-4-5", "pydantic-ai", "anthropic:claude-haiku-4-5"),
            ("ollama:qwen2.5-coder", "pydantic-ai", "ollama:qwen2.5-coder"),
            ("  claude-code:sonnet  ", "claude-code", "sonnet"),
        ],
    )
    def test_prefix_selects_the_backend(self, spec, expected_backend, expected_model):
        assert split_spec(spec) == (expected_backend, expected_model)
        assert backend_name(spec) == expected_backend

    def test_unclaimed_prefixes_reach_pydantic_ai_intact(self):
        """Every provider string that worked before the split still works."""
        assert split_spec("google-gla:gemini-2.0-flash") == (
            "pydantic-ai",
            "google-gla:gemini-2.0-flash",
        )

    def test_build_returns_the_right_class_without_credentials(self):
        assert isinstance(build_backend("claude-code:haiku"), ClaudeAgentBackend)
        assert isinstance(build_backend("copilot:gpt-5"), CopilotBackend)
        assert isinstance(build_backend("anthropic:claude-opus-5"), PydanticAIBackend)

    @pytest.mark.parametrize(
        "spec,needs_key",
        [
            ("claude-code:haiku", False),
            ("copilot:gpt-5", False),
            ("ollama:qwen2.5-coder", False),
            ("openai-compatible:local", False),
            ("anthropic:claude-haiku-4-5", True),
        ],
    )
    def test_which_specs_need_a_provider_key(self, spec, needs_key):
        assert needs_api_key(spec) is needs_key

    def test_subscription_backends_do_not_claim_a_temperature(self):
        """Reported, not silently dropped -- a configured temperature that does
        nothing is worse than one the CLI tells you it is ignoring."""
        assert not build_backend("claude-code:haiku").supports_temperature
        assert not build_backend("copilot:gpt-5").supports_temperature
        assert build_backend("anthropic:claude-opus-5").supports_temperature


class TestToolContract:
    def test_a_raising_handler_becomes_a_marked_result(self):
        def boom(args):
            raise ValueError("nope")

        spec = ToolSpec("t", "d", {"type": "object"}, boom)
        out = spec.call({})
        assert out.startswith("[[ossuary:tool-error t: ValueError: nope")

    def test_missing_arguments_do_not_raise(self):
        spec = ToolSpec("t", "d", {"type": "object"}, lambda args: str(args))
        assert spec.call(None) == "{}"

    def test_scanner_schemas_are_serializable_json_schema(self, loaded_store, claude_session):
        """Every backend takes a schema; if it will not serialize, none of them work."""
        deps = ScannerDeps(
            store=loaded_store,
            session_id=claude_session.session_id,
            session_content_hash=claude_session.content_hash,
        )
        specs = scanner_tools(deps)
        assert {s.name for s in specs} == {
            "read_events",
            "search_session",
            "read_event_slice",
            "tool_stats",
            "report_issue",
        }
        for spec in specs:
            schema = json.loads(json.dumps(spec.parameters))
            assert schema["type"] == "object"
            assert spec.description.strip(), f"{spec.name} has no description"

    def test_clusterer_collects_proposals_incrementally(self):
        """The same partial-results property Agent A has, for the same reason."""
        deps = ClustererDeps()
        (propose,) = clusterer_tools(deps)
        propose.call({"name": "Capped Bash output", "summary": "s", "member_issue_ids": ["a"]})
        propose.call(
            {
                "name": "Stale cache",
                "summary": "s",
                "member_issue_ids": ["b"],
                "existing_cluster_id": "stale-cache-1234",
            }
        )
        assert [c.name for c in deps.collected] == ["Capped Bash output", "Stale cache"]
        assert deps.collected[0].existing_cluster_id is None
        assert deps.collected[1].existing_cluster_id == "stale-cache-1234"


class TestSdkAdapters:
    """The wrappers, against the real SDK types rather than a mock of them."""

    def test_claude_agent_sdk_accepts_the_tool_surface(self, loaded_store, claude_session):
        claude_agent_sdk = pytest.importorskip("claude_agent_sdk")

        from ossuary.backends.claude_agent import SERVER_NAME, _as_sdk_tool

        deps = ScannerDeps(
            store=loaded_store,
            session_id=claude_session.session_id,
            session_content_hash=claude_session.content_hash,
        )
        specs = scanner_tools(deps)
        server = claude_agent_sdk.create_sdk_mcp_server(
            name=SERVER_NAME, version="1.0.0", tools=[_as_sdk_tool(s) for s in specs]
        )
        assert server is not None

    def test_claude_agent_sdk_handler_records_through_deps(self):
        pytest.importorskip("claude_agent_sdk")
        import anyio

        from ossuary.backends.claude_agent import _as_sdk_tool

        deps = ScannerDeps(store=SessionStore(), session_id="s", session_content_hash="h")
        report = next(s for s in scanner_tools(deps) if s.name == "report_issue")
        handler = _as_sdk_tool(report).handler
        result = anyio.run(
            handler,
            {
                "title": "t",
                "description": "d",
                "severity": "low",
                "phase": "tool",
                "evidence_event_indices": [2, 1, 1],
                "confidence": 0.7,
            },
        )
        assert result["content"][0]["type"] == "text"
        assert deps.collected[0].evidence_event_indices == [1, 2]

    def test_copilot_tool_carries_the_same_schema(self):
        pytest.importorskip("copilot")

        from ossuary.backends.copilot import _as_copilot_tool

        spec = clusterer_tools(ClustererDeps())[0]
        tool = _as_copilot_tool(spec)
        assert tool.name == spec.name
        assert tool.parameters == spec.parameters


class TestMcpServer:
    def test_every_tool_is_exposed_and_documented(self):
        pytest.importorskip("mcp")
        import anyio

        from ossuary.mcp_server import build_server

        tools = anyio.run(build_server(redact=True).list_tools)
        names = {t.name for t in tools}
        assert names == {
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
        for tool in tools:
            assert tool.description and tool.description.strip()

    def test_session_ids_resolve_by_prefix(self, golden_root):
        pytest.importorskip("mcp")

        from ossuary.mcp_server import _State

        state = _State([golden_root / "claude-code"], redact=True)
        state.ensure_loaded()
        assert state.resolve("sess-golden") == "sess-golden-0001"

    def test_an_unknown_session_says_where_to_look(self, golden_root):
        pytest.importorskip("mcp")

        from ossuary.mcp_server import _State

        state = _State([golden_root / "claude-code"], redact=True)
        with pytest.raises(ValueError, match="ossuary_sources"):
            state.resolve("nope")
