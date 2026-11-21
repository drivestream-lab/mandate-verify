"""The polling loop: once per head, re-check each poll, post only on change."""

from __future__ import annotations

from concurrent.futures import Future

import pytest
from conftest import HEAD, KD, first, pr, review, second

from mandate_runner.models import CommandsResult
from mandate_runner.service import Runner
from mandate_runner.store import Store

NEW = "d" * 40


class InlinePool:
    def submit(self, fn, *args):
        f: Future = Future()
        fn(*args)
        f.set_result(None)
        return f

    def shutdown(self, **_):
        pass


class DeferredPool:
    """Holds jobs until released, like a long make journeys."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        f: Future = Future()
        self.jobs.append((fn, args, f))
        return f

    def release(self):
        for fn, args, f in self.jobs:
            fn(*args)
            f.set_result(None)
        self.jobs.clear()

    def shutdown(self, **_):
        pass


class FakeGitHub:
    def __init__(self):
        self.prs = {"drivestream-lab/mandate": [pr()], "drivestream-lab/mandate-control-tower": []}
        self.reviews = {7: [first()]}
        self.paths = {7: ["src/business_services/programmes/x_service.py"]}
        self.files = {}
        self.statuses = []
        self.calls = {"list_changed_paths": 0}

    def list_open_prs(self, repo):
        return list(self.prs[repo])

    def list_reviews(self, repo, number):
        return list(self.reviews.get(number, []))

    def list_changed_paths(self, repo, number):
        self.calls["list_changed_paths"] += 1
        return list(self.paths[number])

    def merge_base(self, repo, base_sha, head_sha):
        return "e" * 40

    def file_at(self, repo, path, ref):
        return self.files.get(ref)

    def post_status(self, repo, sha, state, description, target_url):
        self.statuses.append((repo, sha, state, description, target_url))


class FakeExecutor:
    def __init__(self, fail_on=None, error=False):
        self.runs = []
        self.fail_on = fail_on
        self.error = error

    def run(self, repo, sha, commands, run_id, runlog):
        self.runs.append((repo, sha, commands))
        runlog.write("ran")
        if self.error:
            raise RuntimeError("sandbox unreachable")
        if self.fail_on in commands:
            return CommandsResult(passed=False, failed_command=self.fail_on, run_id=run_id)
        return CommandsResult(passed=True, failed_command=None, run_id=run_id)


@pytest.fixture
def gh():
    return FakeGitHub()


def make(cfg, gh, tmp_path, executor=None, pool=None):
    executor = executor or FakeExecutor()
    return Runner(cfg, gh, executor, Store(tmp_path), pool=pool or InlinePool()), executor


def states(gh):
    return [(s[1][:4], s[2]) for s in gh.statuses]


def test_unchanged_head_is_not_graded_twice(cfg, gh, tmp_path):
    runner, ex = make(cfg, gh, tmp_path)
    for _ in range(5):
        runner.poll_once()
    assert len(ex.runs) == 1
    assert ex.runs[0] == ("drivestream-lab/mandate", HEAD, ("make check", "make test", "make journeys"))
    assert states(gh) == [("aaaa", "success")]


def test_unchanged_head_is_not_graded_again_after_a_restart(cfg, gh, tmp_path):
    runner, ex = make(cfg, gh, tmp_path)
    runner.poll_once()
    again, ex2 = make(cfg, gh, tmp_path)
    again.poll_once()
    assert len(ex.runs) == 1 and ex2.runs == []
    assert len(gh.statuses) == 1  # nothing changed, nothing reposted


def test_pending_while_running_then_one_final_status(cfg, gh, tmp_path):
    pool = DeferredPool()
    runner, ex = make(cfg, gh, tmp_path, pool=pool)
    runner.poll_once()
    runner.poll_once()  # still running: not started twice, pending not reposted
    assert len(pool.jobs) == 1
    assert states(gh) == [("aaaa", "pending")]
    pool.release()
    runner.poll_once()
    assert states(gh) == [("aaaa", "pending"), ("aaaa", "success")]
    assert len(ex.runs) == 1


def test_review_rules_rechecked_each_poll_without_rerunning(cfg, gh, tmp_path):
    gh.reviews[7] = []
    runner, ex = make(cfg, gh, tmp_path)
    runner.poll_once()
    runner.poll_once()
    gh.reviews[7] = [first()]
    runner.poll_once()
    gh.reviews[7] = [first(), first("CHANGES_REQUESTED")]
    runner.poll_once()
    assert len(ex.runs) == 1
    assert [s[2] for s in gh.statuses] == ["failure", "success", "failure"]
    assert "mandate-challenger" in gh.statuses[0][3]


def test_failed_head_stays_failed_until_a_new_push(cfg, gh, tmp_path):
    runner, ex = make(cfg, gh, tmp_path, FakeExecutor(fail_on="make test"))
    runner.poll_once()
    gh.reviews[7] = [first(), review(KD)]
    runner.poll_once()
    assert len(ex.runs) == 1
    assert gh.statuses == [
        ("drivestream-lab/mandate", HEAD, "failure", "make test failed", gh.statuses[0][4]),
    ]
    assert gh.statuses[0][4].startswith("http://localhost:8090/runs/drivestream-lab__mandate-aaaaaaaaaaaa-")

    ex.fail_on = None
    gh.prs["drivestream-lab/mandate"] = [pr(sha=NEW)]
    gh.reviews[7] = [first(commit=NEW)]
    runner.poll_once()
    assert len(ex.runs) == 2 and ex.runs[1][1] == NEW
    assert gh.statuses[-1][1:3] == (NEW, "success")


def test_runner_error_is_retried_not_cached(cfg, gh, tmp_path):
    ex = FakeExecutor(error=True)
    runner, _ = make(cfg, gh, tmp_path, ex)
    runner.poll_once()
    assert states(gh) == [("aaaa", "pending")]
    ex.error = False
    runner.poll_once()
    assert len(ex.runs) == 2
    assert states(gh)[-1] == ("aaaa", "success")


def test_prs_into_other_bases_are_not_graded(cfg, gh, tmp_path):
    gh.prs["drivestream-lab/mandate"] = [pr(base="release/x")]
    runner, ex = make(cfg, gh, tmp_path)
    runner.poll_once()
    assert ex.runs == [] and gh.statuses == []


def test_trust_critical_diff_is_fetched_once_per_head(cfg, gh, tmp_path):
    gh.paths[7] = ["src/engine/ledger/hash_chain.py"]
    runner, _ = make(cfg, gh, tmp_path)
    runner.poll_once()
    gh.reviews[7] = [first(), second()]
    runner.poll_once()
    runner.poll_once()
    assert gh.calls["list_changed_paths"] == 1
    assert [s[2] for s in gh.statuses] == ["failure", "success"]


def test_pyproject_tool_change_is_compared_against_the_merge_base(cfg, gh, tmp_path):
    gh.paths[7] = ["pyproject.toml"]
    gh.files = {"e" * 40: b"[tool.ruff]\nline-length = 120\n", HEAD: b"[tool.ruff]\nline-length = 200\n"}
    runner, _ = make(cfg, gh, tmp_path)
    runner.poll_once()
    assert gh.statuses[-1][2] == "failure"
    assert "pyproject.toml [tool]" in gh.statuses[-1][3]


def test_run_log_is_kept_and_records_the_status(cfg, gh, tmp_path):
    runner, _ = make(cfg, gh, tmp_path)
    runner.poll_once()
    logs = list((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert "pinned commands (runner config): make check, make test, make journeys" in text
    assert "posted mandate/build-verified = success" in text
    assert gh.statuses[0][4].endswith("/runs/" + logs[0].name)


def test_wave_pr_graded_with_the_main_command_set(cfg, gh, tmp_path):
    gh.prs["drivestream-lab/mandate"] = [pr(base="main", head_ref="develop", labels=("wave-accepted",))]
    gh.reviews[7] = [review(KD, body=f"Initiative: INIT-MANDATE-001\nHead: {HEAD}")]
    runner, ex = make(cfg, gh, tmp_path)
    runner.poll_once()
    assert ex.runs[0][2] == cfg.bases["main"].commands
    assert gh.statuses[-1][2] == "success"
