from __future__ import annotations

import os
from pathlib import Path

import pytest

from mandate_runner import config as config_mod
from mandate_runner.models import Diff, PullRequest, Review

HEAD = "a" * 40
OLD = "b" * 40
CHALLENGER = "mandate-challenger[bot]"
KD = "nikd10x"


def config_path() -> Path:
    env = os.environ.get("MANDATE_RUNNER_CONFIG")
    candidates = [Path(env)] if env else []
    candidates += [p / "config" / "mandate-runner" / "runner.toml" for p in Path(__file__).resolve().parents]
    candidates.append(Path("/etc/mandate-runner/runner.toml"))
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("runner.toml not found")


@pytest.fixture(scope="session")
def cfg() -> config_mod.Config:
    """The real config in this repo, so the tests pin what the runner enforces."""
    return config_mod.load(config_path())


def pr(
    *,
    repo: str = "drivestream-lab/mandate",
    base: str = "develop",
    head_ref: str = "feature/INIT-MANDATE-001-w0-x",
    head_repo: str | None = None,
    sha: str = HEAD,
    draft: bool = False,
    labels: tuple[str, ...] = (),
    number: int = 7,
) -> PullRequest:
    return PullRequest(
        repo=repo,
        number=number,
        head_sha=sha,
        head_ref=head_ref,
        head_repo=head_repo or repo,
        base_ref=base,
        base_sha="c" * 40,
        draft=draft,
        labels=frozenset(labels),
    )


_ids = iter(range(1, 10_000_000))


def review(login: str, state: str = "APPROVED", body: str = "", commit: str = HEAD) -> Review:
    return Review(id=next(_ids), login=login, state=state, commit_id=commit, body=body)


def first(state: str = "APPROVED", commit: str = HEAD, extra: str = "") -> Review:
    return review(CHALLENGER, state, f"Looks right against the spec.\n\nChallenger: first\n{extra}", commit)


def second(state: str = "APPROVED", commit: str = HEAD) -> Review:
    return review(CHALLENGER, state, "Independent review.\nChallenger: second", commit)


def diff(*paths: str, tool_changed: bool = False) -> Diff:
    return Diff(paths=tuple(paths), pyproject_tool_changed=tool_changed)
