"""The runner's App credentials, and redaction for everything the runner writes.

The key, the App JWT and installation tokens are never printed or logged.
Every log line and every run log passes through `redact` as a backstop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.DOTALL),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(authorization:\s*)(basic|bearer|token)\s+\S+"),
)


def redact(text: str) -> str:
    for rx in _PATTERNS:
        text = rx.sub(lambda m: (m.group(1) if m.re.groups else "") + "[redacted]", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class SecretsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppCredentials:
    app_id: int
    installation_id: int
    private_key: bytes = field(repr=False)


def load_credentials(directory: Path) -> AppCredentials:
    """Reads app-id, installation-id and private-key.pem. Errors name files, never content."""

    def read(name: str) -> bytes:
        path = directory / name
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SecretsError(f"cannot read {path}: {exc.strerror}") from None

    def integer(name: str) -> int:
        raw = read(name).decode("ascii", "replace").strip()
        if not raw.isdigit():
            raise SecretsError(f"{directory / name} must hold a number")
        return int(raw)

    key = read("private-key.pem")
    if b"PRIVATE KEY-----" not in key:
        raise SecretsError(f"{directory / 'private-key.pem'} is not a PEM private key")
    return AppCredentials(app_id=integer("app-id"), installation_id=integer("installation-id"), private_key=key)
