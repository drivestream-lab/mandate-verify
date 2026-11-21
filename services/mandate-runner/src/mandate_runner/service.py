"""The polling loop: grade each open PR head once, re-check its review rules every poll.

- Commands run once per (repo, head sha, command set). The result is kept on disk,
  so an unchanged head is never graded twice, across restarts too.
- A head whose commands failed stays failed until a new push moves the head.
- Review rules are re-evaluated on every poll; a status is posted only when the
  state, description or link changes.
- Infrastructure errors are not cached: the head is retried on a later poll.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Executor as Pool
from concurrent.futures import Future, ThreadPoolExecutor

from .config import BaseRules, Config, RepoRules
from .executor import Executor
from .github import GitHub
from .models import Diff, PullRequest
from .paths import tool_settings_changed
from .rules import evaluate, fit_description
from .store import Store, commands_digest, new_run_id

log = logging.getLogger(__name__)

PYPROJECT = "pyproject.toml"


class Runner:
    def __init__(self, cfg: Config, github: GitHub, executor: Executor, store: Store, *, pool: Pool | None = None):
        self._cfg = cfg
        self._gh = github
        self._executor = executor
        self._store = store
        self._pool = pool or ThreadPoolExecutor(max_workers=1, thread_name_prefix="grade")
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, str, str], Future[None]] = {}
        self._diffs: dict[tuple[str, int, str, str], Diff] = {}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def log_url(self, run_id: str) -> str:
        return f"{self._cfg.log_base_url}/runs/{run_id}.log"

    # --- one poll -----------------------------------------------------------

    def poll_once(self) -> None:
        for repo in self._cfg.repos:
            try:
                prs = self._gh.list_open_prs(repo.name)
            except Exception as exc:
                log.warning("listing open PRs on %s failed: %s", repo.name, exc)
                continue
            for pr in prs:
                try:
                    self.grade(repo, pr)
                except Exception:
                    log.exception("grading %s#%d at %s failed", pr.repo, pr.number, pr.head_sha[:12])

    def grade(self, repo: RepoRules, pr: PullRequest) -> None:
        base = self._cfg.bases.get(pr.base_ref)
        if base is None:
            return  # only PRs into a configured base are graded
        digest = commands_digest(base.commands)
        result = self._store.load_result(pr.repo, pr.head_sha, digest)
        if result is None:
            self._start(pr, base, digest)
            result = self._store.load_result(pr.repo, pr.head_sha, digest)
        if result is None:
            self._post(pr, "pending", "Running " + ", ".join(base.commands), None, None)
            return

        url = self.log_url(result.run_id)
        if not result.passed:
            why = "timed out" if result.timed_out else "failed"
            self._post(pr, "failure", f"{result.failed_command} {why}", url, result.run_id)
            return

        verdict = evaluate(pr, self._gh.list_reviews(pr.repo, pr.number), self._diff(repo, pr), self._cfg)
        self._post(pr, "success" if verdict.ok else "failure", verdict.description, url, result.run_id)

    # --- commands -------------------------------------------------------------

    def _start(self, pr: PullRequest, base: BaseRules, digest: str) -> None:
        key = (pr.repo, pr.head_sha, digest)
        with self._lock:
            if key in self._inflight:
                return
            run_id = new_run_id(pr.repo, pr.head_sha)
            future = self._pool.submit(self._execute, pr, base, digest, run_id)
            if not future.done():
                self._inflight[key] = future
        if not future.done():
            future.add_done_callback(lambda _f: self._done(key))

    def _done(self, key: tuple[str, str, str]) -> None:
        with self._lock:
            self._inflight.pop(key, None)

    def _execute(self, pr: PullRequest, base: BaseRules, digest: str, run_id: str) -> None:
        runlog = self._store.open_log(run_id)
        runlog.write(f"mandate-runner grading {pr.repo}#{pr.number} head {pr.head_sha} into {pr.base_ref}")
        runlog.write("pinned commands (runner config): " + ", ".join(base.commands))
        try:
            result = self._executor.run(pr.repo, pr.head_sha, base.commands, run_id, runlog)
        except Exception as exc:
            runlog.write(f"runner error, will retry on a later poll: {exc}")
            log.warning("run %s errored, will retry: %s", run_id, exc)
            return
        self._store.save_result(pr.repo, pr.head_sha, digest, result)
        runlog.write("commands passed" if result.passed else f"failed: {result.failed_command}")

    # --- diff facts -----------------------------------------------------------

    def _diff(self, repo: RepoRules, pr: PullRequest) -> Diff:
        key = (pr.repo, pr.number, pr.head_sha, pr.base_sha)
        cached = self._diffs.get(key)
        if cached is not None:
            return cached
        paths = tuple(self._gh.list_changed_paths(pr.repo, pr.number))
        tool_changed = False
        if repo.pyproject_tool_settings_protected and PYPROJECT in paths:
            merge_base = self._gh.merge_base(pr.repo, pr.base_sha, pr.head_sha)
            tool_changed = tool_settings_changed(
                self._gh.file_at(pr.repo, PYPROJECT, merge_base), self._gh.file_at(pr.repo, PYPROJECT, pr.head_sha)
            )
        diff = Diff(paths=paths, pyproject_tool_changed=tool_changed)
        self._diffs[key] = diff
        return diff

    # --- status -----------------------------------------------------------------

    def _post(self, pr: PullRequest, state: str, description: str, url: str | None, run_id: str | None) -> None:
        description = fit_description(description)
        if self._store.load_posted(pr.repo, pr.head_sha) == (state, description, url):
            return
        self._gh.post_status(pr.repo, pr.head_sha, state, description, url)
        self._store.save_posted(pr.repo, pr.head_sha, state, description, url)
        log.info("%s#%d %s: %s %s", pr.repo, pr.number, pr.head_sha[:12], state, description)
        if run_id is not None:
            self._store.open_log(run_id).write(f"posted {self._cfg.status_context} = {state}: {description}")
