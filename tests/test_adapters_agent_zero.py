"""The Agent Zero adapter.

The `log` half of the golden fixture was written by Agent Zero's own
`helpers/log.py` -- so the truncation markers, the `no` sequencing and the kvps
handling are its writer's, not ours -- and the context envelope and history
string were written by hand to `_serialize_context` and `History.to_dict`, which
need the whole model stack to run for real. Then it was damaged: an item that is
not an object, a history string cut off mid-write, a log whose first item is
numbered 812, a call that never returned, and an item type from a plugin that
did not exist when this was written.

That makes the shapes here faithful to the source. It does not make them
observed: no Agent Zero install existed on the machine this was written on.
Where a real chat disagrees with this fixture, the chat wins and the adapter
changes.
"""

from __future__ import annotations

import json
from pathlib import Path

from ossuary.adapters import get_adapter
from ossuary.models import Session
from ossuary.outline import render_outline

GOLDEN = Path(__file__).parent / "golden"
AGENT_ZERO_ROOT = GOLDEN / "agent-zero"


def _result(session: Session, tool: str):
    return next(
        e for e in session.events if e.kind == "tool_result" and e.tool_name == tool
    )


class TestDiscovery:
    def test_identity_and_project_come_from_the_chat(self, agent_zero_session: Session):
        assert agent_zero_session.session_id == "ctx-golden-0001"
        assert agent_zero_session.source == "agent-zero"
        assert agent_zero_session.project == "/a0/usr/projects/dates"

    def test_project_falls_back_to_the_chat_title(
        self, agent_zero_compacted_session: Session
    ):
        """Agent Zero has no per-session cwd. A chat bound to no project has a
        name, and the header event says which of the two the column holds."""
        assert agent_zero_compacted_session.project == "Flaky timezone test"
        header = agent_zero_compacted_session.events[0]
        assert header.meta["project_source"] == "chat_name"

    def test_discovered_id_matches_the_parsed_id(self):
        """Otherwise the store caches a session under a name nothing asks for."""
        adapter = get_adapter("agent-zero", roots=[AGENT_ZERO_ROOT])
        for ref in adapter.discover([AGENT_ZERO_ROOT]):
            assert ref.session_id == adapter.parse(ref).session_id

    def test_pre_compaction_backups_are_discovered(self):
        """`/compact` empties the live chat. Without the backup, a compacted
        session reads as one that only ever contained its own summary."""
        adapter = get_adapter("agent-zero", roots=[AGENT_ZERO_ROOT])
        ids = {ref.session_id for ref in adapter.discover([AGENT_ZERO_ROOT])}
        assert "ctx-golden-0002@pre-compact-20260816-105012" in ids

    def test_a_backup_does_not_collide_with_the_chat_it_was_cut_from(
        self, agent_zero_backup_session: Session
    ):
        """Both files carry the same `id` field, so the id comes from the path."""
        assert agent_zero_backup_session.session_id.startswith("ctx-golden-0002@")
        assert agent_zero_backup_session.events[0].meta["context_id"] == "ctx-golden-0002"
        assert agent_zero_backup_session.events[0].meta["pre_compaction_backup"] is True

    def test_agent_zero_does_not_claim_the_other_sources(self):
        adapter = get_adapter("agent-zero")
        for other in ("claude-code", "codex", "copilot", "pi"):
            for path in (GOLDEN / other).rglob("*"):
                if path.is_file():
                    assert not adapter.claims(path), f"agent-zero claimed a {other} file"

    def test_the_other_sources_do_not_claim_agent_zero(self):
        """Copilot is the only other adapter that reads `.json`."""
        for path in AGENT_ZERO_ROOT.rglob("*.json"):
            for other in ("claude-code", "codex", "copilot", "pi"):
                assert not get_adapter(other).claims(path), f"{other} claimed {path.name}"


