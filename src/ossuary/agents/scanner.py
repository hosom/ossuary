"""Agent A -- the session investigator.

One run per session. A tool-using loop with a hard turn cap, emitting `Issue`s.

Three design points that are easy to undo by accident:

  * The full `session_outline` is in context from turn one. The agent does not
    choose whether to see every event -- it always has, at low resolution. That
    is what makes recall independent of the model's curiosity and runs
    reproducible enough to diff week over week.
  * Issues are reported incrementally through the `report_issue` tool rather than
    returned in a final structured payload. If the turn cap cuts the run short,
    whatever was found up to that point survives.
  * The tools and the prompt are backend-independent. Which credential pays for
    the reasoning is a deployment question; it must not change what the agent
    can see or is told.

The prompt lives in `agents.yaml`. Nothing here describes what a problem looks
like, and that omission is deliberate: handing the model a taxonomy of known
failure modes would turn discovery into recognition.
"""

from __future__ import annotations

from ..backends import AgentBackend, build_backend
from ..config import AgentConfig
from .tools import MAX_EVENT_SPAN, scanner_tools

__all__ = ["MAX_EVENT_SPAN", "build_scanner_backend", "scanner_prompt", "scanner_tools"]


def build_scanner_backend(config: AgentConfig) -> AgentBackend:
    """Construct Agent A's backend. Does not require credentials."""
    return build_backend(
        config.model,
        temperature=config.temperature,
        max_turns=config.max_turns,
        max_tokens=config.max_tokens,
        extra=config.extra,
    )


def scanner_prompt(outline: str) -> str:
    return (
        f"Investigate this session.\n\n{outline}\n\n"
        f"Call report_issue as soon as you find each issue, then continue. "
        f"When you have accounted for the whole outline, reply with a one-line "
        f"summary of what you found."
    )
