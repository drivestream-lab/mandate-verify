"""Runs a PR head's pinned commands in the sandbox, never in the runner's own container.

The runner checks the exact commit out into a fresh workspace on the volume it
shares with the sandbox (Docker-in-Docker, no secrets), then runs each command in
a job container there. PR code never sees the runner's key or token: git fetches
happen in the runner with the token in the environment of that one git process,
never written to .git/config, and the job container gets no GitHub credentials.

Between runs the sandbox is reset: every container, network and volume is
removed, so each head gets a fresh stack. The job image is built once and run by
image id, so a PR cannot retag it for the next run. A full rebuild of the sandbox
(its engine's images and state too) is done by the deployer on request.

The sandbox has no route out except the egress proxy (public addresses only).
The runner's docker client config hands the proxy to every build and container.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from .config import Config, DeployRules
from .deployer import DeployerClient, DeployerError, deploy_tar
from .models import CommandsResult
from .store import RunLog

log = logging.getLogger(__name__)

TEST_SECRETS_IN_SANDBOX = "/secrets/test"
NOTHING_TO_DEPLOY = "nothing to deploy yet"


class InfraError(RuntimeError):
    """The runner could not grade the head; not the PR's fault. Retried next poll."""


class Executor(Protocol):
    def run(self, repo: str, sha: str, commands: tuple[str, ...], run_id: str, runlog: RunLog) -> CommandsResult: ...
    def run_develop(
        self, repo: str, sha: str, commands: tuple[str, ...], run_id: str, runlog: RunLog, deploy: DeployRules
    ) -> CommandsResult: ...
    def rebuild_sandbox(self, runlog: RunLog) -> None: ...


def has_make_target(makefile: Path, target: str) -> bool:
    """A rule for the target in the Makefile itself (`journeys:` or `journeys::`, not `journeys := x`)."""
    try:
        text = makefile.read_text(errors="replace")
    except OSError:
        return False
    return re.search(rf"^{re.escape(target)}\s*::?(?!:?=)", text, re.MULTILINE) is not None


def _git_auth_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


class DockerSandboxExecutor:
    def __init__(
        self,
        cfg: Config,
        token: Callable[[], str],
        *,
        test_secrets: bool = False,
        deployer: DeployerClient | None = None,
        jobs_dir: Path = Path("/jobs"),
    ):
        self._cfg = cfg
        self._token = token
        self._test_secrets = test_secrets
        self._deployer = deployer
        self._jobs_dir = jobs_dir
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

    def rebuild_sandbox(self, runlog: RunLog) -> None:
        """Recreate the sandbox from scratch (make runner-reset-sandbox, run by the deployer)."""
        if self._deployer is None:
            raise InfraError("no deployer: cannot rebuild the sandbox")
        runlog.write("rebuilding the sandbox from scratch (make runner-reset-sandbox, by the deployer)")
        try:
            result = self._deployer.reset_sandbox()
        except DeployerError as exc:
            raise InfraError(str(exc)) from None
        if result.log:
            runlog.write(result.log)
        if not result.ok:
            raise InfraError(result.message)
        self._job_image_id = None  # the sandbox's images are gone
        runlog.write(result.message)

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

    @staticmethod
    def _container_name(run_id: str) -> str:
        return f"mandate-job-{run_id}"[:120]

    def _job_container(self, repo: str, sha: str, workspace: Path, run_id: str, runlog: RunLog) -> str:
        image = self.job_image(runlog)
        container = self._container_name(run_id)
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
        return container

    def _commands(self, container: str, commands: tuple[str, ...], run_id: str, runlog: RunLog) -> CommandsResult:
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

    def _cleanup(self, run_id: str, workspace: Path, runlog: RunLog) -> None:
        subprocess.run(["docker", "rm", "-f", "-v", self._container_name(run_id)], capture_output=True, timeout=120)
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            self.reset_sandbox(runlog)
        except InfraError as exc:
            log.warning("sandbox reset after %s failed: %s", run_id, exc)

    def run(self, repo: str, sha: str, commands: tuple[str, ...], run_id: str, runlog: RunLog) -> CommandsResult:
        workspace = self._cfg.work_dir / run_id
        try:
            self.reset_sandbox(runlog)
            self.checkout(repo, sha, workspace, runlog)
            container = self._job_container(repo, sha, workspace, run_id, runlog)
            return self._commands(container, commands, run_id, runlog)
        finally:
            self._cleanup(run_id, workspace, runlog)

    # --- after a merge into develop -------------------------------------------

    def run_develop(
        self, repo: str, sha: str, commands: tuple[str, ...], run_id: str, runlog: RunLog, deploy: DeployRules
    ) -> CommandsResult:
        """Deploy the merged head to the lab stack, then run every journey built so far."""
        workspace = self._cfg.work_dir / run_id
        try:
            self.reset_sandbox(runlog)
            self.checkout(repo, sha, workspace, runlog)
            missing = [
                what
                for what, present in (
                    ("Dockerfile", (workspace / "Dockerfile").is_file()),
                    ("journeys target in the Makefile", has_make_target(workspace / "Makefile", "journeys")),
                )
                if not present
            ]
            if missing:
                runlog.write(f"{NOTHING_TO_DEPLOY}: the head has no " + " and no ".join(missing))
                return CommandsResult(passed=True, failed_command=None, run_id=run_id, note=NOTHING_TO_DEPLOY)
            if deploy.enabled:
                failed = self._deploy(sha, workspace, runlog)
                if failed is not None:
                    return CommandsResult(passed=False, failed_command=failed, run_id=run_id)
            else:
                runlog.write("deploy is disabled in the runner config; running the journeys only")
            container = self._job_container(repo, sha, workspace, run_id, runlog)
            return self._commands(container, commands, run_id, runlog)
        finally:
            self._cleanup(run_id, workspace, runlog)

    def _deploy(self, sha: str, workspace: Path, runlog: RunLog) -> str | None:
        """Builds the head's image in a freshly rebuilt sandbox and hands it to the deployer.

        Returns the failing step, or None. PR code never touches the host engine: the
        image is built in the sandbox, saved to a file, and loaded by the deployer.
        """
        if self._deployer is None:
            raise InfraError("no deployer: cannot deploy")
        # A clean engine for the image that reaches the lab: nothing an earlier PR run
        # left in the sandbox can end up in it.
        self.rebuild_sandbox(runlog)
        tag = f"mandate-deploy:{sha}"
        runlog.write(f"=== deploy: build {tag} in the sandbox")
        try:
            code = self._run(["docker", "build", "-t", tag, str(workspace)], runlog, timeout=1800)
        except subprocess.TimeoutExpired:
            return "deploy (docker build timed out)"
        if code != 0:
            return "deploy (docker build)"
        tar = deploy_tar(self._jobs_dir, sha)
        tar.parent.mkdir(parents=True, exist_ok=True)
        self._must(["docker", "save", "-o", str(tar), tag], runlog, "docker save", timeout=900)
        runlog.write("=== deploy: the deployer loads it and runs it on the lab stack")
        try:
            result = self._deployer.deploy(sha)
        except DeployerError as exc:
            tar.unlink(missing_ok=True)
            raise InfraError(str(exc)) from None
        if result.log:
            runlog.write(result.log)
        runlog.write(f"=== deploy: {result.message}")
        return None if result.ok else "deploy"
