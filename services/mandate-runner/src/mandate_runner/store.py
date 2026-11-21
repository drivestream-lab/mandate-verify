"""What the runner keeps on disk: command results per head, the last status posted, run logs.

A command result is keyed by repo, head sha and a digest of the command set, so a
head is graded once per command set and a restart does not grade it again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import CommandsResult
from .secrets import redact

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def commands_digest(commands: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(commands).encode()).hexdigest()[:12]


def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def new_run_id(repo: str, sha: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{repo_slug(repo)}-{sha[:12]}-{stamp}"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.logs_dir = root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _result_path(self, repo: str, sha: str, digest: str) -> Path:
        return self.root / "results" / repo_slug(repo) / f"{sha}-{digest}.json"

    def _posted_path(self, repo: str, sha: str) -> Path:
        return self.root / "posted" / repo_slug(repo) / f"{sha}.json"

    def load_result(self, repo: str, sha: str, digest: str) -> CommandsResult | None:
        data = _read_json(self._result_path(repo, sha, digest))
        return CommandsResult(**data) if data is not None else None

    def save_result(self, repo: str, sha: str, digest: str, result: CommandsResult) -> None:
        _write_json(self._result_path(repo, sha, digest), asdict(result))

    def load_posted(self, repo: str, sha: str) -> tuple[str, str, str | None] | None:
        data = _read_json(self._posted_path(repo, sha))
        return (data["state"], data["description"], data.get("target_url")) if data else None

    def save_posted(self, repo: str, sha: str, state: str, description: str, target_url: str | None) -> None:
        _write_json(
            self._posted_path(repo, sha), {"state": state, "description": description, "target_url": target_url}
        )

    def log_path(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise ValueError("bad run id")
        return self.logs_dir / f"{run_id}.log"

    def open_log(self, run_id: str) -> RunLog:
        return RunLog(self.log_path(run_id))


class RunLog:
    """Append-only, redacted, line-timestamped log for one run."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = redact(text).rstrip("\n").split("\n")
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"{stamp} {line}\n")
