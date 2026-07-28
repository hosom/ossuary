"""Shape record computation -- measurement, not detection.

Nothing here decides whether something is a problem. Each field is a physical
property of the payload; interpretation is the agent's job. The distinction
matters: a deterministic detector can only find what its author already knew
about, and the premise of the tool is finding failure modes nobody thought to
look for in advance.
"""

from __future__ import annotations

import hashlib
import math
import re

from .models import ShapeRecord

# Byte counts that are almost certainly a configured cap rather than a
# coincidence. We do not claim a payload of this size *is* capped -- we flag the
# roundness and let the agent weigh it against the corpus statistics.
_COMMON_CAPS = {
    1000, 2000, 4000, 5000, 8000, 10000, 16000, 20000, 25000, 30000, 32000,
    40000, 50000, 60000, 64000, 65536, 80000, 100000, 128000, 131072, 200000,
    256000, 262144, 500000, 512000, 524288, 1000000, 1048576,
}

# Powers of two are caps often enough to be worth flagging on their own.
_MIN_ROUND_CANDIDATE = 512

_SENTENCE_END = tuple(".!?\"')]}>`:;,")

# A final line longer than this that ends with no terminator is the signature of
# a cut, not of a payload that simply finished. Ordinary command output ends in
# short lines (`README.md`, `done`, a file path), so the threshold keeps the flag
# quiet on healthy payloads -- a flag that fires on every row carries no
# information and trains the agent to ignore it.
_LONG_FINAL_LINE = 120

# Minimum line count before uniform-width output is considered regular enough
# for a short final line to mean anything.
_MIN_UNIFORM_LINES = 4

_REPLACEMENT_CHAR = "�"


def is_round_number(n: int) -> bool:
    """True if `n` looks like a configured limit rather than a natural length."""
    if n < _MIN_ROUND_CANDIDATE:
        return False
    if n in _COMMON_CAPS:
        return True
    if n & (n - 1) == 0:  # exact power of two
        return True
    # Exact multiples of 1000 or 1024 at or above 1000 bytes.
    if n >= 1000 and (n % 1000 == 0 or n % 1024 == 0):
        return True
    return False


def terminates_cleanly(text: str) -> bool:
    """True if the payload ends at a plausible boundary rather than mid-flow.

    Deliberately conservative: it answers "clean" unless there is positive
    evidence of a cut. Distinguishing a truncated token from a short final word
    is not decidable from the text alone, and the useful version of this signal
    is the one that stays quiet on healthy payloads. It is an observation for the
    agent to weigh against the corpus statistics, never a verdict on its own.
    """
    if not text.strip():
        # An empty payload did not stop mid-token; emptiness is reported
        # separately via `is_empty`.
        return True

    # A decode that lost bytes mid-sequence is unambiguous evidence of a cut.
    if text.endswith(_REPLACEMENT_CHAR):
        return False

    stripped = text.rstrip()

    # Checked before punctuation: a JSON object cut after `"version"` ends on a
    # quote, which would otherwise read as a clean boundary.
    if _has_unbalanced_brackets(stripped):
        return False

    # Regular output cut mid-stream: every full line is the same width and the
    # last one is short. Catches byte-capped logs, tables, and hexdumps, where
    # the cut can land anywhere in a line and the length rule below would miss
    # it. Requires genuine uniformity, so irregular output is never flagged.
    if _uniform_lines_end_short(stripped):
        return False

    # Trailing whitespace or newline: the writer finished and flushed.
    if text != stripped:
        return True

    if stripped.endswith(_SENTENCE_END):
        return True

    final_line = stripped.rsplit("\n", 1)[-1]
    if len(final_line) < _LONG_FINAL_LINE:
        # A short, unterminated final line is how most command output ends.
        return True

    # A long final line with no terminator of any kind: consistent with a cap.
    return False


def _uniform_lines_end_short(text: str) -> bool:
    """True when uniformly-wide output ends on a short line.

    Deliberately narrow. It demands that every line but the last share one exact
    width before it will call anything a cut, because the alternative -- "the
    last line is shorter than average" -- is true of most healthy output and
    would make the flag meaningless.
    """
    lines = text.split("\n")
    if len(lines) < _MIN_UNIFORM_LINES:
        return False
    widths = {len(line) for line in lines[:-1]}
    if len(widths) != 1:
        return False
    width = widths.pop()
    if width == 0:
        return False
    return len(lines[-1]) < width


def _has_unbalanced_brackets(text: str) -> bool:
    """Cheap structural check for JSON- and code-shaped payloads.

    Only consulted for payloads that already look structured, so prose
    containing a stray bracket is not mistaken for a truncated object.
    """
    head = text.lstrip()
    if not head or head[0] not in "{[":
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return in_string or depth > 0


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]


def compute_shape(
    text: str,
    *,
    duration_ms: int | None = None,
    exit_code: int | None = None,
    has_error_field: bool = False,
    duration_source: str = "unavailable",
) -> ShapeRecord:
    byte_length = len(text.encode("utf-8", errors="surrogatepass"))
    return ShapeRecord(
        byte_length=byte_length,
        duration_ms=duration_ms,
        exit_code=exit_code,
        has_error_field=has_error_field,
        terminates_cleanly=terminates_cleanly(text),
        is_round_number=is_round_number(byte_length),
        content_hash=content_hash(text),
        is_empty=len(text.strip()) == 0,
        duration_source=duration_source,  # type: ignore[arg-type]
    )


def percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile. Deterministic and dependency-free."""
    if not values:
        return 0
    ordered = sorted(values)
    if pct <= 0:
        return ordered[0]
    if pct >= 100:
        return ordered[-1]
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[max(0, min(len(ordered) - 1, rank - 1))]
