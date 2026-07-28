"""The marker invariant.

If any of these fail, Ossuary can manufacture the artifact it exists to detect.
"""

from __future__ import annotations

import pytest

from ossuary.elide import MARKER_RE, elide_middle, elide_tail, is_elided, marker


def test_short_text_is_returned_untouched():
    text = "this is short"
    assert elide_middle(text, 1000) is text
    assert elide_tail(text, 1000) is text
    assert not is_elided(text)


def test_middle_elision_marks_what_it_removed():
    text = "A" * 5000
    out = elide_middle(text, 500)
    assert is_elided(out)
    match = MARKER_RE.search(out)
    assert match is not None
    elided, total = int(match.group(1)), int(match.group(2))
    assert total == 5000
    # Everything not kept must be accounted for in the marker.
    kept = len(out.encode()) - len(match.group(0))
    assert elided + kept == pytest.approx(total, abs=4)


def test_middle_elision_keeps_head_and_tail():
    text = "HEAD" + "x" * 5000 + "TAIL"
    out = elide_middle(text, 400)
    assert out.startswith("HEAD")
    assert out.rstrip().endswith("TAIL")


def test_tail_elision_marks_what_it_removed():
    text = "B" * 3000
    out = elide_tail(text, 300)
    assert is_elided(out)
    assert len(out.encode()) <= 300


def test_elision_honours_the_byte_limit():
    for limit in (64, 128, 512, 2048):
        out = elide_middle("z" * 100_000, limit)
        assert len(out.encode()) <= limit, f"limit {limit} exceeded"


def test_degenerate_limit_emits_marker_not_a_misleading_stub():
    out = elide_middle("y" * 1000, 20)
    # Too small for any content; must be the marker alone rather than a bare
    # fragment that would look like a genuinely short payload.
    assert MARKER_RE.fullmatch(out.strip()) is not None


def test_multibyte_boundaries_never_raise_and_never_mangle():
    text = "日本語のテキスト" * 500
    out = elide_middle(text, 300)
    assert is_elided(out)
    # Must round-trip as valid UTF-8 with no replacement characters introduced.
    out.encode("utf-8").decode("utf-8")
    assert "�" not in out


def test_marker_format_is_exact():
    assert marker(48213, 52000) == "[[ossuary:elided 48213 of 52000 bytes]]"


def test_unmarked_text_means_untruncated():
    """The invariant downstream depends on, stated as a test."""
    original = "a payload that simply ends"
    assert not is_elided(elide_middle(original, 10_000))
