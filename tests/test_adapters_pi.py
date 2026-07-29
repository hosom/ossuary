"""The pi adapter.

The golden fixture was written by pi's own `SessionManager` and then damaged by
hand -- a line cut mid-write, a result whose call never appears -- because pi
will never produce those itself and they are what the adapter has to survive.
That makes the *shapes* here authentic to pi's writer, but it does not make them
observed: no pi install existed on the machine this was written on. Where a real
file disagrees with this fixture, the file wins and the adapter changes.
"""

from __future__ import annotations

import time
from datetime import timezone
from pathlib import Path

import pytest

from ossuary.adapters import get_adapter
from ossuary.models import Session
from ossuary.outline import _flags, render_outline

GOLDEN = Path(__file__).parent / "golden"


def _event(session: Session, kind: str, tool: str) -> object:
    return next(e for e in session.events if e.kind == kind and e.tool_name == tool)


class TestDiscovery:
    def test_session_identity_comes_from_the_header(self, pi_session: Session):
        assert pi_session.session_id == "019faaf5-8dbd-7da9-b8d1-bbcc88555e24"
        assert pi_session.source == "pi"
        assert pi_session.project == "/home/user/demo", "the header cwd, not the encoded dir name"

    def test_discovered_id_matches_the_parsed_id(self):
        """Otherwise the store caches a session under a name nothing asks for."""
        root = GOLDEN / "pi" / "sessions"
        adapter = get_adapter("pi", roots=[root])
        ref = adapter.discover([root])[0]
        assert ref.session_id == adapter.parse(ref).session_id

    def test_pi_does_not_claim_the_other_sources(self):
        adapter = get_adapter("pi")
        for other in ("claude-code", "codex", "copilot"):
            for path in (GOLDEN / other).rglob("*.jsonl"):
                assert not adapter.claims(path), f"pi claimed a {other} transcript"

    def test_the_other_sources_do_not_claim_pi(self):
        """A pi entry carrying a `payload` key reads as a Codex rollout line
        unless Codex recognises the session header first."""
        for path in (GOLDEN / "pi").rglob("*.jsonl"):
            for other in ("claude-code", "codex", "copilot"):
                assert not get_adapter(other).claims(path), (
                    f"{other} claimed {path.name}"
                )


class TestParsing:
    def test_malformed_line_is_degraded_not_dropped(self, pi_session: Session):
        assert pi_session.parse_error_count == 1
        bad = [e for e in pi_session.events if e.kind == "unparseable"]
        assert len(bad) == 1
        assert bad[0].raw and bad[0].parse_error

    def test_one_message_line_becomes_several_events(self, pi_session: Session):
        """A thinking block, a text block and a tool call are three events."""
        kinds = [e.kind for e in pi_session.events[4:7]]
        assert kinds == ["thinking", "message", "tool_call"]
        assert [e.index for e in pi_session.events] == list(range(len(pi_session.events)))

    def test_images_are_named_not_inlined(self, pi_session: Session):
        """Base64 in a shape record would measure the encoding, not the payload."""
        event = next(e for e in pi_session.events if "[image" in e.text)
        assert "[image image/png base64=" in event.text
        assert "iVBORw0KGgo" not in event.text

    def test_a_file_with_no_header_still_parses(self, tmp_path: Path):
        """A transcript cut off before its first line is still a transcript."""
        path = tmp_path / "headerless.jsonl"
        path.write_text(
            '{"type": "message", "id": "a", "parentId": null, '
            '"timestamp": "2026-01-01T00:00:00.000Z", '
            '"message": {"role": "user", "content": "hi"}}\n'
        )
        adapter = get_adapter("pi", roots=[tmp_path])
        ref = adapter.discover([tmp_path], require_claim=False)[0]
        session = adapter.parse(ref)
        assert [e.text for e in session.events] == ["hi"]

    def test_a_parent_cycle_does_not_hang(self, tmp_path: Path):
        """pi cannot write one; a damaged file can."""
        path = tmp_path / "cycle.jsonl"
        path.write_text(
            '{"type": "session", "version": 3, "id": "s1", '
            '"timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/x"}\n'
            '{"type": "message", "id": "a", "parentId": "b", '
            '"timestamp": "2026-01-01T00:00:01.000Z", '
            '"message": {"role": "user", "content": "x"}}\n'
            '{"type": "message", "id": "b", "parentId": "a", '
            '"timestamp": "2026-01-01T00:00:02.000Z", '
            '"message": {"role": "user", "content": "y"}}\n'
        )
        adapter = get_adapter("pi", roots=[tmp_path])
        session = adapter.parse(adapter.discover([tmp_path])[0])
        assert len(session.events) == 3

    def test_unrecognized_entry_type_is_kept_with_an_explanation(
        self, pi_legacy_session: Session
    ):
        event = pi_legacy_session.events[-1]
        assert event.kind == "meta"
        assert "telemetry_v9" in (event.parse_error or "")
        assert "emitted" in event.text


