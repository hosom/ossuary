"""The GitHub Copilot SDK backend -- no Anthropic API key required.

The Copilot SDK exposes the same agent runtime as the Copilot CLI as a library,
and authenticates the same way the CLI does: the signed-in `gh` user, or a
`github_token`, or a bring-your-own-key provider. On a machine with a Copilot
subscription that is the subscription. As with the Claude Agent SDK backend,
Ossuary holds no credential of its own.

The same two lockdowns as the Claude backend apply, for the same reasons:

  * Repository instructions, skills, and plugin directories are switched off, so
    the operator's local configuration cannot smuggle a taxonomy of known
    problems into a scan that is supposed to discover them.
  * Built-in tools are switched off. Everything the agent reads arrives through
    Ossuary's own tools, which redact and elide on the way out.

Temperature is not settable through this backend; `reasoning_effort` is the
nearest knob and is exposed through `agents.yaml` as `extra.reasoning_effort`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from .base import AgentBackend, AgentRunResult, BackendUnavailable, ToolSpec

#: Copilot's `send_and_wait` defaults to a 60s ceiling, which a full session
#: investigation blows through routinely. Overridable per agent via
#: `extra.timeout_seconds` in `agents.yaml`.
DEFAULT_TIMEOUT_SECONDS = 900.0

ASSISTANT_MESSAGE = "assistant.message"


class CopilotBackend(AgentBackend):
    name = "copilot"
    supports_temperature = False

    def run(
        self,
        *,
        instructions: str,
        prompt: str,
        tools: Sequence[ToolSpec],
    ) -> AgentRunResult:
        try:
            import copilot  # noqa: F401
        except ImportError as exc:
            raise BackendUnavailable(
                "the copilot backend needs the GitHub Copilot SDK: "
                "pip install 'ossuary[copilot]'"
            ) from exc

        return asyncio.run(self._run(instructions, prompt, tools))

    async def _run(
        self,
        instructions: str,
        prompt: str,
        tools: Sequence[ToolSpec],
    ) -> AgentRunResult:
        from copilot import CopilotClient, PermissionHandler

        extra = self.config.extra
        texts: list[str] = []
        turns = 0

        def on_event(event: Any) -> None:
            nonlocal turns
            if _event_type(event) != ASSISTANT_MESSAGE:
                return
            turns += 1
            content = getattr(getattr(event, "data", None), "content", None)
            if isinstance(content, str) and content.strip():
                texts.append(content)

        async with CopilotClient(
            github_token=extra.get("github_token"),
        ) as client:
            session = await client.create_session(
                model=self.config.model or None,
                tools=[_as_copilot_tool(spec) for spec in tools],
                # Only Ossuary's tools. The agent reads transcripts through the
                # store or not at all.
                available_tools=[],
                system_message={"mode": "replace", "content": instructions},
                on_permission_request=PermissionHandler.approve_all,
                on_event=on_event,
                # Keep the operator's local Copilot configuration out of the run.
                skip_custom_instructions=True,
                enable_config_discovery=False,
                enable_skills=False,
                reasoning_effort=extra.get("reasoning_effort"),
                session_limits={"max_turns": self.config.max_turns},
            )
            async with session:
                await session.send_and_wait(
                    prompt,
                    timeout=float(extra.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
                )

        # Copilot enforces the turn ceiling itself and simply stops; there is no
        # distinct "hit the cap" signal, so infer it the only way available.
        return AgentRunResult(
            text=texts[-1] if texts else "",
            turns=turns,
            hit_turn_cap=turns >= self.config.max_turns,
        )


def _event_type(event: Any) -> str:
    value = getattr(event, "type", "")
    return getattr(value, "value", value) or ""


def _as_copilot_tool(spec: ToolSpec) -> Any:
    from copilot import Tool, ToolResult

    def handler(invocation: Any) -> Any:
        arguments = getattr(invocation, "arguments", None) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        return ToolResult(text_result_for_llm=spec.call(arguments))

    return Tool(
        name=spec.name,
        description=spec.description,
        handler=handler,
        parameters=spec.parameters,
        skip_permission=True,
    )


__all__ = ["ASSISTANT_MESSAGE", "DEFAULT_TIMEOUT_SECONDS", "CopilotBackend"]
