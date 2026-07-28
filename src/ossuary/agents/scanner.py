"""Agent A -- the session investigator.

One run per session. A tool-using loop with a hard turn cap, emitting `Issue`s.

Two design points that are easy to undo by accident:

  * The full `session_outline` is in context from turn one. The agent does not
    choose whether to see every event -- it always has, at low resolution. That
    is what makes recall independent of the model's curiosity and runs
    reproducible enough to diff week over week.
  * Issues are reported incrementally through the `report_issue` tool rather than
    returned in a final structured payload. If the turn cap cuts the run short,
    whatever was found up to that point survives.

The prompt lives in `agents.yaml`. Nothing here describes what a problem looks
like, and that omission is deliberate: handing the model a taxonomy of known
failure modes would turn discovery into recognition.
"""

from __future__ import annotations

from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.settings import ModelSettings

from ..config import AgentConfig
from ..models import Issue, Phase, Severity
from ..store import DEFAULT_EVENT_BUDGET
from .deps import ScannerDeps
from .models import resolve_model

# A single `read_events` call should not be able to consume the whole window.
MAX_EVENT_SPAN = 40


def build_scanner_agent(config: AgentConfig) -> Agent[ScannerDeps, str]:
    agent: Agent[ScannerDeps, str] = Agent(
        resolve_model(config.model),
        deps_type=ScannerDeps,
        output_type=str,
        instructions=config.prompt,
        model_settings=ModelSettings(
            temperature=config.temperature,
            **({"max_tokens": config.max_tokens} if config.max_tokens else {}),
        ),
        retries=2,
        # Resolve the provider at run time, not construction time. Building the
        # agent must not require credentials: `ossuary agents test` runs it
        # against a stub model, and a missing key should surface when a real
        # call is actually attempted.
        defer_model_check=True,
    )

    @agent.tool
    def read_events(ctx: RunContext[ScannerDeps], start: int, end: int) -> str:
        """Read full events from the session by index range.

        Args:
            start: First event index to read, inclusive.
            end: Last event index, exclusive. At most 40 events per call.
        """
        deps = ctx.deps
        span_end = min(end, start + MAX_EVENT_SPAN)
        args = {"start": start, "end": span_end}
        note = ""
        if end > span_end:
            note = (
                f"\n\n[[ossuary:elided events {span_end}..{end}; "
                f"at most {MAX_EVENT_SPAN} events per call -- "
                f"call read_events again from {span_end} to continue]]"
            )
        body = deps.cached_or(
            "read_events",
            args,
            lambda: deps.store.read_events(
                deps.session_id, start, span_end, per_event_budget=DEFAULT_EVENT_BUDGET
            ),
        )
        return body + note

    @agent.tool
    def search_session(ctx: RunContext[ScannerDeps], pattern: str) -> str:
        """Search this session's text with a regular expression.

        Args:
            pattern: A Python regular expression. Returns matching event indices
                with surrounding context.
        """
        deps = ctx.deps
        return deps.cached_or(
            "search_session",
            {"pattern": pattern},
            lambda: deps.store.search_session(deps.session_id, pattern),
        )

    @agent.tool
    def read_event_slice(
        ctx: RunContext[ScannerDeps], event_index: int, offset: int = 0, limit: int = 8000
    ) -> str:
        """Read one oversized payload a page at a time.

        Args:
            event_index: Index of the event whose payload to read.
            offset: Byte offset to start from.
            limit: Maximum bytes to return in this page.
        """
        deps = ctx.deps
        args = {"event_index": event_index, "offset": offset, "limit": limit}
        return deps.cached_or(
            "read_event_slice",
            args,
            lambda: deps.store.read_event_slice(
                deps.session_id, event_index, offset, limit
            ),
        )

    @agent.tool
    def tool_stats(ctx: RunContext[ScannerDeps], tool_name: str) -> str:
        """Corpus-wide statistics for one tool, across every scanned session.

        Use this to check whether something odd in this session is odd
        everywhere, or normal for this tool.

        Args:
            tool_name: Exact tool name as it appears in the outline.
        """
        return ctx.deps.stats_for(tool_name)

    @agent.tool
    def report_issue(
        ctx: RunContext[ScannerDeps],
        title: str,
        description: str,
        severity: Severity,
        phase: Phase,
        evidence_event_indices: list[int],
        confidence: float,
    ) -> str:
        """Record one issue you have found. Call this as soon as you find it.

        Args:
            title: Short description in your own words.
            description: What went wrong, what the evidence shows, and why it matters.
            severity: One of "low", "medium", "high".
            phase: Where the problem originates -- "prompt", "tool", "model",
                "harness", "user", or "unknown".
            evidence_event_indices: Event indices a reader should look at to
                verify this.
            confidence: 0.0 to 1.0.
        """
        issue = Issue(
            title=title.strip(),
            description=description.strip(),
            severity=severity,
            phase=phase,
            evidence_event_indices=sorted(set(evidence_event_indices)),
            confidence=max(0.0, min(1.0, confidence)),
        )
        ctx.deps.collected.append(issue)
        return (
            f"Recorded issue {len(ctx.deps.collected)}: {issue.title!r}. "
            f"Continue investigating, or finish if you have covered the outline."
        )

    return agent


def scanner_usage_limits(config: AgentConfig) -> UsageLimits:
    """Turn cap.

    `request_limit` counts model requests, which is the turn count for a
    tool-using loop -- one request per turn plus the final answer.
    """
    return UsageLimits(request_limit=config.max_turns)
