"""Plain data the runner reads from GitHub and keeps between polls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Review:
    id: int
    login: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING
    commit_id: str
    body: str


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    head_sha: str
    head_ref: str
    head_repo: str
    base_ref: str
    base_sha: str
    draft: bool
    labels: frozenset[str]
    body: str = ""


@dataclass(frozen=True)
class Diff:
    """What a PR head changes relative to its merge base."""

    paths: tuple[str, ...]
    pyproject_tool_changed: bool


@dataclass(frozen=True)
class CommandsResult:
    passed: bool
    failed_command: str | None
    run_id: str
    timed_out: bool = False
    note: str | None = None  # e.g. "nothing to deploy yet"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    rule: str
    description: str


@dataclass(frozen=True)
class Issue:
    repo: str
    number: int
    url: str
    author: str
