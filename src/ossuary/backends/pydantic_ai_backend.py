"""The API-key backend.

This is the original path: Pydantic AI against a hosted Messages API, or against
a local endpoint. It needs a key (or a local server) and it is the only backend
that can set a temperature, which makes it the one to use when you care about
run-to-run determinism.

No LiteLLM, no Instructor. If a provider is not natively supported, point
Pydantic AI at an OpenAI-compatible proxy via `openai-compatible:`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from .base import AgentBackend, AgentRunResult, BackendUnavailable, ToolSpec

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"


def resolve_model(spec: str) -> Any:
    """Turn a model string into something `Agent(...)` accepts.

    Recognised local/proxy forms:

        ollama:qwen2.5-coder            -> local Ollama, OLLAMA_BASE_URL or default
        openai-compatible:<model>       -> OSSUARY_OPENAI_BASE_URL, any proxy

    Everything else is handed to Pydantic AI unchanged, e.g.
    `anthropic:claude-haiku-4-5`.
    """
    spec = spec.strip()

    if spec.startswith("ollama:"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.ollama import OllamaProvider

        model_name = spec.split(":", 1)[1]
        base_url = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)
        return OpenAIChatModel(model_name, provider=OllamaProvider(base_url=base_url))

    if spec.startswith("openai-compatible:"):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        model_name = spec.split(":", 1)[1]
        base_url = os.environ.get("OSSUARY_OPENAI_BASE_URL")
        if not base_url:
            raise ValueError(
                "model 'openai-compatible:' requires OSSUARY_OPENAI_BASE_URL to be set"
            )
        return OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(
                base_url=base_url,
                api_key=os.environ.get("OSSUARY_OPENAI_API_KEY", "not-needed"),
            ),
        )

    return spec


def is_local_model(spec: str) -> bool:
    return spec.strip().startswith(("ollama:", "openai-compatible:"))


class PydanticAIBackend(AgentBackend):
    name = "pydantic-ai"
    supports_temperature = True

    def run(
        self,
        *,
        instructions: str,
        prompt: str,
        tools: Sequence[ToolSpec],
    ) -> AgentRunResult:
        try:
            from pydantic_ai import Agent, UsageLimits
            from pydantic_ai.settings import ModelSettings
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise BackendUnavailable(
                "the pydantic-ai backend needs `pydantic-ai`: pip install 'ossuary'"
            ) from exc

        config = self.config
        # `model_object` lets a caller hand in an already-constructed Pydantic AI
        # model. Tests use it to drive the real tool wiring against a scripted
        # model without a provider; nothing in normal operation sets it.
        model = config.extra.get("model_object") or resolve_model(config.model)
        agent = Agent(
            model,
            output_type=str,
            instructions=instructions,
            model_settings=ModelSettings(
                temperature=config.temperature,
                **({"max_tokens": config.max_tokens} if config.max_tokens else {}),
            ),
            tools=[_as_pydantic_tool(spec) for spec in tools],
            retries=2,
            # Resolve the provider at run time, not construction time. Building
            # the agent must not require credentials: `ossuary agents test` runs
            # without `--live`, and a missing key should surface when a real call
            # is actually attempted.
            defer_model_check=True,
        )

        # `request_limit` counts model requests, which is the turn count for a
        # tool-using loop -- one request per turn plus the final answer.
        limits = UsageLimits(request_limit=config.max_turns)
        try:
            result = agent.run_sync(prompt, usage_limits=limits)
        except Exception as exc:  # noqa: BLE001 - a cap hit is an outcome
            if "UsageLimit" in type(exc).__name__:
                return AgentRunResult(text="", turns=config.max_turns, hit_turn_cap=True)
            raise
        return AgentRunResult(text=str(result.output), turns=result.usage.requests)


def _as_pydantic_tool(spec: ToolSpec) -> Any:
    from pydantic_ai.tools import Tool

    def call(**kwargs: Any) -> str:
        return spec.call(kwargs)

    return Tool.from_schema(
        call,
        name=spec.name,
        description=spec.description,
        json_schema=spec.parameters,
    )


__all__ = ["DEFAULT_OLLAMA_URL", "PydanticAIBackend", "is_local_model", "resolve_model"]