class TestParsing:
    def test_a_log_item_that_is_not_an_object_is_degraded_not_dropped(
        self, agent_zero_session: Session
    ):
        bad = [
            e
            for e in agent_zero_session.events
            if e.kind == "unparseable" and "log item" in (e.parse_error or "")
        ]
        assert len(bad) == 1
        assert bad[0].raw and "process died" in bad[0].raw

    def test_a_broken_history_does_not_take_the_session_down(
        self, agent_zero_session: Session
    ):
        """`agents[].history` is JSON inside the JSON -- a second parse on
        content that has already survived one."""
        broken = [
            e
            for e in agent_zero_session.events
            if e.kind == "unparseable" and "history did not parse" in (e.parse_error or "")
        ]
        assert len(broken) == 1
        assert "agent 1" in broken[0].parse_error
        assert agent_zero_session.parse_error_count == 2
        assert len(agent_zero_session.events) > 20, "the rest of the session survived"

    def test_a_plugin_item_type_is_kept_and_flagged(self, agent_zero_session: Session):
        """Plugins can log any type. An unknown one is not a lost event."""
        event = next(
            e for e in agent_zero_session.events if e.meta.get("unknown_item_type")
        )
        assert event.kind == "meta"
        assert "telemetry" in event.parse_error
        assert "telemetry" in event.text

    def test_indices_are_a_contiguous_ordinal_over_events(
        self, agent_zero_session: Session
    ):
        assert [e.index for e in agent_zero_session.events] == list(
            range(len(agent_zero_session.events))
        )

    def test_a_file_that_is_not_json_becomes_one_marked_event(self, tmp_path: Path):
        path = tmp_path / "chat.json"
        path.write_text('{"id": "x", "agents": [], "created_at": "2026-01-01', encoding="utf-8")
        adapter = get_adapter("agent-zero", roots=[tmp_path])
        session = adapter.parse(adapter.discover([tmp_path], require_claim=False)[0])
        assert session.parse_error_count == 1
        assert session.events[0].kind == "unparseable"
        assert session.events[0].raw

    def test_a_truncated_raw_payload_says_it_was_truncated(self, tmp_path: Path):
        """Ossuary never shortens anything silently, including a file it could
        not read at all."""
        path = tmp_path / "chat.json"
        path.write_text('{"agents": [], "created_at": "x", ' + "y" * 60_000, encoding="utf-8")
        adapter = get_adapter("agent-zero", roots=[tmp_path])
        session = adapter.parse(adapter.discover([tmp_path])[0])
        assert "[[ossuary:elided" in session.events[0].raw


class TestTheTwoRecords:
    """The log is a display copy; the history is what the model was given."""

    def test_the_payload_comes_from_the_history_where_the_join_reaches_it(
        self, agent_zero_session: Session
    ):
        result = _result(agent_zero_session, "code_execution_tool")
        assert result.meta["payload_source"] == "history"
        assert result.shape.byte_length > 15000, (
            "the log's copy of this payload was cut at 15000 characters; "
            "measuring that would report Agent Zero's UI limit as a tool's output"
        )
        assert "2 failed, 698 passed" in result.text

    def test_the_log_is_used_where_the_history_has_nothing_and_says_so(
        self, agent_zero_session: Session
    ):
        result = _result(agent_zero_session, "search_engine")
        assert result.meta["payload_source"] == "log"

    def test_the_tool_name_comes_from_the_join_not_from_the_heading(
        self, agent_zero_session: Session
    ):
        """`code_exe` and `subagent` items record no tool name in `kvps`. It
        survives in the history message and in the heading string, and a heading
        is a sentence rather than a source of facts."""
        call = next(
            e
            for e in agent_zero_session.events
            if e.kind == "tool_call" and e.meta.get("item_type") == "subagent"
        )
        assert call.tool_name == "call_subordinate"

    def test_a_tool_nobody_named_gets_a_bracketed_bucket(self, tmp_path: Path):
        """Never a guessed name: `<code_exe>` is true, `code_execution_tool`
        might not be."""
        session = _parse_chat(
            tmp_path,
            logs=[_log_item(0, "code_exe", content="output", kvps={"runtime": "terminal"})],
        )
        assert _result(session, "<code_exe>").meta["payload_source"] == "log"

    def test_history_messages_the_log_no_longer_has_still_appear(
        self, agent_zero_session: Session
    ):
        """A session whose start fell off the 1000-item cap still has those
        messages on the history side. Emitting only the log would lose them."""
        recovered = _result(agent_zero_session, "knowledge_tool")
        assert recovered.meta["from_history"] is True
        assert recovered.ts is None, "history records carry an ordering but no clock"
        assert recovered.meta["orphan_result"] is True

    def test_history_events_are_in_walk_order_not_sequence_order(
        self, agent_zero_session: Session
    ):
        """The summary Agent Zero inserts in place of a collapsed run is built
        without a sequence number and carries 0. Ordering by sequence would file
        it at the start of the conversation instead of where it stands."""
        from_history = [e for e in agent_zero_session.events if e.meta.get("from_history")]
        texts = [e.text for e in from_history]
        assert texts.index("Here is the plan.") < texts.index(
            next(t for t in texts if t.startswith("# Summary of the conversation"))
        )

    def test_a_shadowed_message_keeps_its_original_text(
        self, agent_zero_session: Session
    ):
        """`set_summary` fills a field and leaves `content` alone -- the model
        stopped seeing the original, the file still has it."""
        event = next(
            e for e in agent_zero_session.events if e.meta.get("shadowed_by_summary")
        )
        assert event.meta["shadowed_by_summary"] == "embedded data removed"
        assert "here is the failing screen" in event.text

    def test_embedded_images_are_named_not_inlined(self, agent_zero_session: Session):
        """`trim_embeds` only shadows an embed, so the base64 stays in the file
        forever. A shape record measuring it would report the encoding."""
        event = next(e for e in agent_zero_session.events if "[image_url" in e.text)
        assert "iVBORw0KGgo" not in event.text

    def test_token_accounting_survives_the_join(self, agent_zero_session: Session):
        """Usage exists only on the history side."""
        event = next(e for e in agent_zero_session.events if "responses" in e.meta)
        assert event.meta["responses"]["usage"]["input_tokens"] == 8140
        assert "output_items" not in event.meta["responses"], (
            "the provider's raw item list can be the size of the conversation"
        )


