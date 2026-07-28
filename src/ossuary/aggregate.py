"""Corpus-wide tool statistics.

A meaningful share of tool-layer pathology is invisible within any single
session. "This MCP server returns exactly 30000 bytes on 40% of calls" is
unremarkable once and damning across two hundred sessions. Agent A structurally
cannot see it -- it only ever looks at one transcript. Agent B can, but only if
these numbers are computed and handed over.

Note what this module does *not* do: it never labels a tool as broken. It
counts, measures, and ranks. The judgement stays with the agent.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .models import Session, ToolStats

UNKNOWN_TOOL = "<unknown>"


def compute_tool_stats(sessions: list[Session]) -> list[ToolStats]:
    """Per-tool aggregates over every `tool_result` in the corpus."""
    byte_lengths: dict[str, list[int]] = defaultdict(list)
    durations: dict[str, list[int]] = defaultdict(list)
    hashes: dict[str, Counter[str]] = defaultdict(Counter)
    sessions_seen: dict[str, set[str]] = defaultdict(set)
    calls: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    empties: Counter[str] = Counter()
    rounds: Counter[str] = Counter()
    truncated: Counter[str] = Counter()

    for session in sessions:
        for event in session.events:
            if event.kind != "tool_result" or event.shape is None:
                continue
            # Results whose call never appeared are bucketed explicitly rather
            # than attributed to a neighbouring tool, which would quietly
            # corrupt exactly the statistics Agent B reasons about.
            name = event.tool_name or UNKNOWN_TOOL
            shape = event.shape

            calls[name] += 1
            sessions_seen[name].add(session.session_id)
            byte_lengths[name].append(shape.byte_length)
            hashes[name][shape.content_hash] += 1
            if shape.duration_ms is not None:
                durations[name].append(shape.duration_ms)
            if shape.has_error_field:
                errors[name] += 1
            if shape.is_empty:
                empties[name] += 1
            if shape.is_round_number:
                rounds[name] += 1
            if not shape.terminates_cleanly:
                truncated[name] += 1

    from .shape import percentile

    stats: list[ToolStats] = []
    for name, count in calls.items():
        lengths = byte_lengths[name]
        durs = durations[name]
        hash_counts = hashes[name]
        # Every result beyond the first sharing a hash is a repeat.
        duplicates = sum(n - 1 for n in hash_counts.values() if n > 1)

        stats.append(
            ToolStats(
                tool_name=name,
                call_count=count,
                session_count=len(sessions_seen[name]),
                error_count=errors[name],
                empty_count=empties[name],
                round_number_count=rounds[name],
                truncated_looking_count=truncated[name],
                duplicate_result_count=duplicates,
                distinct_result_hashes=len(hash_counts),
                byte_length_min=min(lengths) if lengths else 0,
                byte_length_max=max(lengths) if lengths else 0,
                byte_length_p50=percentile(lengths, 50),
                byte_length_p95=percentile(lengths, 95),
                duration_ms_p50=percentile(durs, 50) if durs else None,
                duration_ms_p95=percentile(durs, 95) if durs else None,
                duration_ms_max=max(durs) if durs else None,
                top_byte_lengths=Counter(lengths).most_common(5),
            )
        )

    stats.sort(key=lambda s: (-s.call_count, s.tool_name))
    return stats


def render_tool_stats(stats: list[ToolStats], *, limit: int | None = None) -> str:
    """Plain-text rendering handed to Agent B and to Agent A's `tool_stats` tool."""
    if not stats:
        return "No tool results in the corpus."

    selected = stats[:limit] if limit else stats
    lines = [
        f"CORPUS TOOL STATISTICS ({len(stats)} tool(s))",
        "",
    ]
    for stat in selected:
        lines.append(f"tool: {stat.tool_name}")
        lines.append(
            f"  calls={stat.call_count} across {stat.session_count} session(s)"
        )
        lines.append(
            f"  errors={stat.error_count} ({stat.error_rate:.1%})  "
            f"empty={stat.empty_count}  "
            f"round_byte_counts={stat.round_number_count}  "
            f"not_terminating_cleanly={stat.truncated_looking_count}"
        )
        lines.append(
            f"  duplicate_results={stat.duplicate_result_count} "
            f"({stat.duplicate_rate:.1%}) over {stat.distinct_result_hashes} "
            f"distinct payload(s)"
        )
        lines.append(
            f"  bytes: min={stat.byte_length_min} p50={stat.byte_length_p50} "
            f"p95={stat.byte_length_p95} max={stat.byte_length_max}"
        )
        if stat.top_byte_lengths:
            common = "  ".join(
                f"{length}B x{n}" for length, n in stat.top_byte_lengths
            )
            lines.append(f"  most common byte lengths: {common}")
        if stat.duration_ms_p50 is not None:
            lines.append(
                f"  duration ms: p50={stat.duration_ms_p50} "
                f"p95={stat.duration_ms_p95} max={stat.duration_ms_max}"
            )
        lines.append("")

    if limit and len(stats) > limit:
        lines.append(f"[[ossuary:elided {len(stats) - limit} of {len(stats)} tools]]")
    return "\n".join(lines)


def corpus_event_count(sessions: list[Session]) -> int:
    return sum(len(s.events) for s in sessions)
