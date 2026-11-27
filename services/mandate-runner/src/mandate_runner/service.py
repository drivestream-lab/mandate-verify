"""The polling loop: grade each open PR head once, re-check its review rules every poll.

- Commands run once per (repo, head sha, command set). The result is kept on disk,
  so an unchanged head is never graded twice, across restarts too.
- A head whose commands failed stays failed until a new push moves the head.
- Review rules are re-evaluated on every poll; a status is posted only when the
  state, description or link changes.
- Infrastructure errors are not cached: the head is retried on a later poll.

B0-02, after every merge into develop (develop_run in the config):
- The merged head is run once: deployed to the lab stack, then `make verify` against it.
  A head with no Dockerfile or no verify target records "nothing to deploy yet".
- A red run opens a bug issue on the repo as the runner App, linked to the merge
  commit and the run's log. While one is open, every PR into the task base fails
  with "develop is red" (see freeze.py). A green run closes it, lifting the freeze.

The sandbox is rebuilt from scratch once a day and after any run that did not pass.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor as Pool
from concurrent.futures import Future, ThreadPoolExecutor

from .config import BaseRules, Config, DevelopRun, RepoRules
from .executor import Executor
from .freeze import closes, freeze_issues, freeze_verdict
from .github import GitHub
from .models import CommandsResult, Diff, Issue, PullRequest
from .paths import tool_settings_changed
from .rules import evaluate, fit_description
from .store import Store, commands_digest, new_run_id

log = logging.getLogger(__name__)

PYPROJECT = "pyproject.toml"
REBUILD = ("sandbox", "rebuild", "")


def develop_digest(dr: DevelopRun) -> str:
    """Keys a develop run's result: a different command set or deploy switch runs the head again."""
    return "develop-" + commands_digest((*dr.commands, f"deploy={dr.deploy.enabled}"))