class TestSilentLosses:
    def test_the_dropped_start_of_a_session_is_counted_and_explained(
        self, agent_zero_session: Session
    ):
        notice = next(
            e for e in agent_zero_session.events if e.meta.get("log_items_dropped")
        )
        assert notice.meta["log_items_dropped"] == 812
        assert "renumber" in notice.text or "renumbers" in notice.text, (
            "a zero first ordinal is not proof of completeness, and the event "
            "has to say so or the number reads as a guarantee"
        )

    def test_a_session_that_lost_nothing_gets_no_notice(
        self, agent_zero_compacted_session: Session
    ):
        assert not any(
            e.meta.get("log_items_dropped") for e in agent_zero_compacted_session.events
        )

    def test_the_displays_own_truncation_is_recorded_with_its_cap(
        self, agent_zero_session: Session
    ):
        """15000 is a multiple of 1000, so `is_round_number` fires on a length
        that is Agent Zero's UI limit rather than any tool's behaviour. The
        marker's own number is carried so the agent can tell the two apart."""
        result = _result(agent_zero_session, "search_engine")
        assert result.shape.is_round_number
        assert result.meta["log_truncation"]["cap_chars"] == 15000
        assert result.meta["log_truncation"]["chars_hidden"] > 0

    def test_the_payloads_own_truncation_is_a_different_claim(
        self, agent_zero_session: Session
    ):
        """This one says the model never saw the rest."""
        result = _result(agent_zero_session, "code_execution_tool")
        assert result.meta["payload_truncation"] == {"chars_removed": 412}
        assert "CHARACTERS REMOVED TO SAVE SPACE" in result.text, "never stripped"

    def test_a_collapsed_attention_window_is_counted(self, agent_zero_session: Session):
        """The one history path that deletes rather than shadows. The gap in
        `sequence` gives the number exactly."""
        note = next(
            e for e in agent_zero_session.events if e.meta.get("history_note") == "collapsed"
        )
        assert note.meta["messages_deleted"] == 2
        assert note.meta["sequence_from"] == 4 and note.meta["sequence_to"] == 7

    def test_summaries_that_now_stand_in_for_the_conversation_are_events(
        self, agent_zero_session: Session
    ):
        notes = {
            e.meta.get("history_note")
            for e in agent_zero_session.events
            if e.meta.get("history_note")
        }
        assert {"bulk_summary", "topic_summary", "collapsed"} <= notes


class TestShapeRecords:
    def test_no_tool_result_ever_carries_a_duration(self, agent_zero_session: Session):
        """The item's timestamp is stamped when the call starts and never
        updated. The gap to the next item is not when the tool returned, and a
        number in that column would be read as though it were."""
        for event in agent_zero_session.events:
            if event.shape is not None:
                assert event.shape.duration_ms is None
                assert event.shape.duration_source == "unavailable"

    def test_no_tool_result_ever_carries_an_exit_code(self, agent_zero_session: Session):
        """The code execution tool renders one into a framework sentence. A
        wrong parse would be indistinguishable from a recorded code."""
        for event in agent_zero_session.events:
            if event.shape is not None:
                assert event.shape.exit_code is None

    def test_a_call_that_never_returned_reads_as_empty(
        self, agent_zero_session: Session
    ):
        """The container stopped, or the user hit stop. The item is still here
        with the arguments it was created with and no result."""
        result = _result(agent_zero_session, "document_query")
        assert result.shape.is_empty
        assert "E" in _flags_for(result)

    def test_the_call_and_its_result_cannot_be_mispaired(
        self, agent_zero_session: Session
    ):
        """Both halves are written into the same log item, so the pairing is by
        identity rather than by a join or by position."""
        for event in agent_zero_session.events:
            if event.kind == "tool_result" and not event.meta.get("from_history"):
                call = agent_zero_session.events[event.meta["call_event_index"]]
                assert call.kind == "tool_call"
                assert call.tool_name == event.tool_name
                assert call.meta["item_position"] == event.meta["item_position"]


