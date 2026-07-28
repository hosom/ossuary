"""The seam between Ossuary's agents and whoever actually runs inference.

Ossuary's two agents are both the same shape: a system prompt, one user turn, a
handful of Python tools, and a turn cap. Nothing about that shape is specific to
an API vendor, so it is described once here and implemented three ways --
Pydantic AI (an API key), the Claude Agent SDK (whatever Claude Code is logged
in as), and the GitHub Copilot SDK (whatever `gh` is logged in as).

Two decisions worth not undoing:

  * `ToolSpec` carries a raw JSON Schema rather than a typed callable. Every
    backend wants a schema in the end, and going through a schema keeps the tool
    surface byte-identical across backends -- which is what makes a finding
    reproducible when you switch where inference runs.
  * `ToolSpec.call` never raises. A bad regex from the model is a tool result,
    not a crashed scan. Backends differ in what they do with a raised exception
    (retry, abort, swallow), and that difference would otherwise leak into
    results.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar


class BackendUnavailable(RuntimeError):
    """A backend was named in `agents.yaml` but its dependency is not installed."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool, described the way every backend needs it described."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]

    def call(self, arguments: dict[str, Any] | None) -> str:
        """Run the tool, turning any failure into a result the model can read.

        The marker is the same shape as every other Ossuary annotation so the
        agent can tell an error injected by this tool from transcript content.
        """
        try:
            return self.handler(dict(arguments or {}))
        except Exception as exc:  # noqa: BLE001 - a tool error is a result
            return f"[[ossuary:tool-error {self.name}: {type(exc).__name__}: {exc}]]"


@dataclass
class AgentRunResult:
    """What a backend reports back. Findings themselves arrive through tools."""

    text: str = ""
    turns: int = 0
    hit_turn_cap: bool = False


@dataclass
class BackendConfig:
    """The `agents.yaml` knobs, minus the prompt."""

    model: str
    temperature: float = 0.0
    max_turns: int = 15
    max_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AgentBackend(ABC):
    """Runs one tool-using turn loop to completion."""

    name: ClassVar[str] = "backend"

    #: Backends that drive a coding-agent harness rather than the raw Messages
    #: API cannot set sampling parameters. Reported so `ossuary agents show` can
    #: say so out loud instead of silently ignoring a configured temperature.
    supports_temperature: ClassVar[bool] = True

    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    @property
    def model(self) -> str:
        return self.config.model

    @abstractmethod
    def run(
        self,
        *,
        instructions: str,
        prompt: str,
        tools: Sequence[ToolSpec],
    ) -> AgentRunResult:
        """Run to completion or to the turn cap. Must not raise on a cap hit."""

    def describe(self) -> str:
        return f"{self.name}:{self.model or '(default)'}"


def object_schema(properties: dict[str, Any], required: Sequence[str]) -> dict[str, Any]:
    """A JSON Schema object, spelled the way every backend accepts it."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def as_text(value: Any) -> str:
    """Coerce a tool return value to the text a model will see."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


__all__ = [
    "AgentBackend",
    "AgentRunResult",
    "BackendConfig",
    "BackendUnavailable",
    "ToolSpec",
    "as_text",
    "object_schema",
]
