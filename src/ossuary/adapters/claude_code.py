"""Claude Code transcript adapter.

Layout confirmed against real files on disk (see `docs/formats.md`):

    ~/.claude/projects/<project-slug>/<session-id>.jsonl

where `<project-slug>` is the working directory with every non-alphanumeric
character replaced by `-`. There is **no** `sessions/` subdirectory under
`projects/`; the `.jsonl` files sit directly in the project directory. We glob
recursively anyway rather than hardcoding the depth, because this is exactly the
kind of detail that moves between CLI releases.

Structure notes that drove the design, all observed on disk:

  * One line can expand to several events. An assistant message carries a list of
    content blocks, and a line with a text block and two `tool_use` blocks is
    three events.
  * `tool_use` and `tool_result` live on *separate lines* and are **not
    adjacent** -- results arrive in completion order, not call order. Pairing is
    by `tool_use_id`, never by position.
  * A `toolUseResult` key sits alongside `message` on the line carrying the
    result, holding the harness's structured record (stdout, stderr, interrupted,
    ...). The block's own `content` is what the model actually saw, so that is
    what becomes `text`; the structured record goes to `meta`.
  * Line types beyond user/assistant include `attachment`, `queue-operation`,
    `last-prompt`, `system`, and `summary`. All are preserved as `meta` events
    rather than dropped -- a queued-and-cancelled operation is a health signal.
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

_ROLE_BY_TYPE = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
}

# Line types that are harness bookkeeping rather than conversation. Kept as
# `meta` events so they appear in the outline.
_META_TYPES = {
    "attachment",
    "queue-operation",
    "last-prompt",
    "summary",
    "file-history-snapshot",
    "compact-boundary",
}


class ClaudeCodeAdapter(Adapter):
    source = "claude-code"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots

    # -- discovery ------------------------------------------------------

    def default_roots(self) -> list[Path]:
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        candidates = [Path(env) if env else Path.home() / ".claude"]
        return [c / "projects" for c in candidates]

    def claims(self, path: Path) -> bool:
        """Claude Code stamps `sessionId` on every conversation line."""
        for record in self.head_records(path):
            if "sessionId" in record:
                return True
            if "message" in record and "uuid" in record:
                return True
        return False

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
                if require_claim and not self.claims(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                refs.append(
                    SessionRef(
                        session_id=path.stem,
                        source="claude-code",
                        path=str(path),
                        size_bytes=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        project=self._project_of(path),
                    )
                )
        return refs

    @staticmethod
    def _project_of(path: Path) -> str | None:
        parent = path.parent.name
        return parent or None

    # -- parsing --------------------------------------------------------

    def parse(self, ref: SessionRef) -> Session:
        path = Path(ref.path)
        events: list[NormalizedEvent] = []
        parse_errors = 0
        # tool_use_id -> (event index, timestamp) so results can be paired with
        # their call regardless of how far apart they landed.
        pending_calls: dict[str, tuple[int, datetime | None]] = {}

        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            session = Session(
                session_id=ref.session_id,
                source="claude-code",
                path=str(path),
                project=ref.project,
            )
            session.events.append(
                unparseable_event(
                    session_id=ref.session_id,
                    source="claude-code",
                    index=0,
                    raw="",
                    error=f"unreadable file: {exc}",
                )
            )
            session.parse_error_count = 1
            return session

        for line_no, line in enumerate(raw_lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except (json.JSONDecodeError, ValueError) as exc:
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="claude-code",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: {exc}",
                    )
                )
                parse_errors += 1
                continue

            if not isinstance(record, dict):
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="claude-code",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: expected object, got {type(record).__name__}",
                    )
                )
                parse_errors += 1
                continue

            try:
                produced = self._events_for_record(
                    record,
                    ref=ref,
                    line=line,
                    line_no=line_no,
                    next_index=len(events),
                    pending_calls=pending_calls,
                )
            except Exception as exc:  # noqa: BLE001 - never lose a line
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="claude-code",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: normalization failed: {exc!r}",
                    )
                )
                parse_errors += 1
                continue

            events.extend(produced)

        _join_tool_names(events)

        session = Session(
            session_id=ref.session_id,
            source="claude-code",
            path=str(path),
            events=events,
            content_hash=self.file_hash(path),
            parse_error_count=parse_errors,
            project=ref.project,
        )
        return session

    # -- record -> events ------------------------------------------------

    def _events_for_record(
        self,
        record: dict[str, Any],
        *,
        ref: SessionRef,
        line: str,
        line_no: int,
        next_index: int,
        pending_calls: dict[str, tuple[int, datetime | None]],
    ) -> list[NormalizedEvent]:
        line_type = str(record.get("type") or "unknown")
        ts = self.parse_timestamp(record.get("timestamp"))
        base_meta = self._line_meta(record)

        if line_type in _META_TYPES:
            return [
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="claude-code",
                    index=next_index,
                    ts=ts,
                    role="system",
                    kind="meta",
                    text=self._meta_text(line_type, record),
                    meta={**base_meta, "line_type": line_type, "line_no": line_no + 1},
                )
            ]

        message = record.get("message")
        if not isinstance(message, dict):
            # A conversation line with no message body. Unusual but not fatal --
            # keep it as meta so it is visible in the outline.
            return [
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="claude-code",
                    index=next_index,
                    ts=ts,
                    role=_ROLE_BY_TYPE.get(line_type, "unknown"),  # type: ignore[arg-type]
                    kind="meta",
                    text="",
                    meta={**base_meta, "line_type": line_type, "line_no": line_no + 1},
                    raw=line,
                    parse_error="no message body on conversation line",
                )
            ]

        role = _ROLE_BY_TYPE.get(str(message.get("role") or line_type), "unknown")
        content = message.get("content")
        tool_use_result = record.get("toolUseResult")

        message_meta = {**base_meta, "line_no": line_no + 1}
        if isinstance(message.get("model"), str):
            message_meta["model"] = message["model"]
        for key in ("stop_reason", "stop_sequence", "usage", "id"):
            if key in message:
                message_meta[key] = message[key]

        if isinstance(content, str):
            return [
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="claude-code",
                    index=next_index,
                    ts=ts,
                    role=role,  # type: ignore[arg-type]
                    kind="message",
                    text=content,
                    meta=message_meta,
                )
            ]

        if not isinstance(content, list):
            return [
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="claude-code",
                    index=next_index,
                    ts=ts,
                    role=role,  # type: ignore[arg-type]
                    kind="message",
                    text="" if content is None else str(content),
                    meta=message_meta,
                    raw=None if content is None else line,
                    parse_error=(
                        None
                        if content is None
                        else f"unexpected content type {type(content).__name__}"
                    ),
                )
            ]

        events: list[NormalizedEvent] = []
        for block in content:
            index = next_index + len(events)
            if not isinstance(block, dict):
                events.append(
                    NormalizedEvent(
                        session_id=ref.session_id,
                        source="claude-code",
                        index=index,
                        ts=ts,
                        role=role,  # type: ignore[arg-type]
                        kind="message",
                        text=str(block),
                        meta=message_meta,
                        raw=line,
                        parse_error=f"content block was {type(block).__name__}, not object",
                    )
                )
                continue
            events.append(
                self._event_for_block(
                    block,
                    ref=ref,
                    index=index,
                    ts=ts,
                    role=role,
                    message_meta=message_meta,
                    tool_use_result=tool_use_result,
                    pending_calls=pending_calls,
                )
            )

        if not events:
            # An empty content list is itself worth seeing in the outline.
            events.append(
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="claude-code",
                    index=next_index,
                    ts=ts,
                    role=role,  # type: ignore[arg-type]
                    kind="message",
                    text="",
                    meta={**message_meta, "empty_content_list": True},
                )
            )
        return events

    def _event_for_block(
        self,
        block: dict[str, Any],
        *,
        ref: SessionRef,
        index: int,
        ts: datetime | None,
        role: str,
        message_meta: dict[str, Any],
        tool_use_result: Any,
        pending_calls: dict[str, tuple[int, datetime | None]],
    ) -> NormalizedEvent:
        block_type = str(block.get("type") or "unknown")

        if block_type == "tool_use":
            tool_id = str(block.get("id") or "")
            if tool_id:
                pending_calls[tool_id] = (index, ts)
            args = block.get("input")
            return NormalizedEvent(
                session_id=ref.session_id,
                source="claude-code",
                index=index,
                ts=ts,
                role=role,  # type: ignore[arg-type]
                kind="tool_call",
                tool_name=str(block.get("name") or "") or None,
                text=_stringify_args(args),
                meta={
                    **message_meta,
                    "tool_use_id": tool_id,
                    "tool_input": args,
                    "caller": block.get("caller"),
                },
            )

        if block_type == "tool_result":
            tool_id = str(block.get("tool_use_id") or "")
            call_index, call_ts = pending_calls.get(tool_id, (None, None))
            text = _tool_result_text(block.get("content"))
            structured = tool_use_result if isinstance(tool_use_result, dict) else None

            duration_ms, duration_source = _duration_for(structured, call_ts, ts)
            exit_code = _exit_code_for(structured)
            is_error = bool(block.get("is_error"))
            has_error_field = is_error or _has_error_signal(structured)

            shape = compute_shape(
                text,
                duration_ms=duration_ms,
                exit_code=exit_code,
                has_error_field=has_error_field,
                duration_source=duration_source,
            )

            meta: dict[str, Any] = {
                **message_meta,
                "tool_use_id": tool_id,
                "is_error": is_error,
            }
            if call_index is not None:
                meta["call_event_index"] = call_index
            if structured is not None:
                meta["tool_use_result"] = _summarize_structured(structured)

            return NormalizedEvent(
                session_id=ref.session_id,
                source="claude-code",
                index=index,
                ts=ts,
                role=role,  # type: ignore[arg-type]
                kind="tool_result",
                tool_name=_tool_name_for_result(structured),
                text=text,
                shape=shape,
                meta=meta,
            )

        if block_type == "thinking":
            thinking_text = str(block.get("thinking") or block.get("text") or "")
            thinking_meta = dict(message_meta)
            if not thinking_text and block.get("signature"):
                # Claude Code persists the signature but not the reasoning text
                # for some responses. Flagged so a run of empty thinking rows
                # reads as "not written to disk" rather than "the model produced
                # no reasoning" -- those are very different findings.
                thinking_meta["thinking_signature_only"] = True
            return NormalizedEvent(
                session_id=ref.session_id,
                source="claude-code",
                index=index,
                ts=ts,
                role=role,  # type: ignore[arg-type]
                kind="thinking",
                text=thinking_text,
                meta=thinking_meta,
            )

        if block_type in ("text", "redacted_thinking"):
            return NormalizedEvent(
                session_id=ref.session_id,
                source="claude-code",
                index=index,
                ts=ts,
                role=role,  # type: ignore[arg-type]
                kind="thinking" if block_type == "redacted_thinking" else "message",
                text=str(block.get("text") or block.get("data") or ""),
                meta=message_meta,
            )

        # An unrecognised block type. Keep the payload and say so, rather than
        # guessing at semantics that may not exist yet.
        return NormalizedEvent(
            session_id=ref.session_id,
            source="claude-code",
            index=index,
            ts=ts,
            role=role,  # type: ignore[arg-type]
            kind="message",
            text=_stringify_args(block),
            meta={**message_meta, "block_type": block_type},
            parse_error=f"unrecognized content block type {block_type!r}",
        )

    @staticmethod
    def _line_meta(record: dict[str, Any]) -> dict[str, Any]:
        keep = (
            "uuid", "parentUuid", "requestId", "promptId", "gitBranch", "cwd",
            "version", "isSidechain", "userType", "entrypoint", "permissionMode",
            "effort", "origin", "promptSource", "sourceToolAssistantUUID",
        )
        return {k: record[k] for k in keep if k in record}

    @staticmethod
    def _meta_text(line_type: str, record: dict[str, Any]) -> str:
        if line_type == "attachment":
            attachment = record.get("attachment")
            if isinstance(attachment, dict):
                kind = attachment.get("type") or "attachment"
                content = attachment.get("content")
                if isinstance(content, str):
                    return f"[{kind}] {content}"
                return f"[{kind}] {_stringify_args({k: v for k, v in attachment.items() if k != 'type'})}"
        if line_type == "queue-operation":
            return f"[queue-operation {record.get('operation')}] {record.get('content') or ''}".strip()
        if line_type == "summary":
            return str(record.get("summary") or "")
        if line_type == "last-prompt":
            return str(record.get("lastPrompt") or "")
        return _stringify_args({k: v for k, v in record.items() if k != "type"})


# -- helpers ------------------------------------------------------------


def _join_tool_names(events: list[NormalizedEvent]) -> None:
    """Label each `tool_result` with the tool that produced it.

    Claude Code does not repeat the tool name on the result line, and results are
    not adjacent to their calls, so the join is on `tool_use_id`. Results whose
    call never appears -- a transcript truncated mid-flight, or a resumed session
    that begins after the call -- keep `tool_name=None` and are counted under an
    explicit unknown bucket rather than being attributed to the wrong tool.
    """
    names_by_call_id: dict[str, str] = {}
    for event in events:
        if event.kind == "tool_call":
            call_id = str(event.meta.get("tool_use_id") or "")
            if call_id and event.tool_name:
                names_by_call_id[call_id] = event.tool_name

    for event in events:
        if event.kind != "tool_result" or event.tool_name:
            continue
        call_id = str(event.meta.get("tool_use_id") or "")
        name = names_by_call_id.get(call_id)
        if name:
            event.tool_name = name
        else:
            event.meta["orphan_result"] = True


def _stringify_args(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_result_text(content: Any) -> str:
    """The text the model actually saw for this tool result."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image":
                    source = block.get("source")
                    media = ""
                    if isinstance(source, dict):
                        media = str(source.get("media_type") or "")
                    parts.append(f"[image {media}]".strip())
                else:
                    parts.append(_stringify_args(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return _stringify_args(content)


def _duration_for(
    structured: dict[str, Any] | None,
    call_ts: datetime | None,
    result_ts: datetime | None,
) -> tuple[int | None, str]:
    """Duration in ms, preferring a recorded value over a derived one.

    Claude Code records `durationMs` for some tools but not for Bash, so for most
    results the only available measure is the wall-clock gap between the call
    line and the result line. That is a real measurement, but it includes any
    time the harness spent elsewhere, so its provenance is reported alongside it
    and never presented as if the CLI had supplied it.
    """
    if structured:
        for key in ("durationMs", "duration_ms", "durationMS", "elapsedMs"):
            value = structured.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value), "recorded"

    if call_ts is not None and result_ts is not None:
        delta_ms = int((result_ts - call_ts).total_seconds() * 1000)
        if delta_ms >= 0:
            return delta_ms, "derived"

    return None, "unavailable"


def _exit_code_for(structured: dict[str, Any] | None) -> int | None:
    """An exit code only when one was genuinely recorded.

    Deliberately does not synthesise a code from an error flag: a fabricated 1
    would be indistinguishable from a real one downstream, and the agent reasons
    about exactly this field.
    """
    if not structured:
        return None
    for key in ("exitCode", "exit_code", "returnCode", "code", "status"):
        value = structured.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def _has_error_signal(structured: dict[str, Any] | None) -> bool:
    if not structured:
        return False
    if structured.get("interrupted") is True:
        return True
    stderr = structured.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        return True
    for key in ("error", "isError", "is_error"):
        value = structured.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _tool_name_for_result(structured: dict[str, Any] | None) -> str | None:
    """Claude Code does not name the tool on the result line.

    The name is recovered downstream by joining on `tool_use_id`, which is exact.
    Guessing here from the structured record's key shape would be a heuristic
    that silently mislabels statistics, so we return nothing and let the join do
    it.
    """
    return None


def _summarize_structured(structured: dict[str, Any]) -> dict[str, Any]:
    """Keep the structured record's signal without duplicating whole payloads."""
    out: dict[str, Any] = {}
    for key, value in structured.items():
        if isinstance(value, str):
            out[f"{key}_len"] = len(value)
            if len(value) <= 200:
                out[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            out[f"{key}_keys"] = sorted(value.keys())[:20]
    return out
