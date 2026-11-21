"""Runs a PR head's pinned commands in the sandbox, never in the runner's own container.

The runner checks the exact commit out into a fresh workspace on the volume it
shares with the sandbox (Docker-in-Docker, no secrets), then runs each command in
a job container there. PR code never sees the runner's key or token: git fetches
happen in the runner with the token in the environment of that one git process,
never written to .git/config, and the job container gets no GitHub credentials.

Between runs the sandbox is reset: every container, network and volume is
removed, so each head gets a fresh stack. The job image is built once and run by
image id, so a PR cannot retag it for the next run.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from .config import Config
from .models import CommandsResult
from .store import RunLog

log = logging.getLogger(__name__)

TEST_SECRETS_IN_SANDBOX = "/secrets/test"


class InfraError(RuntimeError):
    """The runner could not grade the head; not the PR's fault. Retried next poll."""


class Executor(Protocol):
    def run(self, repo: str, sha: str, commands: tuple[str, ...], run_id: str, runlog: RunLog) -> CommandsResult: ...


def _git_auth_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


class DockerSandboxExecutor:
    def __init__(self, cfg: Config, token: Callable[[], str], *, test_secrets: bool = False):
        self._cfg = cfg
        self._token = token
        self._test_secrets = test_secrets
        self._job_image_id: str | None = None

    # --- process helpers ----------------------------------------------------

    def _run(
        self,
        args: list[str],
        runlog: RunLog,
        *,
        timeout: int = 600,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        show: str | None = None,
    ) -> int:
        runlog.write(f"$ {show or ' '.join(args)}")
        full_env = {**os.environ, **(env or {})}
        proc = subprocess.Popen(
            args, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            if out:
                runlog.write(out)
            raise
        if out:
            runlog.write(out)
        return proc.returncode

    def _must(
        self, args: list[str], runlog: RunLog, what: str, *, timeout: int = 600, env: Mapping[str, str] | None = None
    ) -> None:
        try:
            code = self._run(args, runlog, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise InfraError(f"{what} timed out") from None
        if code != 0:
            raise InfraError(f"{what} failed (exit {code})")

    def _docker_out(self, args: list[str]) -> str:
        res = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise InfraError(f"docker {args[0]} failed: {res.stderr.strip()[:300]}")
        return res.stdout.strip()

    # --- sandbox ------------------------------------------------------------

    def reset_sandbox(self, runlog: RunLog) -> None:
        ids = self._docker_out(["ps", "-aq"]).split()
        if ids:
            self._must(["docker", "rm", "-f", "-v", *ids], runlog, "sandbox reset")
        self._must(["docker", "network", "prune", "-f"], runlog, "sandbox network prune")
        self._must(["docker", "volume", "prune", "-af"], runlog, "sandbox volume prune")

    def job_image(self, runlog: RunLog) -> str:
        if self._job_image_id:
            res = subprocess.run(["docker", "image", "inspect", self._job_image_id], capture_output=True, timeout=60)
            if res.returncode == 0:
                return self._job_image_id
        runlog.write("building the job image in the sandbox")
        self._must(
            ["docker", "build", "-t", self._cfg.job_image_tag, str(self._cfg.job_image_context)],
            runlog,
            "job image build",
            timeout=1800,
        )
        self._job_image_id = self._docker_out(["image", "inspect", "--format", "{{.Id}}", self._cfg.job_image_tag])
        runlog.write(f"job image {self._job_image_id}")
        return self._job_image_id

    def checkout(self, repo: str, sha: str, workspace: Path, runlog: RunLog) -> None:
        workspace.mkdir(parents=True)
        env = _git_auth_env(self._token())
        url = f"https://github.com/{repo}.git"
        for args, what in (
            (["git", "init", "-q", str(workspace)], "git init"),
            (["git", "-C", str(workspace), "remote", "add", "origin", url], "git remote"),
            (["git", "-C", str(workspace), "fetch", "-q", "--depth=1", "origin", sha], "git fetch"),
            (["git", "-C", str(workspace), "checkout", "-q", "--detach", sha], "git checkout"),
            (
                ["git", "-C", str(workspace), "submodule", "update", "-q", "--init", "--recursive", "--depth=1"],
                "git submodule update",
            ),
        ):
            self._must(args, runlog, what, env=env, timeout=900)
        head = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if head != sha:
            raise InfraError(f"checked out {head}, expected {sha}")
        runlog.write(f"checked out {repo}@{sha}")

    # --- run ----------------------------------------------------------------

    def run(self, repo: str, sha: str, commands: tuple[str, ...], run_id: str, runlog: RunLog) -> CommandsResult:
        workspace = self._cfg.work_dir / run_id
        container = f"mandate-job-{run_id}"[:120]
        try:
            self.reset_sandbox(runlog)
            image = self.job_image(runlog)
            self.checkout(repo, sha, workspace, runlog)
            create = [
                "docker", "create", "--name", container,
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{workspace}:{workspace}",
                "-w", str(workspace),
                "-e", "CI=1", "-e", f"MANDATE_RUNNER_HEAD_SHA={sha}", "-e", f"MANDATE_RUNNER_REPO={repo}",
            ]  # fmt: skip
            if self._test_secrets:
                create += ["-v", f"{TEST_SECRETS_IN_SANDBOX}:{TEST_SECRETS_IN_SANDBOX}:ro"]
            self._must([*create, image, "sleep", "infinity"], runlog, "job container create")
            self._must(["docker", "start", container], runlog, "job container start")
            for command in commands:
                runlog.write(f"=== {command}")
                try:
                    code = self._run(
                        ["docker", "exec", container, "bash", "-lc", command],
                        runlog,
                        timeout=self._cfg.command_timeout_seconds,
                        show=command,
                    )
                except subprocess.TimeoutExpired:
                    runlog.write(f"=== {command}: timed out after {self._cfg.command_timeout_seconds}s")
                    return CommandsResult(passed=False, failed_command=command, run_id=run_id, timed_out=True)
                runlog.write(f"=== {command}: exit {code}")
                if code != 0:
                    return CommandsResult(passed=False, failed_command=command, run_id=run_id)
            return CommandsResult(passed=True, failed_command=None, run_id=run_id)
        finally:
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, timeout=120)
            shutil.rmtree(workspace, ignore_errors=True)
            try:
                self.reset_sandbox(runlog)
            except InfraError as exc:
                log.warning("sandbox reset after %s failed: %s", run_id, exc)
