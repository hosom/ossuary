"""Agent Zero transcript adapter.

Layout (see `docs/formats.md`, and `docs/agent-zero-investigation.md` for the
reasoning):

    <agent-zero-root>/usr/chats/<context-id>/chat.json
    <agent-zero-root>/usr/chats/<context-id>/backups/pre-compact-<ts>.json

Written against Agent Zero's source, not against a real conversation: no install
existed on the machine this was written on. Same evidence class as the Codex
adapter.

Four things make this different from the other four formats:

  * **One JSON object per chat, not a line per event.** The whole file is read
    and parsed at once. A file that is not valid JSON is one degraded event
    carrying its (elided) text, not a lost session.

  * **There are two records of the same session and neither contains the
    other.** `log` is what the user saw: ordered, timestamped, and cut -- every
    payload truncated for display at 15000 characters on the way *in*, so no
    complete copy exists even in Agent Zero's memory, and only the last 1000
    items survive a save. `agents[].history` is what the model was given: full
    text, no timestamps at all. The log is the event stream here; the history is
    joined onto it by recorded id, supplies the payload the model actually
    received wherever it can, and contributes its own events for everything the
    log never had or has since dropped.

  * **A tool call and its result are the same log item.** The item is created
    with `kvps` holding the arguments and an empty `content`, and the result is
    written into that same item when the tool returns. Two events come out of
    it, so the call and the result cannot be mispaired -- but the item's
    timestamp is the moment the call *started* and nothing anywhere records when
    it finished, so there is no duration to report.

  * **Subordinate agents share one log.** `call_subordinate` spawns a chain that
    all writes to the same list, distinguished only by `agentno`. That number is
    carried in `meta` and rendered as an `A1:` prefix in the outline, because a
    transcript that silently attributes a subordinate's tool calls to the
    top-level agent produces confident, wrong findings.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..elide import elide_middle
from ..models import NormalizedEvent, Session, SessionRef
from ..shape import compute_shape
from .base import Adapter, unparseable_event

# Log item types that are a tool call and its result in one item.
_TOOL_ITEM_TYPES = {"tool", "code_exe", "browser", "mcp", "subagent"}

# Log item types that are a turn in the conversation.
_MESSAGE_ITEM_TYPES = {"user": "user", "response": "assistant"}

# Everything else `helpers/log.py` can write: harness bookkeeping and failures.
_META_ITEM_TYPES = {"error", "warning", "hint", "info", "progress", "input", "util"}

# `kvps` keys that are Agent Zero's own bookkeeping rather than tool arguments.
_INTERNAL_KVPS = {"_tool_name", "step"}

# Agent Zero's two truncation markers. They are different claims and both are
# left in the text: the first says the *display* copy was cut and the model saw
# the whole thing, the second says the *payload* was cut before the model saw
# anything. Reading the number out of them is not the same as recovering an exit
# code from prose -- these are format strings with no natural-language variation,
# and the number they carry is a measurement of Agent Zero's own cut rather than
# a fact about the tool. A marker that does not match (the prompt file is
# user-editable) records nothing rather than a guess.
_LOG_TRUNCATION_RE = re.compile(r"<< (\d+) Characters hidden >>")
_PAYLOAD_TRUNCATION_RE = re.compile(r"<<\s*(\d+) CHARACTERS REMOVED TO SAVE SPACE\s*>>")

# `helpers/log.py`: content is cut at this many *characters*, 250000 for a
# `response` item. Recorded so a byte length sitting on the cap can be read as a
# cap rather than as a coincidence.
_LOG_CONTENT_CAP = 15000
_LOG_RESPONSE_CAP = 250000

# `helpers/persist_chat.py` saves `log.logs[-LOG_SIZE:]`.
_LOG_SIZE = 1000

# How much of an unreadable file to keep on the degraded event. Elided, and
# therefore marked, because an unmarked stub is indistinguishable from a file
# that really was that short.
_MAX_RAW_BYTES = 20_000

_SNIFF_BYTES = 8192


@dataclass
class _HistMessage:
    """One message out of `agents[].history`, in conversation order."""

    id: str
    ai: bool
    content: Any
    summary: str
    sequence: int
    metadata: dict[str, Any]
    tokens: int
    agent_no: int
    container: str


@dataclass
class _HistNote:
    """Something the history says about itself: a summary, or a collapsed run."""

    kind: str
    text: str
    fields: dict[str, Any]
    agent_no: int


@dataclass
class _History:
    """One agent's history, in the order the records are walked.

    Walk order, not `sequence` order: the summary Agent Zero inserts in place of
    a collapsed run is constructed without a sequence number and carries 0, so
    ordering by sequence would file it at the very start of the conversation
    instead of at the point where the messages it replaced used to be.
    """

    agent_no: int
    entries: list[Any] = field(default_factory=list)
    error: str | None = None
    raw: str = ""

    @property
    def messages(self) -> list[_HistMessage]:
        return [e for e in self.entries if isinstance(e, _HistMessage)]


class AgentZeroAdapter(Adapter):
    source = "agent-zero"

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots

    # -- discovery ------------------------------------------------------

    def default_roots(self) -> list[Path]:
        """Where Agent Zero keeps chats -- which nothing on the machine records.

        Every other supported CLI starts from a known directory under `$HOME`.
        Agent Zero computes its data directory from its own install location and
        offers no environment override, and its documented install
        (`docker run -v a0_usr:/a0/usr`) puts chats inside a named Docker volume
        that is not a host path at all on macOS or Windows.

        So the honest default is nearly empty: an Ossuary-side variable for
        people who know where their chats are, and the two locations a source
        checkout produces. Anything else is an explicit path. Guessing wider --
        sweeping `$HOME` for directories named `chats`, or reaching into the
        Docker socket -- would trade a visible "found nothing" for an invisible
        wrong answer.
        """
        configured = os.environ.get("OSSUARY_AGENT_ZERO_DIR")
        if configured:
            return [Path(configured).expanduser()]
        return [
            Path.cwd() / "usr" / "chats",
            Path.home() / "agent-zero" / "usr" / "chats",
        ]

    def claims(self, path: Path) -> bool:
        """Sniff the head for the keys `_serialize_context` writes first.

        The file is a single JSON object, so `head_records` -- which reads JSONL
        -- does not apply. `_serialize_context` writes `id`, `name`,
        `created_at`, `type`, `last_message`, `agents` in that order, so all of
        them are within the first few KB of any chat however large. Requiring
        `agents` alongside a timestamp field is enough: no other supported
        format has an `agents` list of objects, and Copilot -- the only other
        adapter that claims `.json` -- claims only under a directory named for
        chat sessions.
        """
        if path.suffix.lower() != ".json":
            return False
        head = _head_text(path, _SNIFF_BYTES)
        if '"agents"' not in head:
            return False
        return '"created_at"' in head or '"last_message"' in head

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
                # `chat.json` is the live record; `backups/*.json` are the full
                # exports `/compact` writes immediately before destroying it.
                # Without the second glob a compacted session reads as a chat
                # that only ever contained its own summary.
                candidates = sorted(
                    p
                    for p in root.rglob("*.json")
                    if p.name == "chat.json" or p.parent.name == "backups"
                )

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
                        session_id=_session_id_from_path(path),
                        source="agent-zero",
                        path=str(path),
                        size_bytes=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        # Discovery does not read the file, and nothing in the
                        # path names the project. `parse` fills it in.
                        project=None,
                    )
                )
        return refs

    # -- parsing --------------------------------------------------------

    def parse(self, ref: SessionRef) -> Session:
        path = Path(ref.path)
        session_id = ref.session_id or _session_id_from_path(path)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            session = Session(
                session_id=session_id,
                source="agent-zero",
                path=str(path),
                project=ref.project,
            )
            session.events.append(
                unparseable_event(
                    session_id=session_id,
                    source="agent-zero",
                    index=0,
                    raw="",
                    error=f"unreadable file: {exc}",
                )
            )
            session.parse_error_count = 1
            return session

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            data, load_error = None, str(exc)
        else:
            load_error = None
            if not isinstance(data, dict):
                data, load_error = None, f"expected an object, got {type(data).__name__}"

        if data is None:
            session = Session(
                session_id=session_id,
                source="agent-zero",
                path=str(path),
                project=ref.project,
                content_hash=self.file_hash(path),
                parse_error_count=1,
            )
            session.events.append(
                unparseable_event(
                    session_id=session_id,
                    source="agent-zero",
                    index=0,
                    raw=elide_middle(text, _MAX_RAW_BYTES),
                    error=f"chat file is not one JSON object: {load_error}",
                )
            )
            return session

        events: list[NormalizedEvent] = []
        parse_errors = 0

        histories = _read_histories(data)
        by_id: dict[str, _HistMessage] = {}
        for history in histories:
            for message in history.messages:
                # First writer wins: a duplicated id is pathological, and
                # silently rebinding the join to the later one would move a
                # payload onto the wrong event.
                by_id.setdefault(message.id, message)
        joined: set[str] = set()

        events.append(
            _header_event(data, session_id=session_id, index=0, path=path, histories=histories)
        )

        raw_items = data.get("log")
        raw_items = raw_items.get("logs") if isinstance(raw_items, dict) else None
        items = raw_items if isinstance(raw_items, list) else []
        if raw_items is not None and not isinstance(raw_items, list):
            events.append(
                unparseable_event(
                    session_id=session_id,
                    source="agent-zero",
                    index=len(events),
                    raw=_stringify(raw_items)[:2000],
                    error=f"log.logs was {type(raw_items).__name__}, not a list",
                )
            )
            parse_errors += 1

        dropped = _dropped_items_event(items, session_id=session_id, index=len(events))
        if dropped is not None:
            events.append(dropped)

        for position, item in enumerate(items):
            if not isinstance(item, dict):
                events.append(
                    unparseable_event(
                        session_id=session_id,
                        source="agent-zero",
                        index=len(events),
                        raw=_stringify(item)[:2000],
                        error=f"log item {position} was {type(item).__name__}, not an object",
                    )
                )
                parse_errors += 1
                continue
            try:
                produced = self._events_for_item(
                    item,
                    session_id=session_id,
                    next_index=len(events),
                    position=position,
                    by_id=by_id,
                    joined=joined,
                )
            except Exception as exc:  # noqa: BLE001 - never lose an item
                events.append(
                    unparseable_event(
                        session_id=session_id,
                        source="agent-zero",
                        index=len(events),
                        raw=_stringify(item)[:2000],
                        error=f"log item {position}: normalization failed: {exc!r}",
                    )
                )
                parse_errors += 1
                continue
            events.extend(produced)

        for history in histories:
            if history.error:
                events.append(
                    unparseable_event(
                        session_id=session_id,
                        source="agent-zero",
                        index=len(events),
                        raw=elide_middle(history.raw, _MAX_RAW_BYTES) if history.raw else "",
                        error=(
                            f"agent {history.agent_no} history did not parse: {history.error}"
                        ),
                    )
                )
                parse_errors += 1

        events.extend(
            _history_events(
                histories,
                session_id=session_id,
                next_index=len(events),
                joined=joined,
            )
        )

        return Session(
            session_id=session_id,
            source="agent-zero",
            path=str(path),
            events=events,
            content_hash=self.file_hash(path),
            parse_error_count=parse_errors,
            project=_project_for(data, ref),
        )

    # -- item -> events -------------------------------------------------

    def _events_for_item(
        self,
        item: dict[str, Any],
        *,
        session_id: str,
        next_index: int,
        position: int,
        by_id: dict[str, _HistMessage],
        joined: set[str],
    ) -> list[NormalizedEvent]:
        item_type = str(item.get("type") or "unknown")
        ts = self.parse_timestamp(item.get("timestamp"))
        kvps = item.get("kvps")
        kvps = kvps if isinstance(kvps, dict) else {}
        content = item.get("content")
        content = content if isinstance(content, str) else _stringify(content)
        heading = _strip_icon(str(item.get("heading") or ""))

        item_id = item.get("id")
        item_id = item_id if isinstance(item_id, str) and item_id else None
        hist = by_id.get(item_id) if item_id else None
        if hist is not None:
            joined.add(hist.id)

        meta = _item_meta(item, item_type, position, item_id, heading)

        if item_type in _TOOL_ITEM_TYPES:
            return _tool_events(
                session_id=session_id,
                next_index=next_index,
                ts=ts,
                item_type=item_type,
                kvps=kvps,
                content=content,
                hist=hist,
                meta=meta,
            )

        if item_type == "agent":
            return _agent_events(
                session_id=session_id,
                next_index=next_index,
                ts=ts,
                kvps=kvps,
                content=content,
                hist=hist,
                meta=meta,
            )

        if item_type in _MESSAGE_ITEM_TYPES:
            text, payload_source = _payload(hist, content)
            message_meta = {**meta, "payload_source": payload_source}
            message_meta.update(_truncation_fields(text, content, payload_source, item_type))
            message_meta.update(_usage_fields(hist))
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="agent-zero",
                    index=next_index,
                    ts=ts,
                    role=_MESSAGE_ITEM_TYPES[item_type],
                    kind="message",
                    text=text,
                    meta=message_meta,
                )
            ]

        # `error` and `warning` are how every failure Agent Zero survives is
        # recorded -- there is no per-result error flag anywhere in the format.
        # The type leads the text so the failure is legible in the outline's
        # preview column, which is the only place a `meta` row says anything.
        if item_type in _META_ITEM_TYPES:
            return [
                NormalizedEvent(
                    session_id=session_id,
                    source="agent-zero",
                    index=next_index,
                    ts=ts,
                    role="system",
                    kind="meta",
                    text=_labelled(item_type, heading, content),
                    meta=meta,
                )
            ]

        return [
            NormalizedEvent(
                session_id=session_id,
                source="agent-zero",
                index=next_index,
                ts=ts,
                role="unknown",
                kind="meta",
                text=_labelled(item_type, heading, content),
                meta={**meta, "unknown_item_type": True},
                parse_error=f"unrecognized log item type {item_type!r}",
            )
        ]


# -- log item helpers ---------------------------------------------------


def _item_meta(
    item: dict[str, Any],
    item_type: str,
    position: int,
    item_id: str | None,
    heading: str,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"item_type": item_type, "item_position": position}
    if item_id:
        meta["item_id"] = item_id
    if heading:
        meta["heading"] = heading

    number = item.get("no")
    if isinstance(number, int) and not isinstance(number, bool):
        meta["item_no"] = number

    # `agentno` is `agent_number` in files written by older versions. Recorded
    # only when non-zero: 0 is the top-level agent and needs no marking.
    agent_no = item.get("agentno")
    if agent_no is None:
        agent_no = item.get("agent_number")
    if isinstance(agent_no, int) and not isinstance(agent_no, bool) and agent_no:
        meta["agent_no"] = agent_no
    return meta


def _tool_events(
    *,
    session_id: str,
    next_index: int,
    ts: datetime | None,
    item_type: str,
    kvps: dict[str, Any],
    content: str,
    hist: _HistMessage | None,
    meta: dict[str, Any],
) -> list[NormalizedEvent]:
    """One log item, two events: the arguments and the result.

    The pairing is by identity rather than by a join or by position, because
    Agent Zero writes both halves into the same item -- `Tool.get_log_object`
    creates it with the arguments and `Tool.after_execution` writes the result
    into it. Nothing can be mispaired, and nothing can be orphaned.

    No duration, ever. The item's `timestamp` is stamped when the item is
    created and `_update_item` never touches it, so the only available estimate
    is the gap to whatever the harness logged next -- which is not when the tool
    returned. That gap is in `meta` as `next_item_gap_ms` for anyone who wants
    it; it is not in the duration column, where it would be read as a
    measurement of the tool.
    """
    tool_name = _tool_name_for(item_type, kvps, hist)
    arguments = {k: v for k, v in kvps.items() if k not in _INTERNAL_KVPS}

    call = NormalizedEvent(
        session_id=session_id,
        source="agent-zero",
        index=next_index,
        ts=ts,
        role="assistant",
        kind="tool_call",
        tool_name=tool_name,
        text=_stringify(arguments),
        meta=dict(meta),
    )

    text, payload_source = _payload(hist, content)
    result_meta: dict[str, Any] = {
        **meta,
        "call_event_index": next_index,
        "payload_source": payload_source,
    }
    result_meta.update(_truncation_fields(text, content, payload_source, item_type))
    result_meta.update(_usage_fields(hist))

    result = NormalizedEvent(
        session_id=session_id,
        source="agent-zero",
        index=next_index + 1,
        ts=ts,
        role="user",
        kind="tool_result",
        tool_name=tool_name,
        text=text,
        shape=compute_shape(
            text,
            duration_ms=None,
            # The code execution tool retrieves an exit code when its shell dies
            # and renders it into a framework sentence ("with exit code 1").
            # Recovering it means matching prose that changes between releases,
            # and a wrong parse would be indistinguishable from a recorded code.
            exit_code=None,
            # Agent Zero has no per-result error flag. Failures are separate
            # `error` items, which appear as their own rows.
            has_error_field=False,
            duration_source="unavailable",
        ),
        meta=result_meta,
    )
    return [call, result]


def _agent_events(
    *,
    session_id: str,
    next_index: int,
    ts: datetime | None,
    kvps: dict[str, Any],
    content: str,
    hist: _HistMessage | None,
    meta: dict[str, Any],
) -> list[NormalizedEvent]:
    """One LLM turn, which is a thinking event and a message event.

    The reasoning stream is in `kvps.reasoning` and nowhere else -- not in the
    content, not in the history. Agent Zero is the only supported source that
    persists the provider's reasoning as text rather than as a signature.

    `kvps.tool_name` is the tool the model *decided* to call. The record of the
    call is the separate tool item that follows, which is where the arguments
    and the result actually live, so this is `intended_tool` in `meta` rather
    than a `tool_call` event that would double-count every call in the corpus
    statistics.
    """
    events: list[NormalizedEvent] = []

    reasoning = kvps.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        events.append(
            NormalizedEvent(
                session_id=session_id,
                source="agent-zero",
                index=next_index,
                ts=ts,
                role="assistant",
                kind="thinking",
                text=reasoning,
                meta=dict(meta),
            )
        )

    text, payload_source = _payload(hist, content)
    message_meta: dict[str, Any] = {**meta, "payload_source": payload_source}
    for key, name in (
        ("headline", "headline"),
        ("tool_name", "intended_tool"),
        ("thoughts", "thoughts"),
    ):
        if key in kvps and kvps[key] not in (None, "", [], {}):
            message_meta[name] = kvps[key]

    # A turn that died mid-stream has parsed thoughts and no text. Falling back
    # to them keeps the row from reading as a turn that produced nothing, which
    # is a different failure with a different cause.
    if not text.strip() and "thoughts" in message_meta:
        text = _stringify(message_meta["thoughts"])
        message_meta["text_from"] = "thoughts"

    message_meta.update(_truncation_fields(text, content, payload_source, "agent"))
    message_meta.update(_usage_fields(hist))

    events.append(
        NormalizedEvent(
            session_id=session_id,
            source="agent-zero",
            index=next_index + len(events),
            ts=ts,
            role="assistant",
            kind="message",
            text=text,
            meta=message_meta,
        )
    )
    return events


def _tool_name_for(
    item_type: str, kvps: dict[str, Any], hist: _HistMessage | None
) -> str | None:
    """The tool's name, from a field that records it or not at all.

    Most tools put `_tool_name` in `kvps`, and MCP tools put `tool_name` there.
    The six that override `get_log_object()` -- code execution, browser,
    subordinate, skills, wait, office -- record it nowhere except inside the
    heading string, which is a human-readable sentence and therefore not a
    source of facts the corpus statistics will aggregate.

    So where the history join answers it, the history answers it; where nothing
    does, the item's own type becomes an explicitly-bracketed bucket. `<code_exe>`
    says "a code execution item whose tool was not recorded", which is true,
    rather than naming a specific tool that may not be the one that ran.
    """
    if hist is not None and isinstance(hist.content, dict):
        name = hist.content.get("tool_name")
        if isinstance(name, str) and name:
            return name
    for key in ("_tool_name", "tool_name"):
        name = kvps.get(key)
        if isinstance(name, str) and name:
            return name
    return f"<{item_type}>"


def _payload(hist: _HistMessage | None, content: str) -> tuple[str, str]:
    """The text to show and measure, and which record it came from.

    The history holds what the model was given; the log holds a copy cut at
    15000 characters for the browser. Measuring the log copy and reporting it as
    a tool's output would put Agent Zero's UI limit into `ossuary_tool_stats` as
    though it were a tool's own behaviour -- a phantom cap indistinguishable
    from Claude Code's real 30000-byte one. So the history wins wherever the
    join reaches it, and `payload_source` says which was measured.
    """
    if hist is not None:
        text = _content_text(hist.content)
        if text:
            return text, "history"
    return content, "log"


def _truncation_fields(
    text: str, content: str, payload_source: str, item_type: str
) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    payload_cut = _PAYLOAD_TRUNCATION_RE.search(text)
    if payload_cut:
        # Agent Zero cut this before the model saw it.
        fields["payload_truncation"] = {"chars_removed": int(payload_cut.group(1))}

    if payload_source == "log":
        log_cut = _LOG_TRUNCATION_RE.search(content)
        if log_cut:
            fields["log_truncation"] = {
                "chars_hidden": int(log_cut.group(1)),
                "cap_chars": (
                    _LOG_RESPONSE_CAP if item_type == "response" else _LOG_CONTENT_CAP
                ),
            }
    return fields


def _usage_fields(hist: _HistMessage | None) -> dict[str, Any]:
    """Token accounting, which exists only on the history side."""
    if hist is None:
        return {}
    fields: dict[str, Any] = {}
    if hist.tokens:
        fields["approx_tokens"] = hist.tokens
    responses = hist.metadata.get("responses") if isinstance(hist.metadata, dict) else None
    if isinstance(responses, dict):
        # `output_items` is the provider's raw item list and can be the size of
        # the conversation. The accounting is the part worth carrying.
        for key in ("usage", "provider_model_key", "mode", "state"):
            value = responses.get(key)
            if value not in (None, "", {}, []):
                fields.setdefault("responses", {})[key] = value
    return fields


def _dropped_items_event(
    items: list[Any], *, session_id: str, index: int
) -> NormalizedEvent | None:
    """Say so when Agent Zero's 1000-item cap ate the start of the session.

    `_serialize_log` writes `log.logs[-1000:]` with no marker of any kind. `no`
    is the item's ordinal as it was when written, so a first item numbered 812
    proves 812 items are gone and says exactly how many.

    The catch is real and belongs in the event text rather than in this
    docstring: `_deserialize_log` renumbers from zero when Agent Zero loads a
    chat at startup, so a chat that has survived a restart has had the evidence
    erased. A non-zero first `no` is proof of loss. A zero first `no` is not
    proof of completeness.
    """
    first = next((item for item in items if isinstance(item, dict)), None)
    if first is None:
        return None
    number = first.get("no")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    return NormalizedEvent(
        session_id=session_id,
        source="agent-zero",
        index=index,
        ts=None,
        role="system",
        kind="meta",
        text=(
            f"{number} earlier log item(s) are not in this file. Agent Zero keeps "
            f"the last {_LOG_SIZE} items when it saves a chat and writes no marker "
            f"when it drops the rest; the count is the first surviving item's own "
            f"ordinal. Note that this evidence is erased when Agent Zero reloads a "
            f"chat, which renumbers items from zero -- so this number is a floor, "
            f"and its absence in another session is not proof that nothing was lost."
        ),
        meta={"log_items_dropped": number, "log_size_cap": _LOG_SIZE},
    )


def _header_event(
    data: dict[str, Any],
    *,
    session_id: str,
    index: int,
    path: Path,
    histories: list[_History],
) -> NormalizedEvent:
    parts = [f"chat {data.get('name') or '(unnamed)'}"]
    context_type = data.get("type")
    if isinstance(context_type, str) and context_type:
        parts.append(f"type={context_type}")
    profile = data.get("agent_profile")
    if isinstance(profile, str) and profile:
        parts.append(f"profile={profile}")
    if len(histories) > 1:
        parts.append(f"agents={len(histories)}")
    if path.parent.name == "backups":
        parts.append("pre-compaction backup")

    meta: dict[str, Any] = {"chat_name": data.get("name")}
    for key in ("id", "type", "agent_profile", "created_at", "last_message"):
        value = data.get(key)
        if value not in (None, ""):
            meta["context_id" if key == "id" else key] = value
    if path.parent.name == "backups":
        # The live chat.json of a compacted session holds only its summary. This
        # file is the record that existed at the moment compaction ran.
        meta["pre_compaction_backup"] = True
    if len(histories) > 1:
        meta["agent_count"] = len(histories)
    meta["project_source"] = _project_source(data)

    return NormalizedEvent(
        session_id=session_id,
        source="agent-zero",
        index=index,
        # `created_at` is ISO with the user's own offset; `parse_timestamp`
        # normalises it to UTC like everything else.
        ts=Adapter.parse_timestamp(data.get("created_at")),
        role="system",
        kind="meta",
        text=" ".join(parts),
        meta=meta,
    )


# -- history ------------------------------------------------------------


def _read_histories(data: dict[str, Any]) -> list[_History]:
    """Parse every agent's history, which is JSON inside the JSON.

    `History.serialize()` returns `json.dumps(...)` and `_serialize_agent` stores
    that string, so this is a second parse on content that has already survived
    one. A history that does not parse degrades to an event carrying its text --
    it never takes the session down with it.
    """
    agents = data.get("agents")
    if not isinstance(agents, list):
        return []

    histories: list[_History] = []
    for position, agent in enumerate(agents):
        if not isinstance(agent, dict):
            continue
        number = agent.get("number")
        agent_no = number if isinstance(number, int) and not isinstance(number, bool) else position

        raw = agent.get("history")
        history = _History(agent_no=agent_no)
        if isinstance(raw, dict):
            parsed: Any = raw
        elif isinstance(raw, str) and raw.strip():
            history.raw = raw
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                history.error = str(exc)
                histories.append(history)
                continue
        else:
            histories.append(history)
            continue

        if not isinstance(parsed, dict):
            history.error = f"expected an object, got {type(parsed).__name__}"
            histories.append(history)
            continue

        try:
            _walk_history(parsed, history)
        except Exception as exc:  # noqa: BLE001 - a history is not worth a session
            history.error = f"walk failed: {exc!r}"
        histories.append(history)

    return histories


def _walk_history(parsed: dict[str, Any], history: _History) -> None:
    """Conversation order is bulks, then topics, then the current topic."""
    for record in _as_list(parsed.get("bulks")):
        _walk_record(record, history, container="bulk")
    for record in _as_list(parsed.get("topics")):
        _walk_record(record, history, container="topic")
    current = parsed.get("current")
    if isinstance(current, dict):
        _walk_record(current, history, container="current")


def _walk_record(record: Any, history: _History, *, container: str) -> None:
    if not isinstance(record, dict):
        return
    cls = record.get("_cls")

    if cls == "Bulk":
        summary = record.get("summary")
        if isinstance(summary, str) and summary.strip():
            history.entries.append(
                _HistNote(
                    kind="bulk_summary",
                    text=summary,
                    fields={"record_count": len(_as_list(record.get("records")))},
                    agent_no=history.agent_no,
                )
            )
        for nested in _as_list(record.get("records")):
            _walk_record(nested, history, container="bulk")
        return

    if cls == "Topic":
        summary = record.get("summary")
        if isinstance(summary, str) and summary.strip():
            history.entries.append(
                _HistNote(
                    kind="topic_summary",
                    text=summary,
                    fields={"message_count": len(_as_list(record.get("messages")))},
                    agent_no=history.agent_no,
                )
            )
        _walk_messages(_as_list(record.get("messages")), history, container=container)
        return

    if cls == "Message":
        _walk_messages([record], history, container=container)
        return

    # An unrecognised record class is still somebody's conversation. Walk what
    # looks walkable rather than dropping the subtree.
    for key in ("records", "messages"):
        for nested in _as_list(record.get(key)):
            _walk_record(nested, history, container=container)


def _walk_messages(
    messages: list[Any], history: _History, *, container: str
) -> None:
    """Messages in order, with a note wherever a run of them is missing.

    `sequence` is a counter over every message the history has ever held, and
    topics partition it in order, so consecutive messages carry consecutive
    numbers. A jump is `Topic.compress_attention` having replaced a run of
    messages with one summary -- the only path in Agent Zero that deletes rather
    than shadows -- and the size of the jump is the number of messages it took.

    The inserted summary itself carries `sequence` 0, because it is constructed
    without one. That is how it is told apart from a message that was written.
    """
    previous: int | None = None
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        sequence = raw.get("sequence")
        sequence = sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0

        inserted = sequence == 0 and previous is not None
        if sequence and previous is not None and sequence > previous + 1:
            history.entries.append(
                _HistNote(
                    kind="collapsed",
                    text=(
                        f"{sequence - previous - 1} message(s) between sequence "
                        f"{previous} and {sequence} are not in this file. Agent Zero "
                        f"summarised that stretch of the conversation and replaced "
                        f"the originals with the summary; they are the only text "
                        f"here that compression deleted rather than shadowed."
                    ),
                    fields={
                        "messages_deleted": sequence - previous - 1,
                        "sequence_from": previous,
                        "sequence_to": sequence,
                    },
                    agent_no=history.agent_no,
                )
            )

        message_id = raw.get("id")
        history.entries.append(
            _HistMessage(
                id=message_id if isinstance(message_id, str) else "",
                ai=bool(raw.get("ai")),
                content=raw.get("content"),
                summary=raw.get("summary") if isinstance(raw.get("summary"), str) else "",
                sequence=sequence,
                metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
                tokens=raw.get("tokens") if isinstance(raw.get("tokens"), int) else 0,
                agent_no=history.agent_no,
                container="inserted_summary" if inserted else container,
            )
        )
        if sequence:
            previous = sequence


def _history_events(
    histories: list[_History],
    *,
    session_id: str,
    next_index: int,
    joined: set[str],
) -> list[NormalizedEvent]:
    """What the history holds that the log does not.

    Two kinds of thing land here. Messages the log never had or has since
    dropped -- a session whose first 800 items fell off the 1000-item cap still
    has them here, and emitting only the log would lose them. And the history's
    statements about itself: summaries that now stand in for text the model
    stopped being shown, and the runs of messages that compression deleted.

    None of it has a timestamp. History records carry `sequence` and nothing
    else, so interleaving them into the log's timeline would mean inventing
    times for them. They go at the end, in conversation order, and the outline
    legend says what they are.
    """
    events: list[NormalizedEvent] = []

    for history in histories:
        for entry in history.entries:
            if isinstance(entry, _HistMessage) and entry.id and entry.id in joined:
                continue
            index = next_index + len(events)
            if isinstance(entry, _HistNote):
                events.append(
                    NormalizedEvent(
                        session_id=session_id,
                        source="agent-zero",
                        index=index,
                        ts=None,
                        role="system",
                        kind="meta",
                        text=_labelled(entry.kind, "", entry.text),
                        meta={
                            "from_history": True,
                            "history_note": entry.kind,
                            **entry.fields,
                            **({"agent_no": entry.agent_no} if entry.agent_no else {}),
                        },
                    )
                )
                continue

            events.append(_history_message_event(entry, session_id=session_id, index=index))

    return events


def _history_message_event(
    message: _HistMessage, *, session_id: str, index: int
) -> NormalizedEvent:
    is_tool_result = isinstance(message.content, dict) and "tool_result" in message.content
    text = _content_text(message.content)

    meta: dict[str, Any] = {
        "from_history": True,
        "sequence": message.sequence,
        "history_container": message.container,
    }
    if message.agent_no:
        meta["agent_no"] = message.agent_no
    if message.summary:
        # `set_summary` shadows: `output()` shows the summary, `to_dict` keeps
        # both. The original is the text above; this is what the model is being
        # shown in its place, and the difference is the point.
        meta["shadowed_by_summary"] = message.summary
    meta.update(_usage_fields(message))
    meta.update(_truncation_fields(text, text, "history", "history"))

    if is_tool_result:
        tool_name = message.content.get("tool_name")
        return NormalizedEvent(
            session_id=session_id,
            source="agent-zero",
            index=index,
            ts=None,
            role="user",
            kind="tool_result",
            tool_name=tool_name if isinstance(tool_name, str) and tool_name else None,
            text=text,
            shape=compute_shape(text, duration_source="unavailable"),
            # No log item claimed this result, so no call event exists for it.
            meta={**meta, "orphan_result": True},
        )

    return NormalizedEvent(
        session_id=session_id,
        source="agent-zero",
        index=index,
        ts=None,
        role="assistant" if message.ai else "user",
        kind="message",
        text=text,
        meta=meta,
    )


# -- text helpers -------------------------------------------------------


def _content_text(content: Any) -> str:
    """One history message's content as text.

    Content is structured rather than rendered: a tool result is
    `{tool_name, tool_result}`, a user message is the framework's
    `{system_message, user_message, attachments}` template, an assistant
    response is the raw string the model emitted.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_content_text(item) for item in content)
    if not isinstance(content, dict):
        return _stringify(content)

    if "raw_content" in content:
        return _raw_message_text(content)

    if "tool_result" in content:
        return _stringify(content.get("tool_result"))

    if "user_message" in content or "system_message" in content:
        parts: list[str] = []
        for key in ("system_message", "user_message", "attachments"):
            value = content.get(key)
            if value in (None, "", [], {}):
                continue
            rendered = _stringify(value)
            parts.append(rendered if key == "user_message" else f"{key}: {rendered}")
        return "\n".join(parts)

    return _stringify(content)


