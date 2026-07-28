"""The tools both agents are given, described once for every backend.

Tool schemas live here rather than in a backend so that switching where
inference runs cannot change what the agent is able to see. A finding produced
against a Claude Max subscription and the same finding produced against an API
key differ only in who did the reasoning.

Descriptions carry the argument documentation because that is the only channel
some backends expose -- do not shorten them on the assumption the schema speaks
for itself.
"""

from __future__ import annotations

from ..backends import ToolSpec, object_schema
from ..models import Issue
from ..store import DEFAULT_EVENT_BUDGET
from .deps import ClustererDeps, ScannerDeps

# A single `read_events` call should not be able to consume the whole window.
MAX_EVENT_SPAN = 40

SEVERITIES = ["low", "medium", "high"]
PHASES = ["prompt", "tool", "model", "harness", "user", "unknown"]


def scanner_tools(deps: ScannerDeps) -> list[ToolSpec]:
    """Agent A's toolset: four ways to read one session, one way to report."""

    def read_events(args: dict) -> str:
        start = int(args["start"])
        end = int(args["end"])
        span_end = min(end, start + MAX_EVENT_SPAN)
        note = ""
        if end > span_end:
            note = (
                f"\n\n[[ossuary:elided events {span_end}..{end}; "
                f"at most {MAX_EVENT_SPAN} events per call -- "
                f"call read_events again from {span_end} to continue]]"
            )
        body = deps.cached_or(
            "read_events",
            {"start": start, "end": span_end},
            lambda: deps.store.read_events(
                deps.session_id, start, span_end, per_event_budget=DEFAULT_EVENT_BUDGET
            ),
        )
        return body + note

    def search_session(args: dict) -> str:
        pattern = str(args["pattern"])
        return deps.cached_or(
            "search_session",
            {"pattern": pattern},
            lambda: deps.store.search_session(deps.session_id, pattern),
        )

    def read_event_slice(args: dict) -> str:
        event_index = int(args["event_index"])
        offset = int(args.get("offset", 0))
        limit = int(args.get("limit", 8000))
        return deps.cached_or(
            "read_event_slice",
            {"event_index": event_index, "offset": offset, "limit": limit},
            lambda: deps.store.read_event_slice(
                deps.session_id, event_index, offset, limit
            ),
        )

    def tool_stats(args: dict) -> str:
        return deps.stats_for(str(args["tool_name"]))

    def report_issue(args: dict) -> str:
        indices = args.get("evidence_event_indices") or []
        issue = Issue(
            title=str(args["title"]).strip(),
            description=str(args["description"]).strip(),
            severity=str(args["severity"]),  # type: ignore[arg-type]
            phase=str(args["phase"]),  # type: ignore[arg-type]
            evidence_event_indices=sorted({int(i) for i in indices}),
            confidence=max(0.0, min(1.0, float(args.get("confidence", 0.5)))),
        )
        deps.collected.append(issue)
        return (
            f"Recorded issue {len(deps.collected)}: {issue.title!r}. "
            f"Continue investigating, or finish if you have covered the outline."
        )

    return [
        ToolSpec(
            name="read_events",
            description=(
                "Read full events from the session by index range. "
                "start: first event index, inclusive. "
                f"end: last event index, exclusive; at most {MAX_EVENT_SPAN} events per call."
            ),
            parameters=object_schema(
                {
                    "start": {"type": "integer", "description": "First event index, inclusive."},
                    "end": {"type": "integer", "description": "Last event index, exclusive."},
                },
                ["start", "end"],
            ),
            handler=read_events,
        ),
        ToolSpec(
            name="search_session",
            description=(
                "Search this session's text with a Python regular expression. "
                "Returns matching event indices with surrounding context."
            ),
            parameters=object_schema(
                {"pattern": {"type": "string", "description": "A Python regular expression."}},
                ["pattern"],
            ),
            handler=search_session,
        ),
        ToolSpec(
            name="read_event_slice",
            description=(
                "Read one oversized payload a page at a time. "
                "event_index: index of the event whose payload to read. "
                "offset: byte offset to start from. "
                "limit: maximum bytes to return in this page."
            ),
            parameters=object_schema(
                {
                    "event_index": {"type": "integer"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 8000},
                },
                ["event_index"],
            ),
            handler=read_event_slice,
        ),
        ToolSpec(
            name="tool_stats",
            description=(
                "Corpus-wide statistics for one tool, across every scanned session. "
                "Use this to check whether something odd in this session is odd "
                "everywhere, or normal for this tool. "
                "tool_name: exact tool name as it appears in the outline."
            ),
            parameters=object_schema(
                {"tool_name": {"type": "string"}},
                ["tool_name"],
            ),
            handler=tool_stats,
        ),
        ToolSpec(
            name="report_issue",
            description=(
                "Record one issue you have found. Call this as soon as you find it. "
                "title: short description in your own words. "
                "description: what went wrong, what the evidence shows, and why it matters. "
                "severity: low, medium, or high. "
                "phase: where the problem originates -- prompt, tool, model, harness, user, or unknown. "
                "evidence_event_indices: event indices a reader should look at to verify this. "
                "confidence: 0.0 to 1.0."
            ),
            parameters=object_schema(
                {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "phase": {"type": "string", "enum": PHASES},
                    "evidence_event_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                ["title", "description", "severity", "phase", "evidence_event_indices", "confidence"],
            ),
            handler=report_issue,
        ),
    ]


def clusterer_tools(deps: ClustererDeps) -> list[ToolSpec]:
    """Agent B's single tool.

    Clusters are reported one at a time rather than returned as a final payload,
    for the same reason Agent A reports issues incrementally: a run that stops
    early should still yield the clusters it had already named.
    """

    def propose_cluster(args: dict) -> str:
        from .clusterer import ProposedCluster

        existing = args.get("existing_cluster_id")
        cluster = ProposedCluster(
            name=str(args["name"]).strip(),
            summary=str(args["summary"]).strip(),
            member_issue_ids=[str(i) for i in (args.get("member_issue_ids") or [])],
            existing_cluster_id=str(existing) if existing else None,
        )
        deps.collected.append(cluster)
        return (
            f"Recorded cluster {len(deps.collected)}: {cluster.name!r} with "
            f"{len(cluster.member_issue_ids)} issue(s). Continue with the next "
            f"cluster, or finish when every issue_id has been placed."
        )

    return [
        ToolSpec(
            name="propose_cluster",
            description=(
                "Record one cluster. Call this once per failure mode, as soon as you "
                "have decided on it. "
                "name: short human-readable name for this failure mode. "
                "summary: what these issues have in common and why it matters. "
                "member_issue_ids: the issue_id values belonging to this cluster. "
                "existing_cluster_id: if this matches a cluster from the stored "
                "taxonomy, its cluster_id; omit it if this is a genuinely new failure mode."
            ),
            parameters=object_schema(
                {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "member_issue_ids": {"type": "array", "items": {"type": "string"}},
                    "existing_cluster_id": {"type": "string"},
                },
                ["name", "summary", "member_issue_ids"],
            ),
            handler=propose_cluster,
        ),
    ]


__all__ = ["MAX_EVENT_SPAN", "PHASES", "SEVERITIES", "clusterer_tools", "scanner_tools"]
