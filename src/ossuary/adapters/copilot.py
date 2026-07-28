"""GitHub Copilot adapter.

Copilot is different in kind from the other two, and in two ways rather than one.

**Copilot CLI** writes a per-session directory::

    ~/.copilot/session-state/<session-id>/events.jsonl
    ~/.copilot/session-state/<session-id>/workspace.yaml

(with a legacy `~/.copilot/history-session-state/` from before v0.0.342). That is
JSONL and structurally close to Claude Code and Codex.

**VS Code Copilot Chat** is the case the brief flagged: chat history lives in
`workspaceStorage` as *JSON*, one object per session with a `requests` array,
not a line-per-event log::

    ~/.config/Code/User/workspaceStorage/<hash>/chatSessions/<uuid>.json   (Linux)
    ~/Library/Application Support/Code/...                                 (macOS)
    %APPDATA%/Code/User/workspaceStorage/...                               (Windows)

Both are handled here, dispatched on what is actually found on disk.

A caveat worth stating plainly: no Copilot data existed on the machine this was
written on, so unlike the Claude Code adapter this one is not confirmed against
real files. It is written defensively -- structures it does not recognise become
events carrying the raw JSON with `parse_error` set, so unfamiliar data degrades
resolution rather than disappearing. The golden tests use synthetic fixtures
built to the documented shape; they prove the adapter's behaviour, not the
schema's accuracy.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import NormalizedEvent, Session, SessionRef
from ..shape import compute_shape
from .base import Adapter, unparseable_event


class CopilotAdapter(Adapter):
    source = "copilot"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots

    # -- discovery ------------------------------------------------------

    def default_roots(self) -> list[Path]:
        roots: list[Path] = []
        home = Path.home()

        cli_home = os.environ.get("COPILOT_HOME")
        base = Path(cli_home) if cli_home else home / ".copilot"
        roots.append(base / "session-state")
        roots.append(base / "history-session-state")

        for storage in _vscode_storage_dirs(home):
            roots.append(storage)
        return roots

    def claims(self, path: Path) -> bool:
        """Copilot is identified by filename and location, not line shape."""
        if path.name == "events.jsonl":
            return True
        if path.suffix == ".json":
            return _looks_like_vscode_chat(path)
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

            if root.is_file():
                candidates = [root]
            else:
                # CLI: <session>/events.jsonl. VS Code: chat session JSON blobs.
                candidates = sorted(root.rglob("events.jsonl"))
                candidates += [
                    p
                    for p in sorted(root.rglob("*.json"))
                    if _looks_like_vscode_chat(p)
                ]

            for path in candidates:
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
                        session_id=(
                            path.parent.name if path.name == "events.jsonl" else path.stem
                        ),
                        source="copilot",
                        path=str(path),
                        size_bytes=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        project=_workspace_hint(path),
                    )
                )
        return refs

    # -- parsing --------------------------------------------------------

    def parse(self, ref: SessionRef) -> Session:
        path = Path(ref.path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            session = Session(session_id=ref.session_id, source="copilot", path=str(path))
            session.events.append(
                unparseable_event(
                    session_id=ref.session_id,
                    source="copilot",
                    index=0,
                    raw="",
                    error=f"unreadable file: {exc}",
                )
            )
            session.parse_error_count = 1
            return session

        if path.suffix == ".jsonl" or path.name == "events.jsonl":
            events, errors = self._parse_jsonl(text, ref)
        else:
            events, errors = self._parse_vscode_json(text, ref)

        return Session(
            session_id=ref.session_id,
            source="copilot",
            path=str(path),
            events=events,
            content_hash=self.file_hash(path),
            parse_error_count=errors,
            project=ref.project,
        )

    def _parse_jsonl(
        self, text: str, ref: SessionRef
    ) -> tuple[list[NormalizedEvent], int]:
        events: list[NormalizedEvent] = []
        errors = 0
        pending: dict[str, tuple[int, datetime | None, str | None]] = {}

        for line_no, line in enumerate(text.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except (json.JSONDecodeError, ValueError) as exc:
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: {exc}",
                    )
                )
                errors += 1
                continue

            if not isinstance(record, dict):
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: expected object, got {type(record).__name__}",
                    )
                )
                errors += 1
                continue

            try:
                events.append(
                    self._event_for_cli_record(
                        record,
                        ref=ref,
                        index=len(events),
                        line_no=line_no,
                        pending=pending,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never lose a line
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: normalization failed: {exc!r}",
                    )
                )
                errors += 1

        return events, errors

    def _event_for_cli_record(
        self,
        record: dict[str, Any],
        *,
        ref: SessionRef,
        index: int,
        line_no: int,
        pending: dict[str, tuple[int, datetime | None, str | None]],
    ) -> NormalizedEvent:
        event_type = str(
            record.get("type") or record.get("event") or record.get("kind") or "unknown"
        ).lower()
        ts = self.parse_timestamp(
            record.get("timestamp") or record.get("time") or record.get("ts")
        )
        meta: dict[str, Any] = {"line_type": event_type, "line_no": line_no + 1}

        body = record
        for key in ("data", "payload", "message"):
            nested = record.get(key)
            if isinstance(nested, dict):
                body = nested
                break

        call_id = str(
            body.get("toolCallId")
            or body.get("tool_call_id")
            or body.get("callId")
            or body.get("id")
            or ""
        )
        tool_name = _first_str(body, ("toolName", "tool_name", "name", "tool"))

        if "tool" in event_type and ("result" in event_type or "output" in event_type or "response" in event_type):
            payload_text = _first_text(body, ("result", "output", "content", "text", "response"))
            call_index, call_ts, call_name = pending.get(call_id, (None, None, None))
            duration_ms, duration_source = _duration(body, call_ts, ts)
            shape = compute_shape(
                payload_text,
                duration_ms=duration_ms,
                exit_code=_int_or_none(body, ("exitCode", "exit_code", "status", "code")),
                has_error_field=_error_signal(body),
                duration_source=duration_source,
            )
            if call_index is not None:
                meta["call_event_index"] = call_index
            elif call_id:
                meta["orphan_result"] = True
            meta["tool_call_id"] = call_id
            return NormalizedEvent(
                session_id=ref.session_id,
                source="copilot",
                index=index,
                ts=ts,
                role="user",
                kind="tool_result",
                tool_name=tool_name or call_name,
                text=payload_text,
                shape=shape,
                meta=meta,
            )

        if "tool" in event_type and ("call" in event_type or "invoc" in event_type or "use" in event_type):
            if call_id:
                pending[call_id] = (index, ts, tool_name)
            meta["tool_call_id"] = call_id
            return NormalizedEvent(
                session_id=ref.session_id,
                source="copilot",
                index=index,
                ts=ts,
                role="assistant",
                kind="tool_call",
                tool_name=tool_name,
                text=_first_text(body, ("arguments", "args", "input", "parameters")),
                meta=meta,
            )

        if event_type in ("user", "user_message", "prompt", "request"):
            return NormalizedEvent(
                session_id=ref.session_id,
                source="copilot",
                index=index,
                ts=ts,
                role="user",
                kind="message",
                text=_first_text(body, ("text", "content", "message", "prompt")),
                meta=meta,
            )

        if event_type in ("assistant", "assistant_message", "response", "completion", "reply"):
            return NormalizedEvent(
                session_id=ref.session_id,
                source="copilot",
                index=index,
                ts=ts,
                role="assistant",
                kind="message",
                text=_first_text(body, ("text", "content", "message", "response")),
                meta=meta,
            )

        if "reason" in event_type or "think" in event_type:
            return NormalizedEvent(
                session_id=ref.session_id,
                source="copilot",
                index=index,
                ts=ts,
                role="assistant",
                kind="thinking",
                text=_first_text(body, ("text", "content", "reasoning")),
                meta=meta,
            )

        return NormalizedEvent(
            session_id=ref.session_id,
            source="copilot",
            index=index,
            ts=ts,
            role="unknown",
            kind="meta",
            text=_stringify(record),
            meta=meta,
            parse_error=f"unrecognized copilot event type {event_type!r}",
        )

    def _parse_vscode_json(
        self, text: str, ref: SessionRef
    ) -> tuple[list[NormalizedEvent], int]:
        """VS Code chat sessions: one JSON object with a `requests` array.

        Each request holds the user's message and the assistant's response parts.
        A whole file that fails to parse becomes a single `unparseable` event
        rather than an empty session -- an empty session would read downstream as
        "nothing happened here", which is a different and much more misleading
        claim than "this file could not be read".
        """
        try:
            blob = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            return (
                [
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=0,
                        raw=text[:4096],
                        error=f"invalid JSON document: {exc}",
                    )
                ],
                1,
            )

        if not isinstance(blob, dict):
            return (
                [
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=0,
                        raw=text[:4096],
                        error=f"expected object, got {type(blob).__name__}",
                    )
                ],
                1,
            )

        events: list[NormalizedEvent] = []
        errors = 0
        requests = blob.get("requests")
        creation = self.parse_timestamp(blob.get("creationDate"))

        if not isinstance(requests, list):
            return (
                [
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=0,
                        raw=text[:4096],
                        error="no `requests` array in chat session document",
                    )
                ],
                1,
            )

        for position, request in enumerate(requests):
            if not isinstance(request, dict):
                events.append(
                    unparseable_event(
                        session_id=ref.session_id,
                        source="copilot",
                        index=len(events),
                        raw=_stringify(request)[:4096],
                        error=f"request {position}: expected object",
                    )
                )
                errors += 1
                continue

            ts = self.parse_timestamp(request.get("timestamp")) or creation
            message = request.get("message")
            prompt = ""
            if isinstance(message, dict):
                prompt = str(message.get("text") or "")
            elif isinstance(message, str):
                prompt = message

            events.append(
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="copilot",
                    index=len(events),
                    ts=ts,
                    role="user",
                    kind="message",
                    text=prompt,
                    meta={"request_index": position, "request_id": request.get("requestId")},
                )
            )

            events.extend(
                self._vscode_response_events(
                    request, ref=ref, ts=ts, position=position, next_index=len(events)
                )
            )

        return events, errors

    def _vscode_response_events(
        self,
        request: dict[str, Any],
        *,
        ref: SessionRef,
        ts: datetime | None,
        position: int,
        next_index: int,
    ) -> list[NormalizedEvent]:
        response = request.get("response")
        parts = response if isinstance(response, list) else [response]
        text_chunks: list[str] = []
        events: list[NormalizedEvent] = []

        for part in parts:
            if part is None:
                continue
            if isinstance(part, str):
                text_chunks.append(part)
                continue
            if not isinstance(part, dict):
                text_chunks.append(str(part))
                continue

            kind = str(part.get("kind") or part.get("type") or "")
            if kind in ("toolInvocation", "toolInvocationSerialized", "tool"):
                tool_name = _first_str(part, ("toolId", "toolName", "name")) or None
                events.append(
                    NormalizedEvent(
                        session_id=ref.session_id,
                        source="copilot",
                        index=next_index + len(events),
                        ts=ts,
                        role="assistant",
                        kind="tool_call",
                        tool_name=tool_name,
                        text=_first_text(part, ("toolSpecificData", "invocationMessage", "input")),
                        meta={"request_index": position, "part_kind": kind},
                    )
                )
                result_text = _first_text(part, ("resultDetails", "output", "result"))
                if result_text:
                    events.append(
                        NormalizedEvent(
                            session_id=ref.session_id,
                            source="copilot",
                            index=next_index + len(events),
                            ts=ts,
                            role="user",
                            kind="tool_result",
                            tool_name=tool_name,
                            text=result_text,
                            shape=compute_shape(
                                result_text,
                                has_error_field=part.get("isError") is True,
                            ),
                            meta={
                                "request_index": position,
                                "call_event_index": next_index + len(events) - 1,
                            },
                        )
                    )
                continue

            value = part.get("value")
            if isinstance(value, str):
                text_chunks.append(value)
            elif isinstance(value, dict) and isinstance(value.get("value"), str):
                text_chunks.append(value["value"])
            elif isinstance(part.get("text"), str):
                text_chunks.append(part["text"])

        if text_chunks:
            events.append(
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="copilot",
                    index=next_index + len(events),
                    ts=ts,
                    role="assistant",
                    kind="message",
                    text="".join(text_chunks),
                    meta={"request_index": position},
                )
            )

        result = request.get("result")
        if isinstance(result, dict) and result.get("errorDetails"):
            events.append(
                NormalizedEvent(
                    session_id=ref.session_id,
                    source="copilot",
                    index=next_index + len(events),
                    ts=ts,
                    role="system",
                    kind="meta",
                    text=_stringify(result.get("errorDetails")),
                    meta={"request_index": position, "result_error": True},
                )
            )

        return events


# -- helpers ------------------------------------------------------------


def _vscode_storage_dirs(home: Path) -> list[Path]:
    """Per-platform VS Code workspaceStorage roots, including the forks."""
    bases: list[Path] = []
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        bases = [support / name for name in ("Code", "Code - Insiders", "VSCodium", "Cursor")]
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            bases = [Path(appdata) / name for name in ("Code", "Code - Insiders", "VSCodium", "Cursor")]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        bases = [config / name for name in ("Code", "Code - Insiders", "VSCodium", "Cursor")]
    return [base / "User" / "workspaceStorage" for base in bases]


def _looks_like_vscode_chat(path: Path) -> bool:
    """Cheap filter so discovery does not read every JSON blob in storage."""
    parts = {p.lower() for p in path.parts}
    return bool(parts & {"chatsessions", "chateditingsessions", "chat-sessions", "interactive-sessions"})


def _workspace_hint(path: Path) -> str | None:
    for parent in path.parents:
        if parent.name == "workspaceStorage":
            return None
        if parent.parent.name == "workspaceStorage":
            return parent.name
    return None


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _first_str(body: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_text(body: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, str):
            return value
        if value is not None:
            return _stringify(value)
    return ""


def _int_or_none(body: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _error_signal(body: dict[str, Any]) -> bool:
    for key in ("isError", "is_error", "error", "failed"):
        value = body.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip():
            return True
    stderr = body.get("stderr")
    return isinstance(stderr, str) and bool(stderr.strip())


def _duration(
    body: dict[str, Any], call_ts: datetime | None, result_ts: datetime | None
) -> tuple[int | None, str]:
    for key in ("durationMs", "duration_ms", "elapsedMs", "duration"):
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return int(value), "recorded"
    if call_ts is not None and result_ts is not None:
        delta = int((result_ts - call_ts).total_seconds() * 1000)
        if delta >= 0:
            return delta, "derived"
    return None, "unavailable"
