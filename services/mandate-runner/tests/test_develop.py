"""After a merge into develop (B0-02): run once, open the freeze issue on red, close it on green.
Also the sandbox rebuilds: daily by GitHub's clock, and after any run that did not pass."""

from __future__ import annotations

from dataclasses import replace

from conftest import HEAD, KD, review
from test_service import FakeExecutor, FakeGitHub, InlinePool

from mandate_runner.models import Issue
from mandate_runner.service import Runner
from mandate_runner.store import Store

MANDATE = "drivestream-lab/mandate"
MERGE = "1" * 40
FIX = "2" * 40


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def setup(cfg, tmp_path, develop_result=None, clock=None):
    gh = FakeGitHub()
    gh.heads[(MANDATE, "develop")] = MERGE
    ex = FakeExecutor()
    ex.develop_result = develop_result
    runner = Runner(cfg, gh, ex, Store(tmp_path), pool=InlinePool(), clock=clock or Clock())
    return runner, gh, ex


def last_status(gh):
    return gh.statuses[-1][2], gh.statuses[-1][3], gh.statuses[-1][4]


def test_nothing_to_deploy_yet_passes_and_opens_no_issue(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path)
    runner.poll_once()
    assert ex.develop_runs == [(MANDATE, MERGE, ("make setup", "make verify"))]
    assert gh.opened == [] and gh.closed == []
    assert last_status(gh)[0] == "success"  # the task PR is not frozen


def test_develop_head_is_run_once(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "green")
    for _ in range(4):
        runner.poll_once()
    assert len(ex.develop_runs) == 1


def test_red_run_opens_one_issue_linked_to_the_merge_and_the_log(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    runner.poll_once()
    assert len(gh.opened) == 1
    repo, title, body, label = gh.opened[0]
    assert repo == MANDATE and label == "develop-red"
    assert title == f"develop is red at {MERGE[:12]}: make verify failed"
    assert f"https://github.com/{MANDATE}/commit/{MERGE}" in body
    assert "http://localhost:8090/runs/" in body
    assert "Fixes #" in body


def test_red_develop_freezes_a_task_pr_that_would_otherwise_pass(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    state, description, url = last_status(gh)
    assert state == "failure"
    assert description.startswith("develop is red (#100)")
    assert url == gh.issues[0].url


def test_signed_fix_pr_is_graded_as_usual_while_frozen(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    issue = gh.issues[0]
    gh.prs[MANDATE] = [replace(gh.prs[MANDATE][0], body=f"Fixes #{issue.number}")]
    gh.reviews[7] = [*gh.reviews[7], review(KD, "APPROVED", "ok", HEAD)]
    runner.poll_once()
    assert last_status(gh)[:2] == ("success", "Commands passed; review rules hold on this head")


def test_green_run_after_the_fix_closes_the_issue_and_lifts_the_freeze(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    number = gh.issues[0].number
    gh.heads[(MANDATE, "develop")] = FIX  # the fix merged
    ex.develop_result = "green"
    runner.poll_once()
    assert [n for n, _ in gh.closed] == [number]
    assert FIX[:12] in gh.closed[0][1]
    assert gh.issues == []
    assert last_status(gh)[0] == "success"


def test_still_red_at_a_new_head_comments_instead_of_opening_another(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    gh.heads[(MANDATE, "develop")] = FIX
    runner.poll_once()
    assert len(gh.opened) == 1
    assert len(gh.comments) == 1 and gh.comments[0][0] == gh.issues[0].number
    assert "Still red" in gh.comments[0][1]


def test_issue_closed_by_hand_is_not_reopened_for_the_same_head(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    gh.issues = []  # KD closed it
    runner.poll_once()
    assert len(gh.opened) == 1
    assert last_status(gh)[0] == "success"


def test_issue_opened_by_someone_else_does_not_freeze(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path)
    gh.issues = [Issue(repo=MANDATE, number=9, url="u", author="mandate-builder[bot]")]
    runner.poll_once()
    assert last_status(gh)[0] == "success"


def test_infra_error_is_retried_and_reports_nothing(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "error")
    runner.poll_once()
    runner.poll_once()
    assert len(ex.develop_runs) == 2
    assert gh.opened == []


def test_never_grades_without_knowing_whether_develop_is_red(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path)

    def down(repo, label):
        raise RuntimeError("GitHub unreachable")

    gh.list_open_issues = down
    runner.poll_once()
    assert gh.statuses == []


def test_missing_develop_branch_is_skipped(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path)
    gh.heads.clear()
    runner.poll_once()
    assert ex.develop_runs == []
    assert last_status(gh)[0] == "success"


# --- a fresh sandbox ---------------------------------------------------------------


def test_sandbox_is_rebuilt_after_a_failed_pr_run(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path)
    ex.fail_on = "make test"
    runner.poll_once()
    assert ex.rebuilds == 1


def test_sandbox_is_rebuilt_after_a_red_develop_run_and_after_an_infra_error(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "red")
    runner.poll_once()
    assert ex.rebuilds == 1
    ex.develop_result = "error"
    gh.heads[(MANDATE, "develop")] = FIX
    runner.poll_once()
    assert ex.rebuilds == 2


def test_passing_runs_do_not_rebuild(cfg, tmp_path):
    runner, gh, ex = setup(cfg, tmp_path, "green")
    runner.poll_once()
    assert ex.rebuilds == 0


def test_sandbox_is_rebuilt_once_a_day(cfg, tmp_path):
    clock = Clock()
    runner, gh, ex = setup(cfg, tmp_path, "green", clock)
    runner.poll_once()  # first start: the day starts now
    clock.t += 23 * 3600
    runner.poll_once()
    assert ex.rebuilds == 0
    clock.t += 3600
    runner.poll_once()
    assert ex.rebuilds == 1
    clock.t += 3600
    runner.poll_once()
    assert ex.rebuilds == 1  # the day starts again from the rebuild


def test_clock_going_back_restarts_the_day_instead_of_rebuilding(cfg, tmp_path):
    clock = Clock()
    runner, gh, ex = setup(cfg, tmp_path, "green", clock)
    runner.poll_once()
    clock.t -= 7 * 24 * 3600  # e.g. a VM clock that lagged
    runner.poll_once()
    clock.t += 23 * 3600
    runner.poll_once()
    assert ex.rebuilds == 0
