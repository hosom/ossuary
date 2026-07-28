"""Adapter contract.

Transcript formats are undocumented internal implementation details of the CLIs
that write them. They change between releases and carry no version field. So
adapters parse like archaeologists, not validators:

  * Never reject a line. Whatever is on disk is the only record that will ever
    exist of that session.
  * A line that fails to parse becomes an `unparseable` event carrying the raw
    text and the error, and parsing continues.
  * Unrecognised fields go into `meta` rather than being dropped.

The cost of a validator that skips bad input is a silently short session that
nobody notices. The cost of a degraded event is a visible row in the outline.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..models import NormalizedEvent, Session, SessionRef, Source


class Adapter(ABC):
    """Turns one on-disk transcript into a `Session` of `NormalizedEvent`s."""

    source: Source

    @abstractmethod
    def discover(
        self, roots: list[Path] | None = None, *, require_claim: bool = True
    ) -> list[SessionRef]:
        """Find candidate session files. Must not read them fully.

        When `require_claim` is set, only files this adapter recognises are
        returned. That matters when the user passes an explicit path with no
        `--source`: without it every adapter globs the same directory and the
        same transcript is parsed two or three different ways. Passing an
        explicit `--source` clears the flag, so a file can always be forced
        through a chosen adapter.
        """

    def claims(self, path: Path) -> bool:
        """Cheap content sniff: could this adapter plausibly read this file?

        Reads only the head of the file. Errs toward claiming -- a false accept
        yields a degraded parse, which is visible, while a false reject makes a
        session silently disappear.
        """
        return True

    @staticmethod
    def head_records(path: Path, limit: int = 8) -> list[dict]:
        """First few JSON objects from a JSONL file, for sniffing."""
        import json

        records: list[dict] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if len(records) >= limit:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            return []
        return records

    @abstractmethod
    def parse(self, ref: SessionRef) -> Session:
        """Read and normalize one session. Must never raise on malformed input."""

    # -- shared helpers -------------------------------------------------

    @staticmethod
    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]

    @staticmethod
    def parse_timestamp(value: object) -> datetime | None:
        """Best-effort timestamp parsing. Returns None rather than raising."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                # Heuristic: values past ~2001 in ms are too large to be seconds.
                seconds = value / 1000.0 if value > 1e11 else float(value)
                return datetime.fromtimestamp(seconds)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                pass
            for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
        return None


def unparseable_event(
    *,
    session_id: str,
    source: Source,
    index: int,
    raw: str,
    error: str,
    ts: datetime | None = None,
) -> NormalizedEvent:
    """A line we could not read, preserved rather than discarded."""
    return NormalizedEvent(
        session_id=session_id,
        source=source,
        index=index,
        ts=ts,
        role="unknown",
        kind="unparseable",
        text="",
        raw=raw,
        parse_error=error,
    )
