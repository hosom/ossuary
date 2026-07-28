"""Backend selection.

The model string in `agents.yaml` names the backend as its prefix:

    claude-code:haiku               Claude Agent SDK -- Claude Code's own credentials
    copilot:gpt-5                   GitHub Copilot SDK -- the signed-in `gh` user
    anthropic:claude-haiku-4-5      Pydantic AI -- ANTHROPIC_API_KEY
    ollama:qwen2.5-coder            Pydantic AI -- a local endpoint

Anything Ossuary does not claim is handed to Pydantic AI unchanged, so every
provider string that worked before still works.
"""

from __future__ import annotations

from .base import (
    AgentBackend,
    AgentRunResult,
    BackendConfig,
    BackendUnavailable,
    ToolSpec,
    object_schema,
)
from .claude_agent import ClaudeAgentBackend
from .copilot import CopilotBackend
from .pydantic_ai_backend import PydanticAIBackend, is_local_model, resolve_model

#: Prefixes Ossuary routes itself. Everything else falls through to Pydantic AI.
BACKENDS: dict[str, type[AgentBackend]] = {
    ClaudeAgentBackend.name: ClaudeAgentBackend,
    CopilotBackend.name: CopilotBackend,
}

#: Backends that need no Anthropic API key, for the message `ossuary scan`
#: prints when a run fails on credentials.
KEYLESS = frozenset({ClaudeAgentBackend.name, CopilotBackend.name})


def split_spec(spec: str) -> tuple[str, str]:
    """Split `backend:model` into its parts, defaulting to Pydantic AI."""
    spec = spec.strip()
    prefix, _, rest = spec.partition(":")
    if prefix in BACKENDS:
        return prefix, rest.strip()
    return PydanticAIBackend.name, spec


def backend_name(spec: str) -> str:
    return split_spec(spec)[0]


def needs_api_key(spec: str) -> bool:
    """Whether running this spec requires a hosted-provider credential."""
    name, model = split_spec(spec)
    if name in KEYLESS:
        return False
    return not is_local_model(model)


def build_backend(
    spec: str,
    *,
    temperature: float = 0.0,
    max_turns: int = 15,
    max_tokens: int | None = None,
    extra: dict | None = None,
) -> AgentBackend:
    """Construct the backend named by `spec`. Does not require credentials."""
    name, model = split_spec(spec)
    config = BackendConfig(
        model=model,
        temperature=temperature,
        max_turns=max_turns,
        max_tokens=max_tokens,
        extra=dict(extra or {}),
    )
    return BACKENDS.get(name, PydanticAIBackend)(config)


__all__ = [
    "BACKENDS",
    "KEYLESS",
    "AgentBackend",
    "AgentRunResult",
    "BackendConfig",
    "BackendUnavailable",
    "ClaudeAgentBackend",
    "CopilotBackend",
    "PydanticAIBackend",
    "ToolSpec",
    "backend_name",
    "build_backend",
    "is_local_model",
    "needs_api_key",
    "object_schema",
    "resolve_model",
    "split_spec",
]
