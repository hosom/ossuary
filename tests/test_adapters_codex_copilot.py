"""Adapters for the two sources with no data on this machine.

These fixtures are built to the documented/​source-derived schema, so they prove
the adapters' behaviour, not the schema's accuracy. Where a fixture and a real
file disagree, the real file wins and the adapter is the thing that changes.
"""

from __future__ import annotations

from ossuary.models import Session


class TestCodex:
    def test_session_id_comes_from_session_meta(self, codex_session: Session):
        assert codex_session.session_id == "abc123"
        assert codex_session.source == "codex"
        assert codex_session.project == "/home/user/proj"

    def test_all_events_carry_the_resolved_session_id(self, codex_session: Session):
        """`session_meta` may arrive after events; ids must not end up mixed."""
        assert {e.session_id for e in codex_session.events} == {"abc123"}

    def test_malformed_line_is_degraded_not_dropped(self, codex_session: Session):
        assert codex_session.parse_error_count == 1
        bad = [e for e in codex_session.events if e.kind == "unparseable"]
        assert len(bad) == 1
        assert bad[0].raw and bad[0].parse_error

    def test_function_call_arguments_are_kept_as_the_model_emitted_them(self, codex_session: Session):
        """The Responses API sends arguments as a JSON string; keep it verbatim."""
        call = next(e for e in codex_session.events if e.tool_name == "shell")
        assert call.kind == "tool_call"
        assert call.text.startswith("{") and "find ." in call.text

    def test_bare_string_output_is_read(self, codex_session: Session):
        result = next(
            e for e in codex_session.events
            if e.kind == "tool_result" and e.meta.get("call_id") == "call_1"
        )
        assert result.text == "42\n"
        assert result.tool_name == "shell", "joined to its call by call_id"

    def test_structured_output_with_success_false_sets_the_error_field(self, codex_session: Session):
        result = next(
            e for e in codex_session.events
            if e.kind == "tool_result" and e.meta.get("call_id") == "call_2"
        )
        assert result.text == "command failed"
        assert result.shape is not None and result.shape.has_error_field
        assert result.meta["success"] is False

    def test_local_shell_call_renders_its_command(self, codex_session: Session):
        call = next(e for e in codex_session.events if e.tool_name == "local_shell")
        assert "exit 1" in call.text

    def test_reasoning_becomes_a_thinking_event(self, codex_session: Session):
        thinking = [e for e in codex_session.events if e.kind == "thinking"]
        assert thinking and "I should use find" in thinking[0].text

    def test_unknown_item_type_is_preserved_with_an_explanation(self, codex_session: Session):
        unknown = [
            e for e in codex_session.events
            if e.parse_error and "brand_new_item_type" in (e.parse_error or "")
        ]
        assert len(unknown) == 1
        assert unknown[0].text, "the payload itself must survive"

    def test_meta_lines_are_kept(self, codex_session: Session):
        types = {e.meta.get("line_type") for e in codex_session.events}
        assert "session_meta" in types
        assert "turn_context" in types


class TestCopilotCli:
    def test_events_parse(self, copilot_cli_session: Session):
        assert copilot_cli_session.source == "copilot"
        assert copilot_cli_session.session_id == "sess-abc"
        kinds = [e.kind for e in copilot_cli_session.events]
        assert "message" in kinds and "tool_call" in kinds and "tool_result" in kinds

    def test_malformed_line_is_degraded_not_dropped(self, copilot_cli_session: Session):
        assert copilot_cli_session.parse_error_count == 1

    def test_recorded_duration_is_preferred_over_a_derived_one(self, copilot_cli_session: Session):
        result = next(e for e in copilot_cli_session.events if e.kind == "tool_result")
        assert result.shape is not None
        assert result.shape.duration_ms == 18000
        assert result.shape.duration_source == "recorded"

    def test_exit_code_is_read(self, copilot_cli_session: Session):
        result = next(e for e in copilot_cli_session.events if e.kind == "tool_result")
        assert result.shape.exit_code == 1

    def test_result_is_joined_to_its_call(self, copilot_cli_session: Session):
        result = next(e for e in copilot_cli_session.events if e.kind == "tool_result")
        assert result.tool_name == "bash"
        assert "call_event_index" in result.meta


class TestCopilotVsCode:
    """The case that is different in kind: JSON documents, not a line-per-event log."""

    def test_requests_become_paired_events(self, copilot_vscode_session: Session):
        roles = [e.role for e in copilot_vscode_session.events]
        assert roles.count("user") >= 2
        assert "assistant" in roles

    def test_markdown_response_parts_are_concatenated(self, copilot_vscode_session: Session):
        messages = [
            e.text for e in copilot_vscode_session.events
            if e.role == "assistant" and e.kind == "message"
        ]
        assert any("It sorts the list in place." == m for m in messages)

    def test_tool_invocations_become_call_and_result(self, copilot_vscode_session: Session):
        calls = [e for e in copilot_vscode_session.events if e.kind == "tool_call"]
        results = [e for e in copilot_vscode_session.events if e.kind == "tool_result"]
        assert calls and results
        assert calls[0].tool_name == "copilot_runTests"
        assert results[0].shape is not None and results[0].shape.has_error_field

    def test_request_level_errors_are_surfaced(self, copilot_vscode_session: Session):
        errors = [e for e in copilot_vscode_session.events if e.meta.get("result_error")]
        assert errors and "Tool call failed" in errors[0].text

    def test_an_unreadable_document_becomes_one_unparseable_event(self, tmp_path):
        """Never an empty session -- that would read as 'nothing happened here'."""
        from ossuary.adapters import get_adapter
        from ossuary.models import SessionRef

        bad = tmp_path / "chatSessions"
        bad.mkdir()
        path = bad / "broken.json"
        path.write_text('{"requests": [ this is not json', encoding="utf-8")

        session = get_adapter("copilot").parse(
            SessionRef(
                session_id="broken", source="copilot", path=str(path),
                size_bytes=path.stat().st_size,
            )
        )
        assert len(session.events) == 1
        assert session.events[0].kind == "unparseable"
        assert session.events[0].raw
