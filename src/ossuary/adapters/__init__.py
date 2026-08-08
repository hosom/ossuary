"""Adapter registry."""

from __future__ import annotations

from pathlib import Path

from ..models import Source
from .base import Adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .copilot import CopilotAdapter
from .pi import PiAdapter

_ADAPTERS: dict[str, type[Adapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "copilot": CopilotAdapter,
    "pi": PiAdapter,
}

ALL_SOURCES: tuple[str, ...] = tuple(_ADAPTERS)


def get_adapter(source: str, roots: list[Path] | None = None) -> Adapter:
    try:
        cls = _ADAPTERS[source]
    except KeyError:
        raise ValueError(
            f"unknown source {source!r}; expected one of {', '.join(ALL_SOURCES)}"
        ) from None
    return cls(roots=roots)  # type: ignore[call-arg]


def all_adapters(roots: list[Path] | None = None) -> list[Adapter]:
    return [cls(roots=roots) for cls in _ADAPTERS.values()]  # type: ignore[call-arg]


__all__ = [
    "Adapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "PiAdapter",
    "ALL_SOURCES",
    "get_adapter",
    "all_adapters",
]
