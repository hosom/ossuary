"""Golden-file tests for the Claude Code adapter.

The archaeology rules, as tests: nothing is skipped, nothing is silently
dropped, and a malformed line survives as a degraded event carrying its raw text.
"""

from __future__ import annotations

from ossuary.models import Session


def test_no_line_is_ever_lost(claude_session: Session):
    """19 fixture lines, one of them blank, must all be accounted for."""
    assert len(claude_session.events) > 0
    # Three deliberately malformed lines: truncated JSON, a JSON array, and
    # non-JSON garbage. The blank line is skipped and is not an error.
    assert claude_session.parse_error_count == 3
    unparseable = [e for e in claude_session.events if e.kind == "unparseable"]
    assert len(unparseable) == 3


def test_malformed_lines_keep_their_raw_text_and_error(claude_session: Session):
    for event in claude_session.events:
        if event.kind == "unparseable":
            assert event.raw, "raw line must be preserved"
            assert event.parse_error, "parse error must be recorded"
            assert event.index >= 0


def test_one_line_can_expand_to_several_events(claude_session: Session):
    """A message with a text block and two tool_use blocks is three events."""
    text_events = [e for e in claude_session.events if e.kind == "message" and "I'll check both" in e.text]
    assert len(text_events) == 1
    anchor = text_events[0].index
    assert claude_session.events[anchor + 1].kind == "tool_call"
    assert claude_session.events[anchor + 2].kind == "tool_call"
    assert {claude_session.events[anchor + 1].tool_name,
            claude_session.events[anchor + 2].tool_name} == {"Bash", "Read"}


def test_indices_are_dense_and_ordinal(claude_session: Session):
    assert [e.index for e in claude_session.events] == list(range(len(claude_session.events)))


def test_tool_results_pair_by_id_not_adjacency(claude_session: Session):
    """The fixture returns toolu_B's result before toolu_A's, out of call order."""
    results = [e for e in claude_session.events if e.kind == "tool_result"]
    read_results = [e for e in results if e.tool_name == "Read"]
    bash_results = [e for e in results if e.tool_name == "Bash"]
    assert read_results, "Read result must be matched to its call by id"
    assert bash_results, "Bash results must be matched to their calls by id"
    # The Read result appears before the Bash result despite Bash being called first.
    assert read_results[0].index < bash_results[0].index


def test_orphan_result_is_flagged_not_misattributed(claude_session: Session):
    orphans = [e for e in claude_session.events if e.meta.get("orphan_result")]
    assert len(orphans) == 1
    assert orphans[0].tool_name is None, "must not be attributed to a neighbouring tool"


def test_shape_records_on_every_tool_result(claude_session: Session):
    for event in claude_session.events:
        if event.kind == "tool_result":
            assert event.shape is not None
            assert event.shape.content_hash


def test_the_thirty_thousand_byte_cap_is_visible(claude_session: Session):
    capped = [
        e for e in claude_session.events
        if e.shape and e.shape.byte_length == 30000
    ]
    assert len(capped) == 1
    shape = capped[0].shape
    assert shape.is_round_number, "30000 bytes is a cap, not a coincidence"
    assert not shape.terminates_cleanly, "cut mid-line"
    assert shape.exit_code == 0, "exit code 0 despite being truncated"


def test_empty_result_with_long_duration_and_exit_zero(claude_session: Session):
    """The swallowed-timeout signature: three fields that only mean something together."""
    empties = [e for e in claude_session.events if e.shape and e.shape.is_empty]
    assert empties
    target = empties[0]
    assert target.shape.exit_code == 0
    assert target.shape.duration_ms is not None
    assert target.shape.duration_ms >= 30_000


def test_duration_provenance_is_recorded(claude_session: Session):
    """A derived duration must never be presented as one the CLI reported."""
    results = [e for e in claude_session.events if e.kind == "tool_result" and e.shape]
    recorded = [e for e in results if e.shape.duration_source == "recorded"]
    derived = [e for e in results if e.shape.duration_source == "derived"]
    assert recorded, "fixture has an explicit durationMs"
    assert derived, "fixture has results whose duration must be derived"
    for event in results:
        if event.shape.duration_ms is None:
            assert event.shape.duration_source == "unavailable"


def test_byte_identical_repeats_share_a_content_hash(claude_session: Session):
    hashes = [
        e.shape.content_hash
        for e in claude_session.events
        if e.kind == "tool_result" and e.shape and "nothing to commit" in e.text
    ]
    assert len(hashes) == 2
    assert hashes[0] == hashes[1]


def test_thinking_signature_without_text_is_flagged(claude_session: Session):
    flagged = [e for e in claude_session.events if e.meta.get("thinking_signature_only")]
    assert len(flagged) == 1
    assert flagged[0].kind == "thinking"
    assert flagged[0].text == ""


def test_meta_line_types_are_kept_not_dropped(claude_session: Session):
    kinds = {e.meta.get("line_type") for e in claude_session.events if e.kind == "meta"}
    assert "attachment" in kinds
    assert "summary" in kinds


def test_session_metadata(claude_session: Session):
    assert claude_session.session_id == "sess-golden-0001"
    assert claude_session.source == "claude-code"
    assert claude_session.content_hash
    assert claude_session.project == "-home-user-demo"


def test_parsing_is_deterministic(claude_session: Session):
    """Same file content must produce the same events and the same hash."""
    from ossuary.adapters import get_adapter
    from ossuary.models import SessionRef
    from pathlib import Path

    path = Path(claude_session.path)
    again = get_adapter("claude-code").parse(
        SessionRef(
            session_id=claude_session.session_id,
            source="claude-code",
            path=str(path),
            size_bytes=path.stat().st_size,
        )
    )
    assert again.content_hash == claude_session.content_hash
    assert [e.index for e in again.events] == [e.index for e in claude_session.events]
    assert [e.kind for e in again.events] == [e.kind for e in claude_session.events]
