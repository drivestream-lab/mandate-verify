"""The merge freeze (B0-02). Pure functions: facts in, one verdict out.

While the runner has an open freeze issue (a red run on develop), every PR into the
task base fails with "develop is red". The one way through is the fix itself: a PR
whose body closes the issue (`Fixes #N`, or `owner/repo#N` from another repo) and
that has the human signer's Approve on its head.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import Config
from .models import Issue, PullRequest, Review, Verdict
from .rules import signer_approval

RULE = "develop-red"
_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+)\b",
    re.IGNORECASE,
)


def freeze_issues(issues: Iterable[Issue], cfg: Config) -> list[Issue]:
    """Only issues the runner itself opened can freeze; anyone else's label is ignored."""
    return sorted((i for i in issues if i.author == cfg.identities.runner_login), key=lambda i: i.number)


def closes(pr: PullRequest, issue: Issue) -> bool:
    """The PR body closes the issue. A bare #N only counts in the issue's own repo."""
    for m in _CLOSES.finditer(pr.body or ""):
        if int(m.group("number")) != issue.number:
            continue
        repo = m.group("repo")
        if (repo or pr.repo).lower() == issue.repo.lower():
            return True
    return False


def freeze_verdict(pr: PullRequest, reviews: Iterable[Review], issues: list[Issue], cfg: Config) -> Verdict | None:
    """A failing verdict while frozen, or None when the PR is graded as usual."""
    if cfg.develop_run is None or pr.base_ref != cfg.develop_run.task_base or not issues:
        return None
    issue = issues[0]
    ref = f"#{issue.number}" if pr.repo.lower() == issue.repo.lower() else f"{issue.repo}#{issue.number}"
    if all(closes(pr, i) for i in issues) and signer_approval(pr, list(reviews), cfg) is not None:
        return None
    signer = cfg.identities.human_signer_login
    return Verdict(
        ok=False,
        rule=RULE,
        description=f"develop is red ({ref}): only a PR that Fixes {ref} with {signer}'s Approve can merge",
    )
