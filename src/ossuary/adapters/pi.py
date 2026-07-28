"""pi transcript adapter.

Layout (see `docs/formats.md`):

    ~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl

Written against pi's own sources and its shipped `docs/session-format.md`, and
against fixture files produced by pi's own `SessionManager` -- not against a real
conversation, because no pi install existed on the machine this was written on.
`docs/pi-investigation.md` records what that means.

Three things make pi different from the other three formats:

  * **Sessions are trees, not lines.** `/tree` and `/rewind` move the leaf back
    to an earlier entry and append from there, so file order is not conversation
    order and some entries on disk are on no path at all. Every entry is emitted
    in file order regardless -- the abandoned ones are the record of an approach
    that was tried and thrown away, which is exactly the failure class this tool
    exists to cluster. They carry `off_path` in `meta` and a `B` flag in the
    outline, so the agent can tell the live conversation from the wreckage.
  * **pi reports its own truncation, numerically.** Tool output is cut at 2000
    lines or 51200 bytes, the payload carries a human-readable marker, and
    `details.truncation` holds the exact totals. Nothing else supported does
    this, so the numbers are carried into `meta` verbatim: "this payload looks
    capped" becomes "this payload was capped, from 900000 bytes, by the byte
    limit".
  * **The result names its own tool.** `toolResult.toolName` is recorded, so
    unlike Claude Code the name does not depend on the join. The join still runs,
    to mark results whose call never appears.

On-disk versions: pi migrates old sessions to v3 *and rewrites the file* when it
opens them, so anything a recent pi has touched is v3. Files it has not touched
can still be v1 (linear, no `id`/`parentId`) or v2 (the extension message role is
spelled `hookMessage`). Both are read here; a missing `id` means linear, and a
linear session is entirely on its own path.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import NormalizedEvent, Session, SessionRef
from ..shape import compute_shape
from .base import Adapter, unparseable_event

# Entry types that are harness bookkeeping rather than conversation. Kept as
# `meta` events: a compaction that dropped 50000 tokens, or a branch summary
# recording an abandoned approach, is a health signal, not noise.
_META_ENTRY_TYPES = {
    "model_change",
    "thinking_level_change",
    "compaction",
    "branch_summary",
    "custom",
    "label",
    "session_info",
}

# Message roles that are summaries pi wrote about the conversation rather than
# turns within it.
_SUMMARY_ROLES = {"branchSummary", "compactionSummary"}

# The user's own `!command`, recorded as a message rather than a tool call. Named
# apart from the model's `bash` tool so corpus statistics do not merge the two.
_USER_BASH = "user_bash"


class PiAdapter(Adapter):
    source = "pi"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots

    # -- discovery ------------------------------------------------------

    def default_roots(self) -> list[Path]:
        """Where pi keeps sessions, honouring its two environment overrides.

        pi derives the variable names from its own package name at runtime, so a
        rebranded build reads `TAU_CODING_AGENT_DIR` and stores under `~/.tau`.
        Only the `PI_` spelling is followed here; chasing rebrands would mean
        guessing at names that do not exist on this machine.
        """
        session_dir = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        if session_dir:
            return [Path(session_dir).expanduser()]
        agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
        base = Path(agent_dir).expanduser() if agent_dir else Path.home() / ".pi" / "agent"
        return [base / "sessions"]

    def claims(self, path: Path) -> bool:
        """A pi session opens with a `{"type": "session", ...}` header line."""
        return self._header(path) is not None

    def _header(self, path: Path) -> dict[str, Any] | None:
        for record in self.head_records(path):
            if record.get("type") == "session" and isinstance(record.get("id"), str):
                return record
        return None

    def discover(
        self, roots: list[Path] | None = None, *, require_claim: bool = True
    ) -> list[SessionRef]:
        search = roots or self._roots or self.default_roots()
        refs: list[SessionRef] = []
        seen: set[Path] = set()

        for root in search:
            root = Path(root).expanduser()
            if not root.exists():
                continue
            paths = [root] if root.is_file() else sorted(root.rglob("*.jsonl"))
            for path in paths:
                resolved = path.resolve()
                if resolved in seen or not path.is_file():
                    continue
                seen.add(resolved)
                header = self._header(path)
                if require_claim and header is None:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                # The header id is what `parse` will report, so reading it here
                # keeps the discovered id and the parsed id the same string.
                # Without a readable header the filename is the best available
                # name: `<timestamp>_<uuid>`.
                session_id = str(header["id"]) if header else _id_from_name(path)
                cwd = header.get("cwd") if header else None
                refs.append(
                    SessionRef(
                        session_id=session_id,
                        source="pi",
                        path=str(path),
                        size_bytes=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        project=cwd if isinstance(cwd, str) and cwd else path.parent.name,
                    )
                )
        return refs

    # -- parsing --------------------------------------------------------

    def parse(self, ref: SessionRef) -> Session:
        path = Path(ref.path)

        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            session = Session(
                session_id=ref.session_id,
                source="pi",
                path=str(path),
                project=ref.project,
            )
            session.events.append(
                unparseable_event(
                    session_id=ref.session_id,
                    source="pi",
                    index=0,
                    raw="",
                    error=f"unreadable file: {exc}",
                )
            )
            session.parse_error_count = 1
            return session

        records = _read_records(raw_lines)
        session_id, project = _identity(records, ref)
        on_path = _active_path(records)

        events: list[NormalizedEvent] = []
        parse_errors = 0
        # toolCallId -> (event index, timestamp), so a result can be paired with
        # its call however far apart they landed.
        pending_calls: dict[str, tuple[int, datetime | None]] = {}

        for line_no, line, record, error in records:
            if record is None:
                events.append(
                    unparseable_event(
                        session_id=session_id,
                        source="pi",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no}: {error}",
                    )
                )
                parse_errors += 1
                continue

            try:
                produced = self._events_for_entry(
                    record,
                    session_id=session_id,
                    line=line,
                    line_no=line_no,
                    next_index=len(events),
                    on_path=on_path,
                    pending_calls=pending_calls,
                )
            except Exception as exc:  # noqa: BLE001 - never lose a line
                events.append(
                    unparseable_event(
                        session_id=session_id,
                        source="pi",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no}: normalization failed: {exc!r}",
                    )
                )
                parse_errors += 1
                continue

            events.extend(produced)

        _mark_orphan_results(events)

        return Session(
            session_id=session_id,
            source="pi",
            path=str(path),
            events=events,
            content_hash=self.file_hash(path),
            parse_error_count=parse_errors,
            project=project,
        )

    # -- entry -> events -------------------------------------------------

    def _events_for_entry(
        self,
        record: dict[str, Any],
        *,
        session_id: str,
        line: str,
        line_no: int,
        next_index: int,
        on_path: set[str] | None,
        pending_calls: dict[str, tuple[int, datetime | None]],
    ) -> list[NormalizedEvent]:
        entry_type = str(record.get("type") or "unknown")
        entry_ts = self.parse_timestamp(record.get("timestamp"))
        meta = _entry_meta(record, entry_type, line_no, on_path)

        if entry_type == "session":
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=entry_ts,
                    role="system",
                    kind="meta",
                    text=_header_text(record),
                    meta={
                        **meta,
                        "session_version": record.get("version", 1),
                        **({"parent_session": record["parentSession"]}
                           if isinstance(record.get("parentSession"), str) else {}),
                    },
                )
            ]

        if entry_type in _META_ENTRY_TYPES:
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=entry_ts,
                    role="system",
                    kind="meta",
                    text=_meta_entry_text(entry_type, record),
                    meta={**meta, **_meta_entry_fields(entry_type, record)},
                )
            ]

        if entry_type == "custom_message":
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=entry_ts,
                    role="system",
                    kind="message",
                    text=_content_text(record.get("content")),
                    meta={
                        **meta,
                        "custom_type": record.get("customType"),
                        "display": record.get("display"),
                    },
                )
            ]

        if entry_type != "message":
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=entry_ts,
                    role="unknown",
                    kind="meta",
                    text=_stringify(record),
                    meta=meta,
                    parse_error=f"unrecognized entry type {entry_type!r}",
                )
            ]

        message = record.get("message")
        if not isinstance(message, dict):
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=entry_ts,
                    role="unknown",
                    kind="meta",
                    text="",
                    meta=meta,
                    raw=line,
                    parse_error="message entry with no message body",
                )
            ]

        # The message carries its own Unix-ms timestamp, written by the same
        # process microseconds from the entry's ISO one. The ms value is the
        # finer measure, so tool durations are derived from it where present.
        ts = self.parse_timestamp(message.get("timestamp")) or entry_ts
        role = str(message.get("role") or "unknown")

        if role == "user":
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=ts,
                    role="user",
                    kind="message",
                    text=_content_text(message.get("content")),
                    meta=meta,
                )
            ]

        if role == "assistant":
            return _assistant_events(
                message,
                session_id=session_id,
                ts=ts,
                next_index=next_index,
                meta=meta,
                pending_calls=pending_calls,
            )

        if role == "toolResult":
            return [
                _tool_result_event(
                    message,
                    session_id=session_id,
                    ts=ts,
                    index=next_index,
                    meta=meta,
                    pending_calls=pending_calls,
                )
            ]

        if role == "bashExecution":
            return _bash_execution_events(
                message,
                session_id=session_id,
                ts=ts,
                next_index=next_index,
                meta=meta,
            )

        if role in ("custom", "hookMessage"):
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=ts,
                    role="system",
                    kind="message",
                    text=_content_text(message.get("content")),
                    meta={
                        **meta,
                        "custom_type": message.get("customType"),
                        "display": message.get("display"),
                        # v2 files spell this role `hookMessage`; v3 renamed it.
                        **({"legacy_role": role} if role == "hookMessage" else {}),
                    },
                )
            ]

        if role in _SUMMARY_ROLES:
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=next_index,
                    ts=ts,
                    role="system",
                    kind="meta",
                    text=str(message.get("summary") or ""),
                    meta={
                        **meta,
                        "message_role": role,
                        **({"from_id": message["fromId"]}
                           if isinstance(message.get("fromId"), str) else {}),
                        **({"tokens_before": message["tokensBefore"]}
                           if isinstance(message.get("tokensBefore"), int) else {}),
                    },
                )
            ]

        return [
            NormalizedEvent(
                session_id=session_id,
                source="pi",
                index=next_index,
                ts=ts,
                role="unknown",
                kind="message",
                text=_content_text(message.get("content")),
                meta={**meta, "message_role": role},
                parse_error=f"unrecognized message role {role!r}",
            )
        ]


# -- file reading -------------------------------------------------------


def _read_records(
    raw_lines: list[str],
) -> list[tuple[int, str, dict[str, Any] | None, str | None]]:
    """One tuple per non-blank line: (line number, raw text, record, error).

    Read in full before any event is emitted, because the tree cannot be walked
    from a single line: which entries are on the live conversation is only
    knowable once the last one has been seen.
    """
    records: list[tuple[int, str, dict[str, Any] | None, str | None]] = []
    for line_no, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            records.append((line_no, line, None, str(exc)))
            continue
        if not isinstance(value, dict):
            records.append(
                (line_no, line, None, f"expected object, got {type(value).__name__}")
            )
            continue
        records.append((line_no, line, value, None))
    return records


def _identity(
    records: list[tuple[int, str, dict[str, Any] | None, str | None]],
    ref: SessionRef,
) -> tuple[str, str | None]:
    """Session id and project from the header, falling back to the ref."""
    for _, _, record, _ in records:
        if record is None or record.get("type") != "session":
            continue
        session_id = record.get("id")
        cwd = record.get("cwd")
        return (
            str(session_id) if isinstance(session_id, str) and session_id else ref.session_id,
            cwd if isinstance(cwd, str) and cwd else ref.project,
        )
    return ref.session_id, ref.project


def _active_path(
    records: list[tuple[int, str, dict[str, Any] | None, str | None]],
) -> set[str] | None:
    """Entry ids on the live conversation, or None when the file is linear.

    pi's leaf on load is the last entry in the file, so the live conversation is
    the walk from there to the root along `parentId`. Everything else is a branch
    the user moved away from. A v1 session has no ids at all and cannot branch,
    so it reports None and every entry counts as current.
    """
    parents: dict[str, str | None] = {}
    leaf: str | None = None
    for _, _, record, _ in records:
        if record is None or record.get("type") == "session":
            continue
        entry_id = record.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            continue
        parent = record.get("parentId")
        parents[entry_id] = parent if isinstance(parent, str) and parent else None
        leaf = entry_id

    if leaf is None:
        return None

    path: set[str] = set()
    cursor: str | None = leaf
    while cursor is not None and cursor not in path:
        path.add(cursor)
        cursor = parents.get(cursor)
    return path


# -- entry helpers ------------------------------------------------------


def _entry_meta(
    record: dict[str, Any],
    entry_type: str,
    line_no: int,
    on_path: set[str] | None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"entry_type": entry_type, "line_no": line_no}
    entry_id = record.get("id")
    if isinstance(entry_id, str) and entry_id:
        meta["entry_id"] = entry_id
    parent_id = record.get("parentId")
    if isinstance(parent_id, str) and parent_id:
        meta["parent_id"] = parent_id
    # Only recorded when true, and never for the header, which is metadata rather
    # than a node in the tree. Absence means "on the live conversation", which is
    # the common case and the one that does not need a flag.
    if (
        on_path is not None
        and entry_type != "session"
        and isinstance(entry_id, str)
        and entry_id
        and entry_id not in on_path
    ):
        meta["off_path"] = True
    return meta


def _header_text(record: dict[str, Any]) -> str:
    parts = [f"session v{record.get('version', 1)}"]
    if isinstance(record.get("cwd"), str):
        parts.append(f"cwd={record['cwd']}")
    if isinstance(record.get("parentSession"), str):
        parts.append(f"forked from {record['parentSession']}")
    return " ".join(parts)


def _meta_entry_text(entry_type: str, record: dict[str, Any]) -> str:
    if entry_type == "model_change":
        return f"model {record.get('provider')}/{record.get('modelId')}"
    if entry_type == "thinking_level_change":
        return f"thinking level {record.get('thinkingLevel')}"
    if entry_type in ("compaction", "branch_summary"):
        return str(record.get("summary") or "")
    if entry_type == "custom":
        return f"[{record.get('customType')}] {_stringify(record.get('data'))}".strip()
    if entry_type == "label":
        label = record.get("label")
        return f"label {label!r} on {record.get('targetId')}" if label else f"label cleared on {record.get('targetId')}"
    if entry_type == "session_info":
        return str(record.get("name") or "")
    return _stringify({k: v for k, v in record.items() if k != "type"})


def _meta_entry_fields(entry_type: str, record: dict[str, Any]) -> dict[str, Any]:
    """The numbers on a bookkeeping entry that the agent may want to reason about."""
    fields: dict[str, Any] = {}
    if entry_type == "model_change":
        fields["provider"] = record.get("provider")
        fields["model"] = record.get("modelId")
    elif entry_type == "thinking_level_change":
        fields["thinking_level"] = record.get("thinkingLevel")
    elif entry_type == "compaction":
        fields["tokens_before"] = record.get("tokensBefore")
        if isinstance(record.get("firstKeptEntryId"), str):
            fields["first_kept_entry_id"] = record["firstKeptEntryId"]
        # v1 sessions point at an index rather than an id; migration rewrites it,
        # but a file pi has never reopened still carries the old spelling.
        if isinstance(record.get("firstKeptEntryIndex"), int):
            fields["first_kept_entry_index"] = record["firstKeptEntryIndex"]
        if isinstance(record.get("retainedTail"), list):
            fields["retained_tail_count"] = len(record["retainedTail"])
        if record.get("fromHook") is True:
            fields["from_extension"] = True
    elif entry_type == "branch_summary":
        fields["from_id"] = record.get("fromId")
        if record.get("fromHook") is True:
            fields["from_extension"] = True
    elif entry_type == "custom":
        fields["custom_type"] = record.get("customType")
    elif entry_type == "label":
        fields["target_id"] = record.get("targetId")
        fields["label"] = record.get("label")
    elif entry_type == "session_info":
        fields["name"] = record.get("name")
    if isinstance(record.get("usage"), dict):
        fields["usage"] = record["usage"]
    return fields


# -- message helpers ----------------------------------------------------


def _assistant_events(
    message: dict[str, Any],
    *,
    session_id: str,
    ts: datetime | None,
    next_index: int,
    meta: dict[str, Any],
    pending_calls: dict[str, tuple[int, datetime | None]],
) -> list[NormalizedEvent]:
    """One event per content block, plus one for a turn that ended badly.

    `stopReason` and `errorMessage` have no field on `NormalizedEvent` and would
    otherwise reach the agent only through `meta`, which nothing renders. A turn
    that ended at `error`, `aborted` or `length` is precisely the kind of thing
    the outline exists to show, so it gets a row of its own carrying pi's own
    words -- never a sentence of ours.
    """
    message_meta = dict(meta)
    for key, name in (
        ("provider", "provider"),
        ("model", "model"),
        ("api", "api"),
        ("usage", "usage"),
        ("stopReason", "stop_reason"),
    ):
        if key in message:
            message_meta[name] = message[key]

    events: list[NormalizedEvent] = []
    content = message.get("content")
    blocks = content if isinstance(content, list) else []

    for block in blocks:
        index = next_index + len(events)
        if not isinstance(block, dict):
            events.append(
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=index,
                    ts=ts,
                    role="assistant",
                    kind="message",
                    text=str(block),
                    meta=message_meta,
                    parse_error=f"content block was {type(block).__name__}, not object",
                )
            )
            continue

        block_type = str(block.get("type") or "unknown")
        if block_type == "toolCall":
            call_id = str(block.get("id") or "")
            if call_id:
                pending_calls[call_id] = (index, ts)
            events.append(
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=index,
                    ts=ts,
                    role="assistant",
                    kind="tool_call",
                    tool_name=str(block.get("name") or "") or None,
                    text=_stringify(block.get("arguments")),
                    meta={**message_meta, "tool_call_id": call_id},
                )
            )
        elif block_type == "thinking":
            events.append(
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=index,
                    ts=ts,
                    role="assistant",
                    kind="thinking",
                    text=str(block.get("thinking") or ""),
                    meta=message_meta,
                )
            )
        elif block_type == "text":
            events.append(
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=index,
                    ts=ts,
                    role="assistant",
                    kind="message",
                    text=str(block.get("text") or ""),
                    meta=message_meta,
                )
            )
        else:
            events.append(
                NormalizedEvent(
                    session_id=session_id,
                    source="pi",
                    index=index,
                    ts=ts,
                    role="assistant",
                    kind="message",
                    text=_block_text(block),
                    meta={**message_meta, "block_type": block_type},
                    parse_error=f"unrecognized content block type {block_type!r}",
                )
            )

    if not events:
        events.append(
            NormalizedEvent(
                session_id=session_id,
                source="pi",
                index=next_index,
                ts=ts,
                role="assistant",
                kind="message",
                text="",
                meta={**message_meta, "empty_content_list": True},
            )
        )

    stop_reason = message.get("stopReason")
    error_message = message.get("errorMessage")
    if stop_reason in ("error", "aborted", "length") or (
        isinstance(error_message, str) and error_message.strip()
    ):
        events.append(
            NormalizedEvent(
                session_id=session_id,
                source="pi",
                index=next_index + len(events),
                ts=ts,
                role="system",
                kind="meta",
                text=(
                    f"turn ended: {stop_reason}"
                    + (f" -- {error_message}" if isinstance(error_message, str) and error_message else "")
                ),
                meta={**message_meta, "turn_end": True},
            )
        )

    return events


def _tool_result_event(
    message: dict[str, Any],
    *,
    session_id: str,
    ts: datetime | None,
    index: int,
    meta: dict[str, Any],
    pending_calls: dict[str, tuple[int, datetime | None]],
) -> NormalizedEvent:
    call_id = str(message.get("toolCallId") or "")
    call_index, call_ts = pending_calls.get(call_id, (None, None))
    text = _content_text(message.get("content"))

    duration_ms: int | None = None
    duration_source = "unavailable"
    if call_ts is not None and ts is not None:
        delta = int((ts - call_ts).total_seconds() * 1000)
        if delta >= 0:
            duration_ms, duration_source = delta, "derived"

    shape = compute_shape(
        text,
        duration_ms=duration_ms,
        # pi records no exit code for tool results. The bash tool appends
        # "Command exited with code N" to the payload when a command fails, but
        # recovering the number means matching a human-readable sentence that
        # changes between releases, and a wrong parse would be indistinguishable
        # from a recorded code. `has_error_field` already carries the signal.
        exit_code=None,
        has_error_field=bool(message.get("isError")),
        duration_source=duration_source,
    )

    result_meta: dict[str, Any] = {**meta, "tool_call_id": call_id}
    if call_index is not None:
        result_meta["call_event_index"] = call_index
    if isinstance(message.get("usage"), dict):
        result_meta["usage"] = message["usage"]

    details = message.get("details")
    if isinstance(details, dict):
        truncation = details.get("truncation")
        if isinstance(truncation, dict):
            # pi is the only supported CLI that says how much it cut and why.
            # Carried verbatim so the agent can read the original size rather
            # than infer a cap from the byte count.
            result_meta["truncation"] = {
                k: v for k, v in truncation.items() if k != "content"
            }
        if isinstance(details.get("fullOutputPath"), str):
            result_meta["full_output_path"] = details["fullOutputPath"]

    return NormalizedEvent(
        session_id=session_id,
        source="pi",
        index=index,
        ts=ts,
        role="user",
        kind="tool_result",
        # pi names the tool on the result itself, so this does not depend on the
        # join. The join below only marks results whose call never appeared.
        tool_name=str(message.get("toolName") or "") or None,
        text=text,
        shape=shape,
        meta=result_meta,
    )


def _bash_execution_events(
    message: dict[str, Any],
    *,
    session_id: str,
    ts: datetime | None,
    next_index: int,
    meta: dict[str, Any],
) -> list[NormalizedEvent]:
    """The user's own `!command`, split into the call and its result.

    One message on disk, but two events: modelling it as a single row would hide
    the command behind its output, and the exit code here is the only genuinely
    recorded one in a pi transcript.
    """
    exit_code = message.get("exitCode")
    output = str(message.get("output") or "")
    shape = compute_shape(
        output,
        duration_ms=None,
        exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        has_error_field=bool(message.get("cancelled")),
        duration_source="unavailable",
    )

    result_meta: dict[str, Any] = {**meta, "user_bash": True}
    for key, name in (
        ("cancelled", "cancelled"),
        ("truncated", "truncated"),
        ("fullOutputPath", "full_output_path"),
        ("excludeFromContext", "excluded_from_context"),
    ):
        if key in message:
            result_meta[name] = message[key]

    return [
        NormalizedEvent(
            session_id=session_id,
            source="pi",
            index=next_index,
            ts=ts,
            role="user",
            kind="tool_call",
            tool_name=_USER_BASH,
            text=str(message.get("command") or ""),
            meta={**meta, "user_bash": True},
        ),
        NormalizedEvent(
            session_id=session_id,
            source="pi",
            index=next_index + 1,
            ts=ts,
            role="user",
            kind="tool_result",
            tool_name=_USER_BASH,
            text=output,
            shape=shape,
            meta={**result_meta, "call_event_index": next_index},
        ),
    ]


def _mark_orphan_results(events: list[NormalizedEvent]) -> None:
    """Flag results whose call never appears in the transcript.

    pi names the tool on the result, so unlike Claude Code nothing is mislabelled
    when the call is missing. What is still worth saying is that the call is
    missing at all: a session resumed after the call, or a file cut mid-flight.
    """
    for event in events:
        if event.kind != "tool_result" or event.meta.get("user_bash"):
            continue
        if "call_event_index" not in event.meta:
            event.meta["orphan_result"] = True


# -- text helpers -------------------------------------------------------


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _block_text(block: dict[str, Any]) -> str:
    """One content block as text.

    Images are named rather than inlined. A pasted screenshot is megabytes of
    base64, and a shape record measuring that would report the encoding rather
    than anything the model reasoned about.
    """
    block_type = block.get("type")
    if isinstance(block.get("text"), str):
        return block["text"]
    if block_type == "image":
        mime = block.get("mimeType")
        data = block.get("data")
        size = len(data) if isinstance(data, str) else 0
        return f"[image {mime or 'unknown'} base64={size}]"
    if block_type == "thinking" and isinstance(block.get("thinking"), str):
        return block["thinking"]
    return _stringify(block)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _stringify(content)
    parts: list[str] = []
    for block in content:
        parts.append(_block_text(block) if isinstance(block, dict) else str(block))
    return "\n".join(parts)


def _id_from_name(path: Path) -> str:
    """`<timestamp>_<uuid>` -> the uuid, when the header could not be read."""
    stem = path.stem
    _, _, tail = stem.rpartition("_")
    return tail or stem