class TestSubordinateAgents:
    def test_a_subordinates_events_are_marked(self, agent_zero_session: Session):
        """One log, one chain of agents, one number to tell them apart."""
        subordinate = [e for e in agent_zero_session.events if e.meta.get("agent_no") == 1]
        assert {e.kind for e in subordinate} == {"message", "tool_call", "tool_result"}

    def test_the_top_level_agent_is_not_marked(self, agent_zero_session: Session):
        top = next(e for e in agent_zero_session.events if e.kind == "thinking")
        assert "agent_no" not in top.meta, "0 is the common case and needs no flag"

    def test_the_outline_says_whose_event_a_row_is(self, agent_zero_session: Session):
        outline = render_outline(agent_zero_session)
        assert " A1: " in outline
        assert "subordinate agent" in outline

    def test_the_outline_explains_the_undated_rows(self, agent_zero_session: Session):
        outline = render_outline(agent_zero_session)
        assert "second record" in outline


class TestReasoning:
    def test_the_reasoning_stream_is_kept_as_text(self, agent_zero_session: Session):
        """Agent Zero is the only supported source that persists the provider's
        reasoning as text rather than as a signature."""
        thinking = next(e for e in agent_zero_session.events if e.kind == "thinking")
        assert "The suite is pytest" in thinking.text

    def test_the_models_intended_tool_is_not_a_second_call_event(
        self, agent_zero_session: Session
    ):
        """`kvps.tool_name` on an agent item is the tool the model decided to
        call. The call itself is the item that follows, and counting both would
        double every call in the corpus statistics."""
        turn = next(
            e
            for e in agent_zero_session.events
            if e.kind == "message" and e.meta.get("intended_tool")
        )
        assert turn.meta["intended_tool"] == "code_execution_tool"
        calls = [
            e
            for e in agent_zero_session.events
            if e.kind == "tool_call" and e.tool_name == "code_execution_tool"
        ]
        assert len(calls) == 1


class TestCompaction:
    def test_a_compacted_chat_holds_only_its_summary(
        self, agent_zero_compacted_session: Session
    ):
        """`/compact` calls `log.reset()` and replaces the history with one
        message. This is what is left of a whole session.

        It is left twice, and that is the format rather than a bug: the
        compactor writes the summary into the log without an id and into a fresh
        history whose message gets a new uuid, so there is nothing to join them
        by. One row carries the time it happened and one does not.
        """
        messages = [e for e in agent_zero_compacted_session.events if e.kind == "message"]
        assert [e.meta.get("from_history", False) for e in messages] == [False, True]
        assert all("Context compacted" in e.text for e in messages)
        assert not [
            e for e in agent_zero_compacted_session.events if e.kind == "tool_result"
        ], "every tool call this session made is gone from the live chat"

    def test_the_backup_holds_what_the_chat_lost(
        self, agent_zero_backup_session: Session
    ):
        tools = {
            e.tool_name for e in agent_zero_backup_session.events if e.kind == "tool_result"
        }
        assert "code_execution_tool" in tools


# -- helpers ------------------------------------------------------------


def _flags_for(event) -> str:
    from ossuary.outline import _flags

    return _flags(event)


def _log_item(no: int, item_type: str, *, content: str = "", kvps: dict | None = None) -> dict:
    return {
        "no": no,
        "id": None,
        "type": item_type,
        "heading": "",
        "content": content,
        "kvps": kvps or {},
        "timestamp": 1787040000.0,
        "agentno": 0,
    }


def _parse_chat(tmp_path: Path, *, logs: list[dict]) -> Session:
    chat = {
        "id": "ctx-tmp",
        "name": "tmp",
        "created_at": "2026-08-16T09:00:00+02:00",
        "type": "user",
        "last_message": "2026-08-16T09:00:00+02:00",
        "agents": [{"number": 0, "agent_profile": "", "data": {}, "history": ""}],
        "streaming_agent": 0,
        "log": {"guid": "g", "logs": logs, "progress": "", "progress_no": 0},
        "data": {},
        "output_data": {},
    }
    directory = tmp_path / "ctx-tmp"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chat.json").write_text(json.dumps(chat), encoding="utf-8")
    adapter = get_adapter("agent-zero", roots=[tmp_path])
    return adapter.parse(adapter.discover([tmp_path])[0])
