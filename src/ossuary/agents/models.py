"""Model resolution.

Pydantic AI handles provider selection natively from a `provider:model` string,
so most names pass straight through. The one case that needs help is a local
Ollama endpoint, which section 12 requires to be a working configuration rather
than an aspiration -- running a redaction pass and then shipping transcripts to
a hosted API is a weaker privacy story than never sending them at all.

No LiteLLM, no Instructor. If a provider is not natively supported, point
Pydantic AI at an OpenAI-compatible proxy via `openai-compatible:`.
"""

from __future__ import annotations

import os

from pydantic_ai.models import Model

DEFAULT_OLLAMA_URL = "http://localhost:11434/v1"


def resolve_model(spec: str) -> Model | str:
    """Turn an `agents.yaml` model string into something `Agent(...)` accepts.

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
        return OpenAIChatModel(
            model_name,
            provider=OllamaProvider(base_url=base_url),
        )

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
