from __future__ import annotations

from ossuary.elide import MARKER_RE, is_elided
from ossuary.models import Session
from ossuary.outline import _RULE, render_outline
from ossuary.store import SessionStore


class TestOutline:
    def test_every_event_gets_exactly_one_row(self, claude_session: Session):
        """The coverage guarantee: no event may be missing from the outline."""
        outline = render_outline(claude_session)
        for event in claude_session.events:
            rows = [
                line for line in outline.splitlines()
                if line.startswith(f"{event.index:<4} ") and len(line) > 20
            ]
            assert len(rows) == 1, f"event {event.index} has {len(rows)} rows"

    def test_row_budget_is_about_twenty_tokens(self, claude_session: Session):
        """~20 tokens/row is what makes a 400-event session fit in ~8k."""
        rows = [
            line for line in render_outline(claude_session).splitlines()
            if line[:4].strip().isdigit()
        ]
        assert rows
        mean_chars = sum(len(r) for r in rows) / len(rows)
        assert mean_chars / 3.5 < 25, f"{mean_chars / 3.5:.0f} tokens/row is over budget"

    def test_a_four_hundred_event_session_fits_in_the_stated_budget(self, claude_session: Session):
        rows = [
            line for line in render_outline(claude_session).splitlines()
            if line[:4].strip().isdigit()
        ]
        projected_tokens = (sum(len(r) for r in rows) / len(rows)) * 400 / 3.5
        assert projected_tokens < 11_000, f"400 events would cost ~{projected_tokens:.0f} tokens"

    def test_shape_signals_reach_the_row(self, claude_session: Session):
        outline = render_outline(claude_session)
        capped = next(e for e in claude_session.events if e.shape and e.shape.byte_length == 30000)
        row = next(l for l in outline.splitlines() if l.startswith(f"{capped.index:<4} "))
        assert "30000" in row
        assert "R" in row, "round-number flag must be on the row"
        assert "T" in row, "unclean-termination flag must be on the row"

    def test_legend_explains_every_abbreviation(self, claude_session: Session):
        outline = render_outline(claude_session)
        legend = outline.split(_RULE)[-1]
        for kind in {e.kind for e in claude_session.events}:
            from ossuary.outline import _KIND_ABBREV

            assert _KIND_ABBREV[kind] in legend, f"kind {kind} not explained"
        for role in {e.role for e in claude_session.events}:
            from ossuary.outline import _ROLE_ABBREV

            assert _ROLE_ABBREV[role] in legend, f"role {role} not explained"

    def test_parse_errors_are_announced_at_the_top(self, claude_session: Session):
        outline = render_outline(claude_session)
        assert "failed to parse" in outline


class TestStoreReads:
    def test_read_events_returns_the_requested_range(self, loaded_store: SessionStore, claude_session: Session):
        out = loaded_store.read_events(claude_session.session_id, 0, 3)
        assert "--- event 0 ---" in out
        assert "--- event 2 ---" in out
        assert "--- event 3 ---" not in out

    def test_read_events_out_of_range_says_so_clearly(self, loaded_store: SessionStore, claude_session: Session):
        out = loaded_store.read_events(claude_session.session_id, 9999, 10001)
        assert "No events in range" in out
        assert str(len(claude_session.events)) in out

    def test_oversized_payloads_are_elided_with_a_marker(self, loaded_store: SessionStore, claude_session: Session):
        capped = next(e for e in claude_session.events if e.shape and e.shape.byte_length == 30000)
        out = loaded_store.read_events(claude_session.session_id, capped.index, capped.index + 1)
        assert is_elided(out), "a shortened payload must always be labelled"

    def test_small_payloads_are_never_marked(self, loaded_store: SessionStore, claude_session: Session):
        small = next(
            e for e in claude_session.events
            if e.kind == "tool_result" and e.shape and 0 < e.shape.byte_length < 200
        )
        out = loaded_store.read_events(claude_session.session_id, small.index, small.index + 1)
        assert not is_elided(out), "an untruncated payload must carry no marker"

    def test_slice_paging_reports_true_totals(self, loaded_store: SessionStore, claude_session: Session):
        capped = next(e for e in claude_session.events if e.shape and e.shape.byte_length == 30000)
        page = loaded_store.read_event_slice(claude_session.session_id, capped.index, 0, 1000)
        assert "of 30000" in page
        assert "bytes [0, 1000)" in page
        marker = MARKER_RE.search(page)
        assert marker and int(marker.group(2)) == 30000

    def test_slice_paging_walks_forward(self, loaded_store: SessionStore, claude_session: Session):
        capped = next(e for e in claude_session.events if e.shape and e.shape.byte_length == 30000)
        second = loaded_store.read_event_slice(claude_session.session_id, capped.index, 1000, 1000)
        assert "bytes [1000, 2000)" in second
        assert "(before this window)" in second

    def test_slice_past_the_end_is_explicit(self, loaded_store: SessionStore, claude_session: Session):
        capped = next(e for e in claude_session.events if e.shape and e.shape.byte_length == 30000)
        out = loaded_store.read_event_slice(claude_session.session_id, capped.index, 99999, 100)
        assert "past the end" in out

    def test_search_returns_event_indices(self, loaded_store: SessionStore, claude_session: Session):
        out = loaded_store.search_session(claude_session.session_id, r"nothing to commit")
        assert "event" in out
        assert "matched" in out

    def test_search_with_no_hits_says_so(self, loaded_store: SessionStore, claude_session: Session):
        out = loaded_store.search_session(claude_session.session_id, r"zzz-not-present-zzz")
        assert "No matches" in out

    def test_invalid_regex_is_reported_not_raised(self, loaded_store: SessionStore, claude_session: Session):
        out = loaded_store.search_session(claude_session.session_id, r"(unclosed")
        assert "Invalid regular expression" in out

    def test_unknown_session_raises_a_useful_error(self, loaded_store: SessionStore):
        import pytest

        with pytest.raises(KeyError, match="not loaded"):
            loaded_store.read_events("no-such-session", 0, 1)

    def test_redaction_applies_to_every_read_path(self, claude_session: Session):
        from ossuary.redact import Redactor

        store = SessionStore(redactor=Redactor(enabled=True, redact_env_values=False))
        store.add(claude_session)
        secret_event = next(
            e for e in claude_session.events if "wJalrXUtnFEMIK7MDENG" in e.text
        )
        out = store.read_events(claude_session.session_id, secret_event.index, secret_event.index + 1)
        assert "wJalrXUtnFEMIK7MDENG" not in out
        assert "ossuary:redacted" in out