class Runner:
    def __init__(
        self,
        cfg: Config,
        github: GitHub,
        executor: Executor,
        store: Store,
        *,
        pool: Pool | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._cfg = cfg
        self._gh = github
        self._executor = executor
        self._store = store
        self._pool = pool or ThreadPoolExecutor(max_workers=1, thread_name_prefix="grade")
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, str, str], Future[None]] = {}
        self._diffs: dict[tuple[str, int, str, str], Diff] = {}
        self._issues: list[Issue] | None = None  # open freeze issues, as last seen

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def log_url(self, run_id: str) -> str:
        return f"{self._cfg.log_base_url}/runs/{run_id}.log"

    # --- one poll -----------------------------------------------------------

    def poll_once(self) -> None:
        self._maybe_rebuild()
        dr = self._cfg.develop_run
        if dr is not None:
            try:
                self._develop(dr)
            except Exception:
                log.exception("develop run on %s failed", dr.repo)
            try:
                self._issues = freeze_issues(self._gh.list_open_issues(dr.repo, dr.freeze_label), self._cfg)
            except Exception as exc:
                log.warning("listing freeze issues on %s failed: %s", dr.repo, exc)
                if self._issues is None:
                    return  # never grade without knowing whether develop is red
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

        issues = self._issues or []
        if issues:
            reviews = self._gh.list_reviews(pr.repo, pr.number) if all(closes(pr, i) for i in issues) else []
            frozen = freeze_verdict(pr, reviews, issues, self._cfg)
            if frozen is not None:
                self._post(pr, "failure", frozen.description, issues[0].url, None)
                return

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

    def _submit(self, key: tuple[str, str, str], fn: Callable[..., None], *args: object) -> None:
        with self._lock:
            if key in self._inflight:
                return
            future = self._pool.submit(fn, *args)
            if not future.done():
                self._inflight[key] = future
        if not future.done():
            future.add_done_callback(lambda _f: self._done(key))

    def _start(self, pr: PullRequest, base: BaseRules, digest: str) -> None:
        run_id = new_run_id(pr.repo, pr.head_sha)
        self._submit((pr.repo, pr.head_sha, digest), self._execute, pr, base, digest, run_id)

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
            self._rebuild_after_failure(run_id)
            return
        self._store.save_result(pr.repo, pr.head_sha, digest, result)
        runlog.write("commands passed" if result.passed else f"failed: {result.failed_command}")
        if not result.passed:
            self._rebuild_after_failure(run_id)

    # --- after a merge into develop -------------------------------------------

    def _develop(self, dr: DevelopRun) -> None:
        sha = self._gh.branch_head(dr.repo, dr.branch)
        if sha is None:
            return
        digest = develop_digest(dr)
        result = self._store.load_result(dr.repo, sha, digest)
        if result is None:
            run_id = new_run_id(dr.repo, sha)
            self._submit((dr.repo, sha, digest), self._execute_develop, dr, sha, digest, run_id)
            result = self._store.load_result(dr.repo, sha, digest)
        if result is not None and not self._store.develop_reported(dr.repo, sha):
            self._report_develop(dr, sha, result)

    def _execute_develop(self, dr: DevelopRun, sha: str, digest: str, run_id: str) -> None:
        runlog = self._store.open_log(run_id)
        runlog.write(f"mandate-runner after a merge into {dr.repo} {dr.branch}: head {sha}")
        runlog.write("deploy to the lab stack, then against it (runner config): " + ", ".join(dr.commands))
        try:
            result = self._executor.run_develop(dr.repo, sha, dr.commands, run_id, runlog, dr.deploy)
        except Exception as exc:
            runlog.write(f"runner error, will retry on a later poll: {exc}")
            log.warning("develop run %s errored, will retry: %s", run_id, exc)
            self._rebuild_after_failure(run_id)
            return
        self._store.save_result(dr.repo, sha, digest, result)
        if result.passed:
            runlog.write(f"develop is green: {result.note or 'deployed and make verify passed'}")
        else:
            runlog.write(f"develop is red: {result.failed_command} failed")
            self._rebuild_after_failure(run_id)

    def _report_develop(self, dr: DevelopRun, sha: str, result: CommandsResult) -> None:
        """Open, extend or close the freeze issue for this develop head. Done once per head."""
        issues = freeze_issues(self._gh.list_open_issues(dr.repo, dr.freeze_label), self._cfg)
        url = self.log_url(result.run_id)
        commit = f"https://github.com/{dr.repo}/commit/{sha}"
        if result.passed:
            for issue in issues:
                self._gh.close_issue(
                    dr.repo,
                    issue.number,
                    f"{dr.branch} is green again at [{sha[:12]}]({commit}) "
                    f"({result.note or 'deployed; make verify passed'}). [Run log]({url}). "
                    "Closing this lifts the merge freeze.",
                )
                log.info("freeze lifted: closed %s#%d (green at %s)", dr.repo, issue.number, sha[:12])
            self._store.save_develop_reported(dr.repo, sha, "closed" if issues else "green")
            return
        why = "timed out" if result.timed_out else "failed"
        what = f"{result.failed_command} {why}"
        if issues:
            self._gh.comment_issue(
                dr.repo,
                issues[0].number,
                f"Still red at [{sha[:12]}]({commit}): {what}. [Run log]({url}).",
            )
            self._store.save_develop_reported(dr.repo, sha, "commented")
            return
        second = f"a separate {self._cfg.identities.challenger_app_slug} Approve ({self._cfg.markers.second})"
        issue = self._gh.open_issue(
            dr.repo,
            f"develop is red at {sha[:12]}: {what}",
            "\n".join(
                [
                    f"After the merge of [{sha[:12]}]({commit}) into `{dr.branch}`, the runner's run is red: "
                    f"**{what}**.",
                    "",
                    f"- Merge commit: {commit}",
                    f"- Run log: {url}",
                    "",
                    f"**Merge freeze.** While this issue is open, every PR into `{dr.task_base}` fails "
                    '`mandate/build-verified` with "develop is red".',
                    "",
                    f"**To fix.** Open a PR whose body says `Fixes #<this issue>` (from another repo: "
                    f"`Fixes {dr.repo}#<this issue>`). It is graded as usual once {second} is on its head. "
                    f"After it merges and the next run on `{dr.branch}` is green, the runner closes this issue "
                    "and the freeze is lifted.",
                ]
            ),
            dr.freeze_label,
        )
        self._store.save_develop_reported(dr.repo, sha, "opened")
        log.warning("develop is red at %s: opened %s (freeze)", sha[:12], issue.url)

    # --- a fresh sandbox ----------------------------------------------------------

    def _maybe_rebuild(self) -> None:
        now = self._clock()
        last = self._store.last_rebuild()
        if last is None or last > now:  # first start, or the clock went back
            self._store.save_rebuild(now)
            return
        if now - last >= self._cfg.sandbox.rebuild_interval_hours * 3600:
            self._submit(REBUILD, self._rebuild, "daily")

    def _rebuild_after_failure(self, run_id: str) -> None:
        if self._cfg.sandbox.rebuild_after_failure:
            self._rebuild(f"after {run_id}")

    def _rebuild(self, why: str) -> None:
        run_id = new_run_id("sandbox/rebuild", "0" * 12)
        runlog = self._store.open_log(run_id)
        runlog.write(f"sandbox rebuild ({why})")
        try:
            self._executor.rebuild_sandbox(runlog)
        except Exception as exc:
            runlog.write(f"sandbox rebuild failed: {exc}")
            log.warning("sandbox rebuild (%s) failed: %s", why, exc)
            return
        self._store.save_rebuild(self._clock())
        log.info("sandbox rebuilt (%s): %s", why, self.log_url(run_id))

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
