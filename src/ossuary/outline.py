"""Session outline -- one row per event, always in Agent A's context.

This is the coverage guarantee. The agent has seen *every* event at low
resolution before it decides where to look closely, so recall does not depend on
the model's curiosity and runs are reproducible enough to diff week over week.

It is also doing most of the analytical work. Anomalies surface here as *rows*
rather than as text buried in a payload: identical byte counts across repeated
calls line up in a column, a suspiciously round 30000 stands out against its
neighbours, a long duration next to an empty body is one glance.

The row format is tuned to a budget of roughly 20 tokens, so a 400-event session
outlines in about 8k tokens. That budget is why roles and kinds are abbreviated
and why the preview is short: every character here is paid for on every event of
every session in the corpus.
"""

from __future__ import annotations

from .models import NormalizedEvent, Session

# Abbreviations exist purely to hold the row budget. Expanded in the legend so
# the agent never has to guess.
_ROLE_ABBREV = {
    "user": "usr",
    "assistant": "ast",
    "system": "sys",
    "unknown": "unk",
}
_KIND_ABBREV = {
    "message": "msg",
    "tool_call": "call",
    "tool_result": "rslt",
    "thinking": "think",
    "meta": "meta",
    "unparseable": "BAD",
}

# Stop reasons that mean the turn did not finish the way it meant to, in the
# spellings the supported CLIs use. Recorded by the harness, not inferred here:
# a turn that ended at `error` or was cut off at the token limit is a fact about
# the run, and one that is otherwise invisible because a failed turn often has
# no text and no payload to measure.
_ABNORMAL_STOP_REASONS = {"error", "aborted", "length", "max_tokens"}

_HEADER = "idx  time     role kind  tool          bytes    dur exit fl preview"
_RULE = "-" * 100
_PREVIEW_CHARS = 30
_TOOL_WIDTH = 12


def render_outline(session: Session, *, preview_chars: int = _PREVIEW_CHARS) -> str:
    lines = [
        f"SESSION {session.session_id}  source={session.source}  "
        f"events={len(session.events)}",
    ]
    if session.project:
        lines.append(f"project: {session.project}")
    if session.parse_error_count:
        lines.append(
            f"NOTE: {session.parse_error_count} line(s) failed to parse and appear "
            f"below as kind=BAD. Their raw text is readable via read_events."
        )
    lines.append("")
    lines.append(_HEADER)
    lines.append(_RULE)
    for event in session.events:
        lines.append(_render_row(event, preview_chars=preview_chars))
    lines.append(_RULE)
    lines.append(_legend(session))
    return "\n".join(lines)


def _render_row(event: NormalizedEvent, *, preview_chars: int) -> str:
    shape = event.shape
    time_text = event.ts.strftime("%H:%M:%S") if event.ts else "--------"
    tool = (event.tool_name or "")[:_TOOL_WIDTH]

    if shape is None:
        byte_text = str(len(event.text.encode("utf-8", errors="surrogatepass")))
        duration_text = ""
        exit_text = ""
    else:
        byte_text = str(shape.byte_length)
        duration_text = "" if shape.duration_ms is None else _fmt_ms(shape.duration_ms)
        exit_text = "" if shape.exit_code is None else str(shape.exit_code)

    return (
        f"{event.index:<4} {time_text} "
        f"{_ROLE_ABBREV.get(event.role, 'unk'):<4} "
        f"{_KIND_ABBREV.get(event.kind, event.kind):<5} "
        f"{tool:<{_TOOL_WIDTH}} "
        f"{byte_text:>6} {duration_text:>6} {exit_text:>4} "
        f"{_flags(event):<2} {_preview(event, preview_chars)}"
    )


def _fmt_ms(duration_ms: int) -> str:
    """Compact duration. Seconds above 10s, where the exact millisecond is noise."""
    if duration_ms >= 10_000:
        return f"{duration_ms / 1000:.0f}s"
    return f"{duration_ms}ms"


def _flags(event: NormalizedEvent) -> str:
    """Compact per-row markers. Observations only -- never verdicts."""
    flags: list[str] = []
    shape = event.shape
    if shape is not None:
        if shape.is_empty:
            flags.append("E")
        if not shape.terminates_cleanly:
            flags.append("T")
        if shape.is_round_number:
            flags.append("R")
        if shape.has_error_field:
            flags.append("X")
        if shape.duration_source == "derived":
            flags.append("~")
    if event.parse_error:
        flags.append("P")
    if event.meta.get("orphan_result"):
        flags.append("O")
    if event.meta.get("thinking_signature_only"):
        flags.append("S")
    if event.meta.get("off_path"):
        flags.append("B")
    if event.meta.get("stop_reason") in _ABNORMAL_STOP_REASONS:
        flags.append("F")
    return "".join(flags)


def _preview(event: NormalizedEvent, preview_chars: int) -> str:
    """A short single-line preview.

    Deliberately *not* routed through the elision marker machinery: this is a
    per-row index entry, not a payload, and the legend states plainly that every
    preview is cut for display. The marker invariant applies to payload text the
    agent reads through the tools, where an unmarked short read would be
    indistinguishable from a genuinely short result.
    """
    source = event.text if event.text else (event.raw or "")
    collapsed = " ".join(source.split())
    if len(collapsed) <= preview_chars:
        return collapsed
    return collapsed[: preview_chars - 1] + "…"


def _legend(session: Session) -> str:
    derived = any(
        e.shape is not None and e.shape.duration_source == "derived"
        for e in session.events
    )
    lines = [
        "role: usr=user ast=assistant sys=system unk=unknown",
        "kind: msg=message call=tool_call rslt=tool_result think=thinking "
        "meta=harness bookkeeping BAD=line failed to parse",
        "fl:   E=empty body  T=does not terminate cleanly  R=round byte count  "
        "X=error field set  P=parse error  O=result with no matching call  "
        "S=thinking stored with a signature but no text  ~=duration derived  "
        "B=on a branch the session moved off  F=turn ended abnormally",
        "time is UTC.",
        "The preview column is a fixed-width index entry cut for display, not "
        "payload text; use read_events to see any event in full.",
    ]
    if derived:
        lines.append(
            "Durations marked ~ are wall-clock gaps between the call and its "
            "result line, because this CLI does not record a duration for these "
            "tools. They include any time the harness spent elsewhere."
        )
    if any(e.meta.get("stop_reason") in _ABNORMAL_STOP_REASONS for e in session.events):
        lines.append(
            "Rows marked F are turns the CLI recorded as ending abnormally -- an "
            "error, an abort, or a turn cut off at the token limit. Such a turn "
            "often has no text at all, so without the flag it reads as a turn "
            "that simply produced nothing. Where the CLI also recorded an error "
            "message it is on the next row, in the CLI's own words."
        )
    if any(e.meta.get("off_path") for e in session.events):
        lines.append(
            "Rows marked B are on a branch this session moved off: someone "
            "rewound to an earlier point and continued from there. Those events "
            "happened, and are still on disk, but are no longer part of the "
            "conversation the model sees. Rows are in file order either way."
        )
    return "\n".join(lines)
