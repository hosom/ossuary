"""Codex CLI rollout adapter.

Layout:

    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl

Schema derived from the Codex source rather than from observation, because no
Codex data exists on the machine this was written on. The relevant Rust
definitions are `RolloutLine` and `RolloutItem` in `codex-rs/protocol/src/
protocol.rs` and `ResponseItem` in `codex-rs/protocol/src/models.rs`:

    #[serde(tag = "type", content = "payload", rename_all = "snake_case")]
    pub enum RolloutItem { SessionMeta, ResponseItem, Compacted, TurnContext,
                           WorldState, EventMsg, InterAgentCommunication, ... }

so each line is::

    {"timestamp": "...", "ordinal": 3, "type": "response_item",
     "payload": {"type": "function_call", "name": "shell",
                 "arguments": "{...}", "call_id": "call_abc"}}

Two shape differences from Claude Code drove the code below:

  * Function-call arguments arrive as a *JSON string*, not an object -- the
    Responses API serialises them that way and Codex stores them verbatim.
  * `function_call_output.output` is untagged: either a bare string or a list of
    content items. Both spellings are handled.

Because this adapter is written against source rather than against real files,
it is deliberately more forgiving than the Claude Code one: any payload shape it
does not recognise still becomes an event carrying the raw JSON, so a schema
drift degrades resolution instead of losing the session.
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

_META_LINE_TYPES = {
    "session_meta",
    "turn_context",
    "compacted",
    "world_state",
    "event_msg",
    "inter_agent_communication_metadata",
}


class CodexAdapter(Adapter):
    source = "codex"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots

    def default_roots(self) -> list[Path]:
        env = os.environ.get("CODEX_HOME")
        base = Path(env) if env else Path.home() / ".codex"
        return [base / "sessions", base / "archived_sessions"]

    def claims(self, path: Path) -> bool:
        """A rollout line is `{timestamp, type, payload}` with no `sessionId`."""
        for record in self.head_records(path):
            if "sessionId" in record:
                return False  # Claude Code
            if record.get("type") == "session" and "cwd" in record:
                return False  # pi: a session header, not a rollout line
            if record.get("type") == "session_meta":
                return True
            if "payload" in record and "type" in record and "timestamp" in record:
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
                        source="codex",
                        path=str(path),
                        size_bytes=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        project=None,
                    )
                )
        return refs

    def parse(self, ref: SessionRef) -> Session:
        path = Path(ref.path)
        events: list[NormalizedEvent] = []
        parse_errors = 0
        pending_calls: dict[str, tuple[int, datetime | None, str | None]] = {}
        project: str | None = None
        session_id = ref.session_id

        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            session = Session(session_id=session_id, source="codex", path=str(path))
            session.events.append(
                unparseable_event(
                    session_id=session_id,
                    source="codex",
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
                        session_id=session_id,
                        source="codex",
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
                        session_id=session_id,
                        source="codex",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: expected object, got {type(record).__name__}",
                    )
                )
                parse_errors += 1
                continue

            line_type = str(record.get("type") or "unknown")
            ts = self.parse_timestamp(record.get("timestamp"))
            payload = record.get("payload")

            if line_type == "session_meta" and isinstance(payload, dict):
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    project = cwd
                for key in ("session_id", "id"):
                    value = payload.get(key)
                    if isinstance(value, str) and value:
                        session_id = value
                        break

            try:
                produced = self._events_for_line(
                    record,
                    line_type=line_type,
                    payload=payload,
                    ts=ts,
                    session_id=session_id,
                    line=line,
                    line_no=line_no,
                    next_index=len(events),
                    pending_calls=pending_calls,
                )
            except Exception as exc:  # noqa: BLE001 - never lose a line
                events.append(
                    unparseable_event(
                        session_id=session_id,
                        source="codex",
                        index=len(events),
                        raw=line,
                        error=f"line {line_no + 1}: normalization failed: {exc!r}",
                    )
                )
                parse_errors += 1
                continue

            events.extend(produced)

        # `session_meta` may appear after events we already emitted, so stamp the
        # resolved id across every event rather than leaving a mixed set.
        for event in events:
            event.session_id = session_id

        return Session(
            session_id=session_id,
            source="codex",
            path=str(path),
            events=events,
            content_hash=self.file_hash(path),
            parse_error_count=parse_errors,
            project=project or ref.project,
        )

    def _events_for_line(
        self,
        record: dict[str, Any],
        *,
        line_type: str,
        payload: Any,
        ts: datetime | None,
        session_id: str,
        line: str,
        line_no: int,
        next_index: int,
        pending_calls: dict[str, tuple[int, datetime | None, str | None]],
    ) -> list[NormalizedEvent]:
        base_meta: dict[str, Any] = {"line_type": line_type, "line_no": line_no + 1}
        if record.get("ordinal") is not None:
            base_meta["ordinal"] = record["ordinal"]

        if line_type in _META_LINE_TYPES:
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="codex",
                    index=next_index,
                    ts=ts,
                    role="system",
                    kind="meta",
                    text=_stringify(payload),
                    meta=base_meta,
                )
            ]

        if line_type != "response_item" or not isinstance(payload, dict):
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="codex",
                    index=next_index,
                    ts=ts,
                    role="unknown",
                    kind="meta",
                    text=_stringify(payload if payload is not None else record),
                    meta=base_meta,
                    raw=line if line_type == "unknown" else None,
                    parse_error=(
                        f"unrecognized rollout line type {line_type!r}"
                        if line_type not in _META_LINE_TYPES
                        else None
                    ),
                )
            ]

        item_type = str(payload.get("type") or "unknown")
        meta = {**base_meta, "item_type": item_type}

        if item_type in ("message", "agent_message"):
            role = str(payload.get("role") or "assistant")
            if role not in ("user", "assistant", "system"):
                role = "unknown"
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="codex",
                    index=next_index,
                    ts=ts,
                    role=role,  # type: ignore[arg-type]
                    kind="message",
                    text=_content_text(payload.get("content")),
                    meta=meta,
                )
            ]

        if item_type == "reasoning":
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="codex",
                    index=next_index,
                    ts=ts,
                    role="assistant",
                    kind="thinking",
                    text=_reasoning_text(payload),
                    meta=meta,
                )
            ]

        if item_type in ("function_call", "local_shell_call", "custom_tool_call", "tool_search_call"):
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            name = _call_name(payload, item_type)
            if call_id:
                pending_calls[call_id] = (next_index, ts, name)
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="codex",
                    index=next_index,
                    ts=ts,
                    role="assistant",
                    kind="tool_call",
                    tool_name=name,
                    text=_call_arguments_text(payload, item_type),
                    meta={**meta, "call_id": call_id},
                )
            ]

        if item_type in ("function_call_output", "custom_tool_call_output"):
            call_id = str(payload.get("call_id") or "")
            call_index, call_ts, call_name = pending_calls.get(call_id, (None, None, None))
            text, success = _output_text_and_success(payload.get("output"))

            duration_ms: int | None = None
            duration_source = "unavailable"
            if call_ts is not None and ts is not None:
                delta = int((ts - call_ts).total_seconds() * 1000)
                if delta >= 0:
                    duration_ms, duration_source = delta, "derived"

            shape = compute_shape(
                text,
                duration_ms=duration_ms,
                exit_code=None,
                has_error_field=success is False,
                duration_source=duration_source,
            )
            result_meta = {**meta, "call_id": call_id}
            if success is not None:
                result_meta["success"] = success
            if call_index is not None:
                result_meta["call_event_index"] = call_index
            else:
                result_meta["orphan_result"] = True

            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="codex",
                    index=next_index,
                    ts=ts,
                    role="user",
                    kind="tool_result",
                    tool_name=call_name,
                    text=text,
                    shape=shape,
                    meta=result_meta,
                )
            ]

        return [
            NormalizedEvent(
                session_id=session_id,
                source="codex",
                index=next_index,
                ts=ts,
                role="unknown",
                kind="meta",
                text=_stringify(payload),
                meta=meta,
                parse_error=f"unrecognized response item type {item_type!r}",
            )
        ]


# -- helpers ------------------------------------------------------------


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _content_text(content: Any) -> str:
    """Flatten a `Vec<ContentItem>` into readable text.

    Variants are `input_text`, `output_text`, `input_image`, `input_audio`.
    Non-text entries are named rather than dropped, so a message that was mostly
    an image does not read downstream as an empty turn.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _stringify(content)

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        item_type = item.get("type")
        if isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item_type == "input_image":
            parts.append("[input_image]")
        elif item_type == "input_audio":
            parts.append("[input_audio]")
        elif isinstance(item.get("encrypted_content"), str):
            parts.append("[encrypted_content]")
        else:
            parts.append(_stringify(item))
    return "\n".join(parts)