def _raw_message_text(content: dict[str, Any]) -> str:
    """A multimodal message, with its embeds named rather than inlined.

    A pasted screenshot is megabytes of base64 that `trim_embeds` only ever
    shadows -- it stays in the file forever. A shape record measuring that would
    report the encoding rather than anything the model reasoned about.
    """
    raw = content.get("raw_content")
    parts: list[str] = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, dict) and item.get("type") == "image_url":
            data = _stringify(item.get("image_url"))
            parts.append(f"[image_url base64={len(data)}]")
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif item is not None:
            parts.append(_stringify(item))
    if not parts and isinstance(content.get("preview"), str):
        return content["preview"]
    return "\n".join(parts)


def _labelled(label: str, heading: str, content: str) -> str:
    parts = [p for p in (label, heading, content) if p]
    if not parts:
        return label
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]}: " + " -- ".join(parts[1:])


def _strip_icon(heading: str) -> str:
    """Headings start with an `icon://name ` sentinel meant for the browser."""
    if heading.startswith("icon://"):
        _, _, rest = heading.partition(" ")
        return rest
    return heading


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _head_text(path: Path, limit: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


# -- path helpers -------------------------------------------------------


def _session_id_from_path(path: Path) -> str:
    """The id is derived from the path, not from the `id` field inside.

    A pre-compaction backup carries the same `id` as the chat it was cut from,
    so trusting the field would collide two sessions onto one name. The
    directory name is also what the user sees in the UI. The file's own `id` is
    kept as `context_id` on the header event.
    """
    if path.name == "chat.json":
        return path.parent.name or path.stem
    if path.parent.name == "backups":
        return f"{path.parent.parent.name}@{path.stem}"
    return path.stem


def _project_source(data: dict[str, Any]) -> str:
    context_data = data.get("data")
    if isinstance(context_data, dict) and str(context_data.get("project") or "").strip():
        return "data.project"
    if str(data.get("name") or "").strip():
        return "chat_name"
    return "none"


def _project_for(data: dict[str, Any], ref: SessionRef) -> str | None:
    """The project folder this chat is bound to, if any -- else its title.

    Agent Zero has no per-session working directory the way the other four
    sources do. `data.project` is the nearest thing: the project folder a chat
    was bound to. Where there is none, the chat's own name is what a human would
    recognise it by, and `project_is_chat_name` on the header event says which
    of the two this is rather than letting a title pass as a path.
    """
    context_data = data.get("data")
    if isinstance(context_data, dict):
        project = context_data.get("project")
        if isinstance(project, str) and project.strip():
            return project
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name
    return ref.project
