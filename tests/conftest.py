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
def loaded_store(claude_session: Session) -> SessionStore:
    store = SessionStore()
    store.add(claude_session)
    return store