class TestTimestamps:
    """pi writes an ISO string with a `Z` on every entry and an epoch-ms number
    inside the message on that same entry. Both are UTC on disk. Reading the
    number as local time put a session's meta rows hours away from its
    conversation rows -- invisible on a UTC machine, which is why this fixes the
    clock to somewhere else."""

    @pytest.fixture(autouse=True)
    def _central_time(self, monkeypatch):
        if not hasattr(time, "tzset"):
            pytest.skip("no tzset on this platform")
        monkeypatch.setenv("TZ", "America/Chicago")
        time.tzset()
        yield
        time.tzset()

    def test_entry_and_message_clocks_agree(self, pi_session: Session):
        header = pi_session.events[0]
        first_message = pi_session.events[3]
        assert header.ts is not None and first_message.ts is not None
        assert header.ts.hour == 9, "the entry ISO timestamp, read as UTC"
        assert first_message.ts.hour == 9, "the message epoch-ms, read as UTC too"

    def test_every_timestamp_is_utc(self, pi_session: Session):
        stamped = [e.ts for e in pi_session.events if e.ts is not None]
        assert stamped
        assert all(e.utcoffset() == timezone.utc.utcoffset(None) for e in stamped)

    def test_the_outline_says_which_zone_it_is_showing(self, pi_session: Session):
        assert "time is UTC." in render_outline(pi_session)

    def test_a_duration_across_both_spellings_is_measured_not_lost(self, tmp_path: Path):
        """An assistant message with no timestamp of its own falls back to the
        entry's ISO one; subtracting a naive time from an aware one raises, and
        the whole line would degrade to unparseable."""
        path = tmp_path / "mixed.jsonl"
        path.write_text(
            '{"type": "session", "version": 3, "id": "s1", '
            '"timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/x"}\n'
            '{"type": "message", "id": "a", "parentId": null, '
            '"timestamp": "2026-01-01T00:00:10.000Z", "message": {"role": "assistant", '
            '"content": [{"type": "toolCall", "id": "c1", "name": "bash", '
            '"arguments": {"command": "true"}}], "stopReason": "toolUse"}}\n'
            '{"type": "message", "id": "b", "parentId": "a", '
            '"timestamp": "2026-01-01T00:00:12.000Z", "message": {"role": "toolResult", '
            '"toolCallId": "c1", "toolName": "bash", '
            '"content": [{"type": "text", "text": "ok"}], "isError": false, '
            '"timestamp": 1767225612000}}\n'
        )
        adapter = get_adapter("pi", roots=[tmp_path])
        session = adapter.parse(adapter.discover([tmp_path])[0])
        assert session.parse_error_count == 0
        result = next(e for e in session.events if e.kind == "tool_result")
        assert result.shape is not None and result.shape.duration_ms == 2000


class TestToolResults:
    def test_result_is_paired_with_its_call_and_names_its_own_tool(self, pi_session: Session):
        call = _event(pi_session, "tool_call", "bash")
        result = _event(pi_session, "tool_result", "bash")
        assert result.meta["tool_call_id"] == "call_1"
        assert result.meta["call_event_index"] == call.index

    def test_duration_is_derived_from_the_millisecond_timestamps(self, pi_session: Session):
        result = _event(pi_session, "tool_result", "bash")
        assert result.shape is not None
        assert result.shape.duration_ms == 4500
        assert result.shape.duration_source == "derived", "pi records no tool duration"

    def test_empty_body_and_a_long_duration_survive_together(self, pi_session: Session):
        """The signature of a timeout swallowed by the harness."""
        result = _event(pi_session, "tool_result", "read")
        assert result.shape is not None
        assert result.shape.is_empty
        assert result.shape.duration_ms == 30_000

    def test_truncation_record_is_carried_verbatim(self, pi_session: Session):
        """pi is the only supported CLI that says how much it cut, and why."""
        result = _event(pi_session, "tool_result", "bash")
        truncation = result.meta["truncation"]
        assert truncation["truncated"] is True
        assert truncation["truncatedBy"] == "bytes"
        assert truncation["totalBytes"] == 900_000
        assert truncation["totalLines"] == 9_000
        assert "content" not in truncation, "the payload is already the event text"
        assert result.meta["full_output_path"] == "/tmp/pi-bash-out-1.txt"

    def test_a_capped_payload_reads_as_a_round_number(self, pi_session: Session):
        result = _event(pi_session, "tool_result", "bash")
        assert result.shape is not None
        assert result.shape.byte_length == 2048
        assert result.shape.is_round_number

    def test_tool_results_never_carry_a_synthesised_exit_code(self, pi_session: Session):
        """pi puts the failing code in prose; parsing it would fabricate a field."""
        for event in pi_session.events:
            if event.kind == "tool_result" and event.tool_name != "user_bash":
                assert event.shape is not None and event.shape.exit_code is None

    def test_error_flag_comes_from_is_error(self, pi_session: Session):
        result = _event(pi_session, "tool_result", "grep")
        assert result.shape is not None and result.shape.has_error_field

    def test_result_with_no_matching_call_is_flagged_and_still_named(self, pi_session: Session):
        result = _event(pi_session, "tool_result", "grep")
        assert result.meta["orphan_result"] is True
        assert "call_event_index" not in result.meta


