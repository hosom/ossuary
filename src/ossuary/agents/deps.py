"""Dependencies injected into agent tools via Pydantic AI's `deps_type`."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..aggregate import render_tool_stats
from ..cache import Cache
from ..models import Issue, StoredIssue, ToolStats
from ..store import SessionStore


@dataclass
class ScannerDeps:
    """What Agent A's tools need.

    Carries the session store rather than a session, so `tool_stats` can answer
    corpus-wide questions while the read tools stay scoped to one transcript.
    `collected` is the incremental sink: the agent reports issues as it finds
    them, so a turn-cap cutoff still yields partial results instead of nothing.
    """

    store: SessionStore
    session_id: str
    session_content_hash: str
    tool_stats: list[ToolStats] = field(default_factory=list)
    cache: Cache | None = None
    collected: list[Issue] = field(default_factory=list)
    tool_calls_made: int = 0

    def stats_for(self, tool_name: str) -> str:
        matches = [s for s in self.tool_stats if s.tool_name == tool_name]
        if not matches:
            known = ", ".join(sorted({s.tool_name for s in self.tool_stats})[:25])
            return (
                f"No corpus statistics for tool {tool_name!r}. "
                f"Tools seen in this corpus: {known or 'none'}"
            )
        return render_tool_stats(matches)

    def cached_or(self, tool_name: str, args: dict, produce) -> str:  # type: ignore[no-untyped-def]
        """Serve a tool response from cache when the session file is unchanged."""
        self.tool_calls_made += 1
        if self.cache is None:
            return produce()
        hit = self.cache.get_tool_response(self.session_content_hash, tool_name, args)
        if hit is not None:
            return hit
        value = produce()
        self.cache.set_tool_response(self.session_content_hash, tool_name, args, value)
        return value


@dataclass
class ClustererDeps:
    """Agent B needs no tools; it receives its material in the prompt."""

    issues: list[StoredIssue] = field(default_factory=list)
    tool_stats: list[ToolStats] = field(default_factory=list)
