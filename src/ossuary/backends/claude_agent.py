"""The Claude Agent SDK backend -- no Anthropic API key required.

The Agent SDK runs the Claude Code harness as a library, and inherits whatever
credentials that harness already has. On a machine where `claude` is logged in
with a Pro or Max subscription, that is the subscription; where
`ANTHROPIC_API_KEY` is set, that is the key; on Bedrock/Vertex/Foundry it is
those. Ossuary does not authenticate anything itself and deliberately holds no
credential of its own.

Two things are pinned deliberately and should not be relaxed casually:

  * `setting_sources=[]`. Without it the harness would load the operator's
    `CLAUDE.md`, skills, and plugins into the scan. Ossuary's whole premise is
    that Agent A is given no taxonomy of known problems; inheriting a user's
    project instructions would smuggle one in and quietly make findings
    unreproducible across machines.
  * `permission_mode="dontAsk"` with only Ossuary's own tools pre-approved. The
    scanner has no business reading the filesystem directly -- every byte it
    sees should arrive through the store, which redacts and elides on the way
    out. Denying by default is what enforces that.

Temperature is not settable through this backend. It is reported as unsupported
rather than silently dropped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from .base import AgentBackend, AgentRunResult, BackendUnavailable, ToolSpec

#: The in-process MCP server Ossuary's tools are exposed through. Tool names the
#: model sees are `mcp__ossuary__<name>`.
SERVER_NAME = "ossuary"

#: `subtype` values the SDK reports when a run ended on its own limits rather
#: than because the agent finished.
CAP_SUBTYPES = frozenset({"error_max_turns", "error_max_budget"})


class ClaudeAgentBackend(AgentBackend):
    name = "claude-code"
    supports_temperature = False

    def run(
        self,
        *,
        instructions: str,
        prompt: str,
        tools: Sequence[ToolSpec],
    ) -> AgentRunResult:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailable(
                "the claude-code backend needs the Claude Agent SDK: "
                "pip install 'ossuary[claude-code]'"
            ) from exc

        return asyncio.run(self._run(instructions, prompt, tools))

    async def _run(
        self,
        instructions: str,
        prompt: str,
        tools: Sequence[ToolSpec],
    ) -> AgentRunResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            create_sdk_mcp_server,
            query,
        )

        server = create_sdk_mcp_server(
            name=SERVER_NAME,
            version="1.0.0",
            tools=[_as_sdk_tool(spec) for spec in tools],
        )
        options = ClaudeAgentOptions(
            model=self.config.model or None,
            system_prompt=instructions,
            max_turns=self.config.max_turns,
            mcp_servers={SERVER_NAME: server},
            allowed_tools=[f"mcp__{SERVER_NAME}__{spec.name}" for spec in tools],
            permission_mode="dontAsk",
            setting_sources=[],
        )

        text = ""
        turns = 0
        hit_cap = False
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                turns += 1
            elif isinstance(message, ResultMessage):
                turns = message.num_turns or turns
                hit_cap = message.subtype in CAP_SUBTYPES
                if isinstance(message.result, str):
                    text = message.result

        return AgentRunResult(text=text, turns=turns, hit_turn_cap=hit_cap)


def _as_sdk_tool(spec: ToolSpec) -> Any:
    from claude_agent_sdk import tool

    @tool(spec.name, spec.description, spec.parameters)
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": spec.call(args)}]}

    return _handler


__all__ = ["CAP_SUBTYPES", "SERVER_NAME", "ClaudeAgentBackend"]
