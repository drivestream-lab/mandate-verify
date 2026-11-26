"""The merge freeze rule (B0-02): while develop is red, only the signed fix gets through."""

from __future__ import annotations

from dataclasses import replace

from conftest import HEAD, KD, OLD, pr, review

from mandate_runner.freeze import closes, freeze_issues, freeze_verdict
from mandate_runner.models import Issue

MANDATE = "drivestream-lab/mandate"
TOWER = "drivestream-lab/mandate-control-tower"
RED = Issue(
    repo=MANDATE, number=21, url="https://github.com/drivestream-lab/mandate/issues/21", author="mandate-runner[bot]"
)


def kd_approve(commit: str = HEAD):
    return review(KD, "APPROVED", "Fix looks right.", commit)


def test_no_open_issue_no_freeze(cfg):
    assert freeze_verdict(pr(), [], [], cfg) is None


def test_task_pr_that_would_pass_is_frozen(cfg):
    verdict = freeze_verdict(pr(), [kd_approve()], [RED], cfg)
    assert verdict is not None and not verdict.ok
    assert verdict.rule == "develop-red"
    assert verdict.description.startswith("develop is red (#21)")


def test_task_pr_on_control_tower_is_frozen_too(cfg):
    verdict = freeze_verdict(pr(repo=TOWER), [], [RED], cfg)
    assert verdict is not None
    assert "drivestream-lab/mandate#21" in verdict.description


def test_wave_pr_is_not_a_task_pr(cfg):
    assert freeze_verdict(pr(base="main", head_ref="develop"), [], [RED], cfg) is None


def test_fix_pr_with_the_signers_approve_is_graded_as_usual(cfg):
    fix = replace(pr(), body="Make the journey pass again.\n\nFixes #21")
    assert freeze_verdict(fix, [kd_approve()], [RED], cfg) is None


def test_fix_pr_without_the_signers_approve_stays_frozen(cfg):
    fix = replace(pr(), body="Fixes #21")
    assert freeze_verdict(fix, [], [RED], cfg) is not None


def test_signers_approve_on_an_older_head_does_not_count(cfg):
    fix = replace(pr(), body="Fixes #21")
    assert freeze_verdict(fix, [kd_approve(commit=OLD)], [RED], cfg) is not None


def test_signers_approve_without_fixes_does_not_unfreeze(cfg):
    assert freeze_verdict(pr(), [kd_approve()], [RED], cfg) is not None


def test_fix_must_close_every_open_freeze_issue(cfg):
    other = replace(RED, number=22)
    fix = replace(pr(), body="Fixes #21")
    assert freeze_verdict(fix, [kd_approve()], [RED, other], cfg) is not None
    both = replace(pr(), body="Fixes #21\nFixes #22")
    assert freeze_verdict(both, [kd_approve()], [RED, other], cfg) is None


def test_closing_keywords():
    for body in ("Fixes #21", "fixed #21", "Closes #21", "resolves: #21", "Resolved #21"):
        assert closes(replace(pr(), body=body), RED), body
    for body in ("Refs #21", "Fixes #210", "#21", "Fixes #2"):
        assert not closes(replace(pr(), body=body), RED), body


def test_bare_number_only_counts_in_the_issues_repo():
    assert not closes(replace(pr(repo=TOWER), body="Fixes #21"), RED)  # that is control-tower's #21
    assert closes(replace(pr(repo=TOWER), body="Fixes drivestream-lab/mandate#21"), RED)
    assert not closes(replace(pr(), body="Fixes drivestream-lab/other#21"), RED)


def test_only_the_runners_own_issues_freeze(cfg):
    planted = replace(RED, number=5, author="mandate-builder[bot]")
    assert freeze_issues([planted, RED], cfg) == [RED]