def _reasoning_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("summary", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item is not None:
                    parts.append(_stringify(item))
    if not parts and payload.get("encrypted_content"):
        return "[encrypted reasoning]"
    return "\n".join(parts)


def _call_name(payload: dict[str, Any], item_type: str) -> str | None:
    name = payload.get("name")
    if isinstance(name, str) and name:
        namespace = payload.get("namespace")
        if isinstance(namespace, str) and namespace:
            return f"{namespace}.{name}"
        return name
    if item_type == "local_shell_call":
        return "local_shell"
    if item_type == "tool_search_call":
        return "tool_search"
    return None


def _call_arguments_text(payload: dict[str, Any], item_type: str) -> str:
    """Arguments as text.

    The Responses API hands back function arguments as a JSON *string*, and
    Codex persists that string verbatim. It is returned unchanged rather than
    re-serialised, so what the outline shows is what the model emitted.
    """
    if item_type == "local_shell_call":
        action = payload.get("action")
        if isinstance(action, dict):
            command = action.get("command")
            if isinstance(command, list):
                return " ".join(str(part) for part in command)
            return _stringify(action)
        return _stringify(action)

    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        return arguments
    return _stringify(arguments)


def _output_text_and_success(output: Any) -> tuple[str, bool | None]:
    """Unpack `FunctionCallOutputPayload`.

    On the wire the body is untagged: a bare string, or a list of content items.
    A sibling `success` flag may or may not be present.
    """
    if output is None:
        return "", None
    if isinstance(output, str):
        return output, None
    if isinstance(output, list):
        return _content_text(output), None
    if isinstance(output, dict):
        success = output.get("success")
        success = success if isinstance(success, bool) else None
        body = output.get("body", output.get("content", output.get("output")))
        if isinstance(body, str):
            return body, success
        if isinstance(body, list):
            return _content_text(body), success
        if body is None and "content_items" in output:
            return _content_text(output["content_items"]), success
        return _stringify(output if body is None else body), success
    return _stringify(output), None
