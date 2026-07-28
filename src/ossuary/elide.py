"""Labelled truncation.

Ossuary exists to detect things like payloads that stop mid-sentence. If Ossuary
itself shortens a payload without saying so, it manufactures the exact artifact
it is trying to detect -- an agent reading a payload that ends abruptly cannot
tell whether the tool truncated it or we did.

So: every code path that shortens a payload routes through here, and every
shortened payload carries an explicit marker. The invariant downstream consumers
rely on is the contrapositive:

    A payload with no `[[ossuary:elided ...]]` marker ended that way on disk.

This is a correctness requirement, not a nicety.
"""

from __future__ import annotations

import re

MARKER_RE = re.compile(r"\[\[ossuary:elided (\d+) of (\d+) bytes\]\]")


def marker(elided_bytes: int, total_bytes: int) -> str:
    return f"[[ossuary:elided {elided_bytes} of {total_bytes} bytes]]"


def is_elided(text: str) -> bool:
    """True if this text carries an Ossuary elision marker."""
    return MARKER_RE.search(text) is not None


def elide_middle(text: str, limit: int) -> str:
    """Keep the head and tail of `text`, marking the removed middle.

    Preferred over head-only truncation for payloads: the tail is where a
    traceback's exception line and a command's exit banner live, and the whole
    point is to let the agent judge whether the payload terminated cleanly.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    raw = text.encode("utf-8", errors="surrogatepass")
    total = len(raw)
    if total <= limit:
        return text

    # Reserve room for the marker itself so the result honours `limit`.
    probe = marker(total, total)
    budget = limit - len(probe) - 2  # two newlines around the marker
    if budget < 16:
        # Degenerate limit: emit the marker alone rather than a misleading stub.
        return marker(total, total)

    head_bytes = (budget * 2) // 3
    tail_bytes = budget - head_bytes
    head = _decode_partial(raw[:head_bytes])
    tail = _decode_partial(raw[total - tail_bytes :], from_end=True)
    elided = total - len(head.encode("utf-8", errors="surrogatepass")) - len(
        tail.encode("utf-8", errors="surrogatepass")
    )
    return f"{head}\n{marker(elided, total)}\n{tail}"


def elide_tail(text: str, limit: int) -> str:
    """Keep the head of `text`, marking everything dropped after it."""
    if limit <= 0:
        raise ValueError("limit must be positive")

    raw = text.encode("utf-8", errors="surrogatepass")
    total = len(raw)
    if total <= limit:
        return text

    probe = marker(total, total)
    budget = limit - len(probe) - 1
    if budget < 16:
        return marker(total, total)

    head = _decode_partial(raw[:budget])
    elided = total - len(head.encode("utf-8", errors="surrogatepass"))
    return f"{head}\n{marker(elided, total)}"


def _decode_partial(chunk: bytes, from_end: bool = False) -> str:
    """Decode a byte slice that may have cut a UTF-8 sequence in half.

    Walks the boundary inward until the slice decodes. Never raises, because a
    transcript is the only record that will ever exist of the session it
    describes and mangled bytes are not a reason to lose it.
    """
    for trim in range(0, 5):
        candidate = chunk[trim:] if from_end else (chunk[: len(chunk) - trim] if trim else chunk)
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return chunk.decode("utf-8", errors="replace")
