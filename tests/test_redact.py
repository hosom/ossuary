from __future__ import annotations

import pytest

from ossuary.redact import Redactor


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(enabled=True, redact_env_values=False)


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-123456789012-abcdefghijklmno",
        "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_known_secret_shapes_are_masked(redactor: Redactor, secret: str):
    result = redactor.redact(f"the token is {secret} ok")
    assert secret not in result.text
    assert "ossuary:redacted" in result.text
    assert result.total >= 1


def test_assigned_secrets_keep_their_variable_name(redactor: Redactor):
    result = redactor.redact('AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMIabcdEXAMPLEKEY"')
    assert "AWS_SECRET_ACCESS_KEY" in result.text, "the name is the signal, keep it"
    assert "wJalrXUtnFEMIabcdEXAMPLEKEY" not in result.text


def test_url_credentials_are_stripped_but_scheme_survives(redactor: Redactor):
    result = redactor.redact("clone https://user:hunter2@github.com/org/repo.git")
    assert "hunter2" not in result.text
    assert "https://" in result.text
    assert "github.com" in result.text


def test_private_keys_are_masked(redactor: Redactor):
    blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc123\n-----END RSA PRIVATE KEY-----"
    result = redactor.redact(blob)
    assert "MIIEabc123" not in result.text


def test_authorization_headers_are_masked(redactor: Redactor):
    result = redactor.redact("Authorization: Bearer abcdefghijklmnop123456")
    assert "abcdefghijklmnop123456" not in result.text
    assert "Authorization" in result.text


def test_ordinary_code_survives_intact(redactor: Redactor):
    code = (
        "def compute_shape(text: str) -> ShapeRecord:\n"
        "    return ShapeRecord(byte_length=len(text))\n"
        "# see src/ossuary/shape.py line 42\n"
        "commit deadbeefcafebabe0123456789abcdef01234567\n"
    )
    result = redactor.redact(code)
    assert result.text == code, f"redaction damaged ordinary code: {result.counts}"


def test_file_paths_and_hex_digests_are_not_masked(redactor: Redactor):
    text = "/home/user/project/src/very/long/path/to/a/module_name_here.py and 0123456789abcdef0123456789abcdef01234567"
    assert redactor.redact(text).text == text


def test_redaction_roughly_preserves_byte_length(redactor: Redactor):
    """Shape records are computed from disk text; redaction must not distort sizes."""
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    original = f"token={secret}"
    redacted = redactor.redact(original).text
    assert len(redacted) == pytest.approx(len(original), abs=4)


def test_no_redact_escape_hatch_is_exact():
    off = Redactor(enabled=False)
    text = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"
    assert off.redact(text).text == text
    assert off.redact(text).total == 0


def test_session_ids_are_never_masked(redactor: Redactor):
    """They are the primary key Agent A addresses transcripts by."""
    session_id = "1999fd32-6655-58f8-8575-58beed8fe404"
    assert session_id in redactor.redact(f"SESSION {session_id} source=claude-code").text


def test_env_var_values_are_masked_when_enabled(monkeypatch):
    monkeypatch.setenv("MY_APP_TOKEN", "super-secret-value-12345")
    redactor = Redactor(enabled=True, redact_env_values=True)
    result = redactor.redact("the config used super-secret-value-12345 here")
    assert "super-secret-value-12345" not in result.text


def test_benign_env_vars_are_not_collected(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    redactor = Redactor(enabled=True, redact_env_values=True)
    assert "/usr/bin:/bin" in redactor.redact("PATH is /usr/bin:/bin").text
