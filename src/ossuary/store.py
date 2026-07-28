"""Session store -- discovery, parsing, and the read paths Agent A's tools use.

Everything the agent can see about a session goes through here, which is also
where redaction and labelled elision are enforced. Tools call these methods
rather than touching adapters or files directly, so there is exactly one place
where payload text can be shortened and exactly one place where it can be
masked.
"""

from __future__ import annotations

import re
from pathlib import Path

from .adapters import ALL_SOURCES, get_adapter
from .elide import elide_middle, elide_tail, marker
from .models import NormalizedEvent, Session, SessionRef
from .outline import render_outline
from .redact import Redactor

# Per-event budget for `read_events`. Generous enough that ordinary results
# arrive whole, small enough that one oversized payload cannot fill the window.
DEFAULT_EVENT_BUDGET = 4000
DEFAULT_SLICE_LIMIT = 8000
MAX_SEARCH_MATCHES = 200


class SessionStore:
    """Loads sessions once, serves them many times."""

    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        roots: list[Path] | None = None,
    ) -> None:
        self.redactor = redactor or Redactor()
        self.roots = roots
        self._sessions: dict[str, Session] = {}
        self._refs: dict[str, SessionRef] = {}

    # -- discovery ------------------------------------------------------

    def discover(
        self, sources: list[str] | None = None, roots: list[Path] | None = None
    ) -> list[SessionRef]:
        wanted = sources or list(ALL_SOURCES)
        # Naming exactly one source is an explicit instruction to read the files
        # that way, so the content sniff is skipped. With several sources in
        # play, each adapter takes only what it recognises -- otherwise an
        # explicit path makes every adapter claim every transcript.
        require_claim = len(wanted) > 1
        refs: list[SessionRef] = []
        for source in wanted:
            adapter = get_adapter(source, roots=roots or self.roots)
            for ref in adapter.discover(roots or self.roots, require_claim=require_claim):
                refs.append(ref)
                self._refs[ref.session_id] = ref
        return refs

    # -- loading --------------------------------------------------------

    def load(self, ref: SessionRef) -> Session:
        cached = self._sessions.get(ref.session_id)
        if cached is not None:
            return cached
        adapter = get_adapter(ref.source)
        session = adapter.parse(ref)
        self._sessions[session.session_id] = session
        self._refs.setdefault(session.session_id, ref)
        return session

    def add(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    @property
    def sessions(self) -> list[Session]:
        return list(self._sessions.values())

    # -- read paths used by Agent A's tools ------------------------------

    def outline(self, session_id: str) -> str:
        session = self._require(session_id)
        return self.redactor.redact_text(render_outline(session))

    def read_events(
        self,
        session_id: str,
        start: int,
        end: int,
        *,
        per_event_budget: int = DEFAULT_EVENT_BUDGET,
    ) -> str:
        """Full events by index range, inclusive of `start`, exclusive of `end`."""
        session = self._require(session_id)
        if start < 0:
            start = 0
        if end <= start:
            end = start + 1

        selected = [e for e in session.events if start <= e.index < end]
        if not selected:
            total = len(session.events)
            return (
                f"No events in range [{start}, {end}) for session {session_id}. "
                f"This session has {total} events, indices 0..{max(0, total - 1)}."
            )

        blocks = [
            f"SESSION {session_id}  events [{start}, {end})  returned {len(selected)}"
        ]
        for event in selected:
            blocks.append(self._render_event(event, per_event_budget))
        return self.redactor.redact_text("\n\n".join(blocks))

    def read_event_slice(
        self,
        session_id: str,
        event_index: int,
        offset: int = 0,
        limit: int = DEFAULT_SLICE_LIMIT,
    ) -> str:
        """Paged read of one oversized payload.

        Reports the true total length and the exact window returned, so the agent
        can page deterministically and always knows how much it has not seen.
        """
        session = self._require(session_id)
        event = session.by_index(event_index)
        if event is None:
            total = len(session.events)
            return (
                f"No event at index {event_index} in session {session_id}. "
                f"Valid indices are 0..{max(0, total - 1)}."
            )

        text = event.text or event.raw or ""
        raw = text.encode("utf-8", errors="surrogatepass")
        total_bytes = len(raw)

        if offset < 0:
            offset = 0
        if offset >= total_bytes:
            return (
                f"SESSION {session_id} event {event_index}: offset {offset} is past "
                f"the end of this payload ({total_bytes} bytes)."
            )

        limit = max(1, min(limit, DEFAULT_SLICE_LIMIT))
        window = raw[offset : offset + limit]
        chunk = window.decode("utf-8", errors="replace")
        end = offset + len(window)
        remaining = total_bytes - end

        header = (
            f"SESSION {session_id} event {event_index} "
            f"({event.kind}{'/' + event.tool_name if event.tool_name else ''}) "
            f"bytes [{offset}, {end}) of {total_bytes}"
        )
        body = [header, chunk]
        if offset > 0:
            body.insert(1, marker(offset, total_bytes) + "  (before this window)")
        if remaining > 0:
            body.append(marker(remaining, total_bytes) + "  (after this window)")
        return self.redactor.redact_text("\n".join(body))

    def search_session(
        self, session_id: str, pattern: str, *, max_matches: int = MAX_SEARCH_MATCHES
    ) -> str:
        """Regex over session text. Returns matching event indices with context."""
        session = self._require(session_id)
        try:
            compiled = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            return f"Invalid regular expression {pattern!r}: {exc}"

        hits: list[str] = []
        matched_events = 0
        for event in session.events:
            haystack = event.text or event.raw or ""
            if not haystack:
                continue
            found = list(compiled.finditer(haystack))
            if not found:
                continue
            matched_events += 1
            for match in found[:3]:
                hits.append(
                    f"  event {event.index} ({event.kind}"
                    f"{'/' + event.tool_name if event.tool_name else ''}) "
                    f"at byte {match.start()}: {_context(haystack, match)}"
                )
            if len(found) > 3:
                hits.append(f"  event {event.index}: +{len(found) - 3} more match(es)")
            if len(hits) >= max_matches:
                hits.append(
                    f"  [[ossuary:elided remaining matches; {max_matches} shown]] "
                    f"Narrow the pattern or use read_events to continue."
                )
                break

        if not hits:
            return f"No matches for {pattern!r} in session {session_id}."
        header = (
            f"{matched_events} event(s) matched {pattern!r} in session {session_id}:"
        )
        return self.redactor.redact_text("\n".join([header, *hits]))

    # -- internals ------------------------------------------------------

    def _require(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            known = ", ".join(sorted(self._sessions)[:5])
            raise KeyError(
                f"session {session_id!r} is not loaded. Loaded sessions: {known or 'none'}"
            )
        return session

    def _render_event(self, event: NormalizedEvent, budget: int) -> str:
        header = [
            f"--- event {event.index} ---",
            f"role={event.role} kind={event.kind}"
            + (f" tool={event.tool_name}" if event.tool_name else "")
            + (f" ts={event.ts.isoformat()}" if event.ts else ""),
        ]
        if event.shape is not None:
            shape = event.shape
            header.append(
                f"shape: bytes={shape.byte_length} "
                f"duration={_fmt_duration(shape)} "
                f"exit={shape.exit_code if shape.exit_code is not None else '-'} "
                f"error_field={shape.has_error_field} "
                f"terminates_cleanly={shape.terminates_cleanly} "
                f"round={shape.is_round_number} empty={shape.is_empty} "
                f"hash={shape.content_hash}"
            )
        if event.parse_error:
            header.append(f"parse_error: {event.parse_error}")
        if event.meta.get("orphan_result"):
            header.append(
                "note: this result has no matching tool call in the transcript"
            )

        body = event.text
        if not body and event.raw:
            header.append("note: showing raw line because no text was recoverable")
            body = event.raw
        if body:
            body = elide_middle(body, budget)
        return "\n".join([*header, "", body])


def _fmt_duration(shape) -> str:  # type: ignore[no-untyped-def]
    if shape.duration_ms is None:
        return "-"
    suffix = {"recorded": "", "derived": " (derived)", "unavailable": ""}[
        shape.duration_source
    ]
    return f"{shape.duration_ms}ms{suffix}"


def _context(haystack: str, match: re.Match[str], width: int = 80) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(haystack), match.end() + width // 2)
    snippet = " ".join(haystack[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(haystack) else ""
    return f"{prefix}{snippet}{suffix}"


__all__ = ["SessionStore", "elide_tail", "DEFAULT_EVENT_BUDGET"]
