from __future__ import annotations

from pathlib import Path

import pytest

from ossuary.adapters import get_adapter
from ossuary.models import Session
from ossuary.store import SessionStore

GOLDEN = Path(__file__).parent / "golden"
CLAUDE_ROOT = GOLDEN / "claude-code" / "projects"
CODEX_ROOT = GOLDEN / "codex" / "sessions"
COPILOT_CLI_ROOT = GOLDEN / "copilot" / "session-state"
COPILOT_VSCODE_ROOT = GOLDEN / "copilot" / "vscode"
PI_ROOT = GOLDEN / "pi" / "sessions"
PI_LEGACY_ROOT = GOLDEN / "pi" / "legacy"
AGENT_ZERO_ROOT = GOLDEN / "agent-zero" / "chats"


def _parse_one(source: str, root: Path) -> Session:
    adapter = get_adapter(source, roots=[root])
    refs = adapter.discover([root])
    assert refs, f"no fixture sessions discovered under {root}"
    return adapter.parse(refs[0])


@pytest.fixture
def claude_session() -> Session:
    return _parse_one("claude-code", CLAUDE_ROOT)


@pytest.fixture
def codex_session() -> Session:
    return _parse_one("codex", CODEX_ROOT)


@pytest.fixture
def copilot_cli_session() -> Session:
    return _parse_one("copilot", COPILOT_CLI_ROOT)


@pytest.fixture
def copilot_vscode_session() -> Session:
    return _parse_one("copilot", COPILOT_VSCODE_ROOT)


@pytest.fixture
def pi_session() -> Session:
    return _parse_one("pi", PI_ROOT)


@pytest.fixture
def pi_legacy_session() -> Session:
    return _parse_one("pi", PI_LEGACY_ROOT)


def _parse_named(source: str, root: Path, session_id: str) -> Session:
    adapter = get_adapter(source, roots=[root])
    ref = next(r for r in adapter.discover([root]) if r.session_id == session_id)
    return adapter.parse(ref)


@pytest.fixture
def agent_zero_session() -> Session:
    return _parse_named("agent-zero", AGENT_ZERO_ROOT, "ctx-golden-0001")


@pytest.fixture
def agent_zero_compacted_session() -> Session:
    return _parse_named("agent-zero", AGENT_ZERO_ROOT, "ctx-golden-0002")


@pytest.fixture
def agent_zero_backup_session() -> Session:
    return _parse_named(
        "agent-zero", AGENT_ZERO_ROOT, "ctx-golden-0002@pre-compact-20260816-105012"
    )


@pytest.fixture
def loaded_store(claude_session: Session) -> SessionStore:
    store = SessionStore()
    store.add(claude_session)
    return store


@pytest.fixture
def golden_root() -> Path:
    """The fixture corpus root, for tools that discover rather than take a session."""
    return GOLDEN
