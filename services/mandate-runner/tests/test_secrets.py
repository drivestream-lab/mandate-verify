import logging

import pytest

from mandate_runner.executor import _git_auth_env
from mandate_runner.secrets import RedactingFormatter, SecretsError, load_credentials, redact

KEY = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----\n"


def test_credentials_load_and_never_show_the_key(tmp_path):
    (tmp_path / "app-id").write_text("5860839\n")
    (tmp_path / "installation-id").write_text("42\n")
    (tmp_path / "private-key.pem").write_bytes(KEY)
    creds = load_credentials(tmp_path)
    assert (creds.app_id, creds.installation_id) == (5860839, 42)
    assert "MIIE" not in repr(creds) and "PRIVATE" not in repr(creds)


def test_missing_secret_error_names_the_file_not_content(tmp_path):
    (tmp_path / "app-id").write_text("1")
    (tmp_path / "installation-id").write_text("2")
    with pytest.raises(SecretsError, match="private-key.pem"):
        load_credentials(tmp_path)


def test_redaction_covers_keys_tokens_and_jwts():
    text = KEY.decode() + " ghs_abcdefghijklmnopqrstuvwxyz0123 eyJhbGciOi.eyJpc3MiOi.c2lnbmF0dXJl"
    out = redact(text)
    assert "MIIE" not in out and "ghs_" not in out and "eyJ" not in out


def test_redaction_covers_git_auth_header():
    value = _git_auth_env("ghs_abcdefghijklmnopqrstuvwxyz0123")["GIT_CONFIG_VALUE_0"]
    assert "basic" in value
    assert redact(f"header {value}") == "header AUTHORIZATION: [redacted]"


def test_log_formatter_redacts():
    record = logging.LogRecord("x", logging.INFO, "f", 1, "token %s", ("ghs_abcdefghijklmnopqrstuvwxyz0123",), None)
    assert "ghs_" not in RedactingFormatter("%(message)s").format(record)
