"""Redaction, applied before anything leaves the machine.

Agent transcripts contain source code, file contents, and not infrequently
credentials. This pass runs on every payload before an API call. It is not a
detector in the sense section 1 forbids -- it never produces findings and never
influences what the agent reports; it only masks secret-shaped text on the way
out.

Redaction preserves byte length wherever it can, because shape records are
computed from the on-disk text and the agent reasons about lengths. Where a
replacement cannot match the original length the delta is small and the mask is
visible, so the agent is never silently misled.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_PLACEHOLDER = "[[ossuary:redacted {kind}]]"

# Ordered: more specific patterns first so a generic rule cannot eat a
# well-identified token and mislabel it.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "url-credentials",
        re.compile(r"\b([a-zA-Z][\w+.\-]*://)[^\s:/@]+:[^\s/@]+@"),
    ),
    (
        "auth-header",
        re.compile(
            r"(?i)\b(authorization\s*:\s*(?:bearer|basic|token)\s+)"
            r"(?!\[\[ossuary:redacted)([A-Za-z0-9._\-+/=]{12,})"
        ),
    ),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|ACCESS_?KEY|CREDENTIAL"
            r"|PRIVATE_?KEY|CLIENT_?SECRET)[A-Z0-9_]*)\s*[=:]\s*"
            # A value already replaced by an earlier rule must not be re-masked:
            # masking a mask inflates byte length and corrupts the shape records
            # the agent reasons about.
            r"(?!\[\[ossuary:redacted)"
            r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;)}\]]{4,})"
        ),
    ),
]

# Long unbroken high-entropy strings, checked last and only when they do not look
# like the hex digests, paths, and base64 blobs that fill ordinary transcripts.
_KEY_SHAPED = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")
_LOOKS_BENIGN = re.compile(r"^(?:[0-9a-f]+|[0-9A-F]+|[0-9]+|[A-Za-z_\-]+)$")

# Placeholder for an already-masked span, opaque to every rule above.
_SENTINEL_RE = re.compile("\x00(\\d+)\x00")


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass
class Redactor:
    """Masks secret-shaped text.

    `enabled=False` is the `--no-redact` escape hatch. It is a deliberate,
    explicit choice by the operator, never a default and never a silent fallback.
    """

    enabled: bool = True
    redact_env_values: bool = True
    min_env_value_length: int = 8
    _env_values: list[tuple[str, str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.redact_env_values:
            self._env_values = _collect_env_values(self.min_env_value_length)

    def redact(self, text: str) -> RedactionResult:
        if not self.enabled or not text:
            return RedactionResult(text=text)

        counts: dict[str, int] = {}
        # Masked spans are parked behind opaque sentinels while the remaining
        # rules run, then restored at the end. Without this, a placeholder like
        # `[[ossuary:redacted github-token]]` is itself matched by the
        # assigned-secret rule (it contains "token="), and each pass inflates the
        # text further -- which would corrupt the byte lengths the shape records
        # depend on.
        vault: list[tuple[str, int]] = []

        def stash(kind: str, original_length: int) -> str:
            vault.append((kind, original_length))
            return f"\x00{len(vault) - 1}\x00"

        out = text
        for kind, pattern in _PATTERNS:
            if kind == "url-credentials":
                # Keep the scheme, drop the `user:password@` that follows it.
                out, n = pattern.subn(
                    lambda m, k=kind: m.group(1) + stash(k, 12) + "@", out
                )
            elif kind in ("auth-header", "assigned-secret"):
                # Keep everything up to the secret itself (the header name and
                # separator, or the variable name and `=`), mask only group 2.
                out, n = pattern.subn(
                    lambda m, k=kind: _keep_prefix(m) + stash(k, len(m.group(2))), out
                )
            else:
                out, n = pattern.subn(lambda m, k=kind: stash(k, len(m.group(0))), out)
            if n:
                counts[kind] = counts.get(kind, 0) + n

        # Literal values of the operator's own environment variables. Catches the
        # case where a transcript echoes a real credential with no recognisable
        # shape at all.
        for name, value in self._env_values:
            if value and value in out:
                occurrences = out.count(value)
                out = out.replace(value, stash(f"env:{name}", len(value)))
                counts["env-value"] = counts.get("env-value", 0) + occurrences

        out, n = _redact_key_shaped(out, stash)
        if n:
            counts["key-shaped"] = counts.get("key-shaped", 0) + n

        out = _SENTINEL_RE.sub(
            lambda m: _mask(*vault[int(m.group(1))]), out
        )
        return RedactionResult(text=out, counts=counts)

    def redact_text(self, text: str) -> str:
        return self.redact(text).text


def _keep_prefix(match: re.Match[str]) -> str:
    """The part of the match before group 2 -- name, separator, and whitespace."""
    return match.group(0)[: match.start(2) - match.start(0)]


def _mask(kind: str, original_length: int) -> str:
    """Build a placeholder, padded toward the original length where possible.

    Byte-length fidelity matters because the agent reasons about payload sizes,
    and a redaction pass that silently shrank payloads would distort exactly the
    signal the shape records carry.
    """
    base = _PLACEHOLDER.format(kind=kind)
    if original_length > len(base):
        return base[:-2] + "=" * (original_length - len(base)) + "]]"
    return base


def _redact_key_shaped(text: str, stash) -> tuple[str, int]:  # type: ignore[no-untyped-def]
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group(0)
        if _LOOKS_BENIGN.match(token):
            return token
        # Require genuine mixed-case-plus-digit entropy before masking, so file
        # paths, identifiers, and hex digests survive intact.
        has_upper = any(c.isupper() for c in token)
        has_lower = any(c.islower() for c in token)
        has_digit = any(c.isdigit() for c in token)
        if not (has_upper and has_lower and has_digit):
            return token
        count += 1
        return stash("key-shaped", len(token))

    return _KEY_SHAPED.sub(repl, text), count


_ENV_DENYLIST_SUBSTRINGS = (
    "PATH", "HOME", "LANG", "TERM", "SHELL", "PWD", "USER", "HOSTNAME",
    "EDITOR", "PAGER", "TZ", "DISPLAY", "LC_",
)


def _collect_env_values(min_length: int) -> list[tuple[str, str]]:
    """Values of environment variables that look like credentials.

    Longest first, so a value that is a substring of another is masked after the
    longer one and cannot corrupt it.
    """
    # Deliberately excludes bare "SESSION": session identifiers are not secrets,
    # they are the primary key Agent A uses to address a transcript, and masking
    # them breaks every tool call that references one. Genuinely sensitive
    # session variables still match via TOKEN, KEY, or SECRET.
    interesting = (
        "SECRET", "PASSWORD", "PASSWD", "TOKEN", "KEY", "CREDENTIAL", "AUTH",
        "PRIVATE", "COOKIE", "SIGNATURE", "SALT",
    )
    found: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        upper = name.upper()
        if any(bad in upper for bad in _ENV_DENYLIST_SUBSTRINGS):
            continue
        if len(value) < min_length:
            continue
        if not any(word in upper for word in interesting):
            continue
        found.append((name, value))
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return found
