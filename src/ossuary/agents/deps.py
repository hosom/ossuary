"""State the agent tools read and write.

Closed over by `agents.tools`, which is the only place these are touched. They
carry no backend types on purpose: the same deps drive a run whether inference
came from an API key, a Claude Code subscription, or a Copilot subscription.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..aggregate import render_tool_stats
from ..cache import Cache
from ..models import Issue, StoredIssue, ToolStats
from ..store import SessionStore

if TYPE_CHECKING:
    from .clusterer import ProposedCluster


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
    """Agent B receives its material in the prompt and reports through one tool.

    `collected` is the same incremental sink as Agent A's: clusters named before
    a run stops early are kept rather than lost with the final payload.
    """

    issues: list[StoredIssue] = field(default_factory=list)
    tool_stats: list[ToolStats] = field(default_factory=list)
    collected: list["ProposedCluster"] = field(default_factory=list)
