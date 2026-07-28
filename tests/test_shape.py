from __future__ import annotations

import pytest

from ossuary.shape import compute_shape, is_round_number, percentile, terminates_cleanly


@pytest.mark.parametrize("value", [1000, 30000, 65536, 1024, 4096, 100000, 1048576])
def test_cap_shaped_numbers_are_round(value: int):
    assert is_round_number(value)


@pytest.mark.parametrize("value", [0, 42, 310, 991, 30001, 12345, 7331])
def test_natural_lengths_are_not_round(value: int):
    assert not is_round_number(value)


class TestTerminatesCleanly:
    """The flag has to stay quiet on healthy payloads or it carries no signal."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "On branch main\nnothing to commit\n",
            "total 16\ndrwxr-xr-x 3 root root 4096 README.md",
            "Done.",
            '{"name": "demo", "version": "1.0"}',
            "some output\n",
            "Result: 42",
            "[1, 2, 3]",
            "```\ncode\n```",
        ],
    )
    def test_healthy_payloads_are_clean(self, text: str):
        assert terminates_cleanly(text), f"false positive on {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "x" * 200,  # long unterminated final line
            '{"name": "demo", "version"',  # cut mid-object
            "line one\nline two\n" + "y" * 300,
            "text ending in a bad decode�",
        ],
    )
    def test_cut_payloads_are_flagged(self, text: str):
        assert not terminates_cleanly(text), f"missed a cut on {text[:40]!r}"

    def test_short_unterminated_line_is_not_flagged(self):
        # Most command output ends this way; flagging it would drown the signal.
        assert terminates_cleanly("hello world")


def test_compute_shape_reports_the_measurements():
    shape = compute_shape("hello", duration_ms=1200, exit_code=0, duration_source="recorded")
    assert shape.byte_length == 5
    assert shape.duration_ms == 1200
    assert shape.exit_code == 0
    assert shape.duration_source == "recorded"
    assert not shape.is_empty
    assert shape.content_hash


def test_identical_text_hashes_identically():
    assert compute_shape("same").content_hash == compute_shape("same").content_hash
    assert compute_shape("a").content_hash != compute_shape("b").content_hash


def test_whitespace_only_counts_as_empty():
    assert compute_shape("   \n\t ").is_empty


def test_byte_length_counts_bytes_not_characters():
    assert compute_shape("日本").byte_length == 6


def test_percentile_is_nearest_rank():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 50) == 5
    assert percentile(values, 95) == 10
    assert percentile(values, 0) == 1
    assert percentile([], 50) == 0
