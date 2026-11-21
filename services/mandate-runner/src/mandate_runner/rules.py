"""The review rules (PL-16). Pure functions: facts in, one verdict out.

Only reviews on the current head count. For each identity (and, for the
challenger, each marker slot) the latest decisive review on the head decides:
an Approve counts until a later Request changes or a dismissal replaces it.
Comment-only reviews never count and never cancel.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from .config import Config, RepoRules
from .models import Diff, PullRequest, Review, Verdict
from .paths import first_match

DECISIVE = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
PYPROJECT_TOOL = "pyproject.toml [tool]"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _lines(body: str) -> list[str]:
    return [line.strip() for line in (body or "").splitlines()]


def _approved(reviews: Iterable[Review], head_sha: str, counts: Callable[[Review], bool]) -> Review | None:
    """The latest decisive matching review on the head, if it is an Approve."""
    decisive = [r for r in reviews if r.commit_id == head_sha and r.state in DECISIVE and counts(r)]
    if not decisive:
        return None
    latest = max(decisive, key=lambda r: r.id)
    return latest if latest.state == "APPROVED" else None


def challenger_slot(review: Review, cfg: Config) -> str | None:
    """'first' or 'second' when the body carries exactly one marker line."""
    lines = _lines(review.body)
    has_first = cfg.markers.first.strip() in lines
    has_second = cfg.markers.second.strip() in lines
    if has_first == has_second:
        return None
    return "first" if has_first else "second"


def challenger_approved(pr: PullRequest, reviews: Iterable[Review], cfg: Config, slot: str) -> bool:
    login = cfg.identities.challenger_login
    return _approved(reviews, pr.head_sha, lambda r: r.login == login and challenger_slot(r, cfg) == slot) is not None


def signer_approval(pr: PullRequest, reviews: Iterable[Review], cfg: Config) -> Review | None:
    login = cfg.identities.human_signer_login
    return _approved(reviews, pr.head_sha, lambda r: r.login == login)


def carries_attestation(review: Review, pr: PullRequest, cfg: Config) -> bool:
    """`Initiative: <initiative>` and `Head: <full head sha>` lines in the body."""
    if not _SHA.match(pr.head_sha):
        return False
    lines = _lines(review.body)
    return f"Initiative: {cfg.wave.initiative}" in lines and f"Head: {pr.head_sha}" in lines


def protected_touch(diff: Diff, repo: RepoRules) -> str | None:
    hit = first_match(diff.paths, repo.protected_paths)
    if hit is None and repo.pyproject_tool_settings_protected and diff.pyproject_tool_changed:
        return PYPROJECT_TOOL
    return hit


def trust_critical_touch(diff: Diff, repo: RepoRules) -> str | None:
    return first_match(diff.paths, repo.trust_critical_paths)


def _fail(rule: str, description: str) -> Verdict:
    return Verdict(ok=False, rule=rule, description=description)


def is_wave_pr(pr: PullRequest, cfg: Config) -> bool:
    return pr.base_ref == cfg.wave.base_branch


def evaluate(pr: PullRequest, reviews: list[Review], diff: Diff, cfg: Config) -> Verdict:
    """The first rule that fails, or ok. Commands are graded separately."""
    ids = cfg.identities
    repo = cfg.repo(pr.repo)

    if is_wave_pr(pr, cfg):
        wave = cfg.wave
        if pr.head_ref != wave.head_branch or pr.head_repo != pr.repo:
            return _fail("wave-source", f"{wave.base_branch} accepts PRs from {wave.head_branch} only")
        if wave.label not in pr.labels:
            return _fail("wave-label", f"Wave PR needs the label {wave.label}")
        if pr.draft:
            return _fail("wave-ready", "Wave PR is a draft: mark it ready for review")
        approval = signer_approval(pr, reviews, cfg)
        if approval is None:
            return _fail("wave-signer-approve", f"Wave PR needs an Approve from {ids.human_signer_login} on this head")
        if not carries_attestation(approval, pr, cfg):
            return _fail(
                "wave-attestation",
                f"{ids.human_signer_login}'s Approve lacks the attestation "
                f"(Initiative: {cfg.wave.initiative}, Head: <full head sha>)",
            )
        return Verdict(ok=True, rule="wave", description="Commands passed; wave signoff holds on this head")

    if not challenger_approved(pr, reviews, cfg, "first"):
        return _fail(
            "challenger-approve",
            f"Needs an Approve from {ids.challenger_app_slug} ({cfg.markers.first}) on this head",
        )
    critical = trust_critical_touch(diff, repo)
    if critical is not None and not challenger_approved(pr, reviews, cfg, "second"):
        return _fail(
            "second-challenger-approve",
            f"{critical} is trust-critical: needs a separate {ids.challenger_app_slug} Approve "
            f"({cfg.markers.second}) on this head",
        )
    protected = protected_touch(diff, repo)
    if protected is not None and signer_approval(pr, reviews, cfg) is None:
        return _fail(
            "signer-protected-approve",
            f"{protected} is protected: needs an Approve from {ids.human_signer_login} on this head",
        )
    return Verdict(ok=True, rule="task", description="Commands passed; review rules hold on this head")


def fit_description(text: str, limit: int = 140) -> str:
    """GitHub truncates status descriptions at 140 characters; do it predictably."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
