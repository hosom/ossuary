"""Content-addressed cache under `.ossuary/cache/`.

Two separately-keyed layers, which is the point:

  * tool responses -- `hash(session_file_content) + hash(call_args)`
  * final issue lists -- `hash(session_file_content) + prompt_version + model`

So editing a prompt re-runs inference without re-paying for I/O, and a session
whose file has not changed costs nothing at all on re-scan. Because the session
key is the file's *content* hash rather than its mtime, a transcript that was
touched but not modified stays cached, and one that was rewritten in place does
not.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CACHE_DIRNAME = "cache"


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Cache:
    """A two-level JSON cache. Never raises on a corrupt entry -- it just misses."""

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root) / CACHE_DIRNAME
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0

    # -- generic -------------------------------------------------------

    def _path(self, namespace: str, key: str) -> Path:
        # Shard by the first two characters so a corpus of thousands of sessions
        # does not put thousands of entries in one directory.
        return self.root / namespace / key[:2] / f"{key}.json"

    def get(self, namespace: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # A half-written or corrupt entry is a miss, never an error. The
            # cache is an optimisation; it must not be able to fail a run.
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot leave a torn entry
            # that a later run would read as valid.
            temp = path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8"
            )
            temp.replace(path)
            self.writes += 1
        except (OSError, TypeError, ValueError):
            pass

    # -- typed keys ----------------------------------------------------

    @staticmethod
    def tool_key(session_content_hash: str, tool_name: str, call_args: dict[str, Any]) -> str:
        return f"{session_content_hash}-{tool_name}-{_stable_hash(call_args)}"

    @staticmethod
    def issues_key(
        session_content_hash: str,
        prompt_version: str,
        model: str,
        *,
        schema_version: int,
        redacted: bool,
        source: str = "",
    ) -> str:
        # `source` is part of the key because one file can legitimately be read
        # by more than one adapter (an explicit path with no --source), and two
        # adapters produce different events from identical bytes. Keying on
        # content alone would serve one adapter's findings for the other's.
        return _stable_hash(
            {
                "session": session_content_hash,
                "prompt": prompt_version,
                "model": model,
                "schema": schema_version,
                "redacted": redacted,
                "source": source,
            }
        )

    def get_tool_response(
        self, session_content_hash: str, tool_name: str, call_args: dict[str, Any]
    ) -> str | None:
        value = self.get("tools", self.tool_key(session_content_hash, tool_name, call_args))
        return value if isinstance(value, str) else None

    def set_tool_response(
        self,
        session_content_hash: str,
        tool_name: str,
        call_args: dict[str, Any],
        response: str,
    ) -> None:
        self.set("tools", self.tool_key(session_content_hash, tool_name, call_args), response)