class TestUserBash:
    def test_bash_execution_splits_into_a_call_and_a_result(self, pi_session: Session):
        call = _event(pi_session, "tool_call", "user_bash")
        result = _event(pi_session, "tool_result", "user_bash")
        assert call.text == "git status --short"
        assert result.text == "?? scratch.py\n"
        assert result.meta["call_event_index"] == call.index

    def test_the_only_genuinely_recorded_exit_code_is_kept(self, pi_session: Session):
        result = _event(pi_session, "tool_result", "user_bash")
        assert result.shape is not None and result.shape.exit_code == 1

    def test_user_bash_is_not_counted_as_an_orphan(self, pi_session: Session):
        result = _event(pi_session, "tool_result", "user_bash")
        assert "orphan_result" not in result.meta


class TestBranches:
    def test_entries_the_session_moved_off_are_kept_and_marked(self, pi_session: Session):
        off = [e for e in pi_session.events if e.meta.get("off_path")]
        assert [e.text for e in off][:1] == [
            "Earlier turns summarized: the suite was run and the config read."
        ]
        assert len(off) == 5, "the compaction, custom entry, custom message, label and name"

    def test_the_live_conversation_is_not_marked(self, pi_session: Session):
        last = pi_session.events[-1]
        assert last.text == "start over from the test failure"
        assert "off_path" not in last.meta
        first_call = _event(pi_session, "tool_call", "bash")
        assert "off_path" not in first_call.meta

    def test_file_order_is_preserved(self, pi_session: Session):
        """Emitting the active path only would delete the evidence of a rewind."""
        line_numbers = [
            e.meta["line_no"] for e in pi_session.events if "line_no" in e.meta
        ]
        assert line_numbers == sorted(line_numbers)

    def test_the_outline_explains_the_branch_flag(self, pi_session: Session):
        outline = render_outline(pi_session)
        assert "B=on a branch the session moved off" in outline
        assert "rewound to an earlier point" in outline

    def test_a_linear_session_marks_nothing(self, pi_legacy_session: Session):
        """v1 sessions have no ids and cannot branch."""
        assert not any(e.meta.get("off_path") for e in pi_legacy_session.events)
        assert render_outline(pi_legacy_session).count(" B ") == 0


class TestHarnessSignals:
    def test_a_turn_that_ended_badly_gets_its_own_row(self, pi_session: Session):
        """`stopReason` has no field on the event and nothing renders meta."""
        event = next(e for e in pi_session.events if e.meta.get("turn_end"))
        assert event.kind == "meta"
        assert event.text == "turn ended: error -- provider returned 500 after 3 retries"
        assert event.meta["stop_reason"] == "error"

    def test_a_failed_turn_is_flagged_on_the_row_that_looks_empty(self, pi_session: Session):
        """Four investigators reading four sessions each concluded pi swallows
        these failures silently, because the empty turn is one row and the error
        text is the next one. The flag goes on both."""
        empty_turn = next(
            e for e in pi_session.events
            if e.kind == "message" and e.role == "assistant" and not e.text
        )
        error_row = next(e for e in pi_session.events if e.meta.get("turn_end"))
        assert "F" in _flags(empty_turn), "the row that reads as a turn that did nothing"
        assert "F" in _flags(error_row)
        assert "reads as a turn that simply produced nothing" in render_outline(pi_session)

    def test_model_changes_are_visible(self, pi_session: Session):
        event = next(e for e in pi_session.events if e.meta.get("entry_type") == "model_change")
        assert event.text == "model anthropic/claude-sonnet-4-5"
        assert event.meta["model"] == "claude-sonnet-4-5"

    def test_compaction_keeps_the_token_count(self, pi_session: Session):
        event = next(e for e in pi_session.events if e.meta.get("entry_type") == "compaction")
        assert event.meta["tokens_before"] == 50_000

    def test_legacy_compaction_index_is_kept_as_an_index(self, pi_legacy_session: Session):
        """v1 pointed at a position; only pi's own migration can resolve it."""
        event = next(
            e for e in pi_legacy_session.events if e.meta.get("entry_type") == "compaction"
        )
        assert event.meta["first_kept_entry_index"] == 1
        assert "first_kept_entry_id" not in event.meta

    def test_v2_hook_message_role_is_read(self, pi_legacy_session: Session):
        event = next(e for e in pi_legacy_session.events if e.meta.get("legacy_role"))
        assert event.kind == "message"
        assert event.text == "Project rules were injected here."
        assert event.meta["custom_type"] == "house-rules"

    def test_usage_is_kept_on_assistant_events(self, pi_session: Session):
        call = _event(pi_session, "tool_call", "bash")
        assert call.meta["usage"]["totalTokens"] == 160
        assert call.meta["provider"] == "anthropic"
