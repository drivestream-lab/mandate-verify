"""The deployer: the only container with the host's Docker engine (B0-02).

It holds no GitHub key and never runs PR code. The runner asks it for one of two
named jobs by dropping a JSON file into the jobs volume, and waits for the result:

- reset-sandbox: rebuild the sandbox from scratch with the existing
  `make runner-reset-sandbox` (drops the sandbox's images and state).
- deploy: load the merged head's image (built and saved by the runner inside the
  sandbox) and run it on the lab stack as the Mandate service, then wait for health.

Nothing in a request names a command, a container or a path: those come from the
runner's config. A request only says which job, and for deploy, which head sha.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import Config, DeployRules

log = logging.getLogger(__name__)

RESET = "reset-sandbox"
DEPLOY = "deploy"
JOBS = (RESET, DEPLOY)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOADED = re.compile(r"^Loaded image(?: ID)?: (\S+)$", re.MULTILINE)


class DeployerError(RuntimeError):
    """The deployer did not answer, or could not do the job for a reason that is not the head's fault."""


@dataclass(frozen=True)
class JobResult:
    ok: bool
    message: str
    log: str = ""


def deploy_tar(jobs_dir: Path, sha: str) -> Path:
    """Where the runner saves the head's image for the deployer to load."""
    if not _SHA.match(sha):
        raise ValueError("bad sha")
    return jobs_dir / "deploy" / f"{sha}.tar"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


# --- runner side -------------------------------------------------------------


class DeployerClient:
    def __init__(self, jobs_dir: Path, *, poll: float = 1.0, sleep: Callable[[float], None] = time.sleep):
        self._dir = jobs_dir
        self._poll = poll
        self._sleep = sleep

    def request(self, job: str, *, sha: str | None = None, timeout: float = 900.0) -> JobResult:
        if job not in JOBS:
            raise ValueError(f"unknown job {job}")
        name = f"{time.time_ns()}-{job}-{secrets.token_hex(4)}.json"
        _write_json(self._dir / "requests" / name, {"job": job, "sha": sha})
        result_path = self._dir / "results" / name
        waited = 0.0
        while waited < timeout:
            if result_path.exists():
                data = json.loads(result_path.read_text())
                result_path.unlink(missing_ok=True)
                return JobResult(ok=bool(data["ok"]), message=str(data["message"]), log=str(data.get("log", "")))
            self._sleep(self._poll)
            waited += self._poll
        (self._dir / "requests" / name).unlink(missing_ok=True)
        raise DeployerError(f"the deployer did not finish {job} within {int(timeout)}s")

    def reset_sandbox(self) -> JobResult:
        return self.request(RESET, timeout=900.0)

    def deploy(self, sha: str) -> JobResult:
        return self.request(DEPLOY, sha=sha, timeout=900.0)


# --- deployer side -------------------------------------------------------------

Run = Callable[[Sequence[str], int], tuple[int, str]]


def _run(args: Sequence[str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(list(args), capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr)[-8000:]


def env_database(env_file: Path) -> str | None:
    """POSTGRES_DB from a docker --env-file (KEY=value lines). Values are never logged."""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("POSTGRES_DB="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


class Deployer:
    def __init__(
        self,
        cfg: Config,
        jobs_dir: Path,
        *,
        reset_command: Sequence[str],
        run: Run = _run,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._cfg = cfg
        self._dir = jobs_dir
        self._reset_command = tuple(reset_command)
        self._run = run
        self._sleep = sleep

    def handle(self, request: dict) -> JobResult:
        job = request.get("job")
        if job == RESET:
            return self.reset_sandbox()
        if job == DEPLOY:
            sha = request.get("sha")
            if not isinstance(sha, str) or not _SHA.match(sha):
                return JobResult(False, "deploy needs a 40-character head sha")
            if self._cfg.develop_run is None or not self._cfg.develop_run.deploy.enabled:
                return JobResult(False, "deploy is not enabled in the runner config")
            return self.deploy(self._cfg.develop_run.deploy, sha)
        return JobResult(False, f"unknown job {job!r}")

    def reset_sandbox(self) -> JobResult:
        code, out = self._run(self._reset_command, 600)
        if code != 0:
            return JobResult(False, f"sandbox reset failed (exit {code})", out)
        return JobResult(True, "sandbox rebuilt", out)

    def deploy(self, rules: DeployRules, sha: str) -> JobResult:
        tar = deploy_tar(self._dir, sha)
        if not tar.is_file():
            return JobResult(False, f"no image saved for {sha[:12]}")
        env_file = Path(rules.env_file)
        if not env_file.is_file():
            return JobResult(False, "the lab env file for the Mandate service is missing (~/.mandate/lab/mandate.env)")
        if env_database(env_file) != rules.database:
            return JobResult(False, f"the lab env file must set POSTGRES_DB={rules.database}")
        try:
            code, out = self._run(["docker", "load", "-i", str(tar)], 600)
        finally:
            tar.unlink(missing_ok=True)
        loaded = _LOADED.findall(out)
        if code != 0 or not loaded:
            return JobResult(False, f"docker load failed (exit {code})", out)
        image = f"{rules.image_repo}:{sha[:12]}"
        steps: list[tuple[list[str], str]] = [
            (["docker", "tag", loaded[-1], image], "docker tag"),
            (["docker", "rm", "-f", rules.container], "remove the previous service"),
            (
                [
                    "docker", "run", "-d", "--name", rules.container, "--network", rules.network,
                    "--env-file", str(env_file), "--restart", "unless-stopped",
                    "--label", f"mandate.head={sha}", image,
                ],
                "docker run",
            ),
        ]  # fmt: skip
        for args, what in steps:
            code, out = self._run(args, 300)
            if code != 0:
                return JobResult(False, f"{what} failed (exit {code})", out)
        return self._wait_healthy(rules, sha)

    def _wait_healthy(self, rules: DeployRules, sha: str, attempts: int = 24) -> JobResult:
        out = ""
        for _ in range(attempts):
            self._sleep(5)
            code, out = self._run(["docker", "exec", rules.container, "curl", "-fsS", rules.health_url], 30)
            if code == 0:
                return JobResult(True, f"{rules.container} runs {sha[:12]} and answers {rules.health_url}", out)
        _, logs = self._run(["docker", "logs", "--tail", "80", rules.container], 30)
        return JobResult(False, f"{rules.container} did not answer {rules.health_url}", out + "\n" + logs)

    # --- loop ----------------------------------------------------------------

    def process_pending(self) -> int:
        requests = sorted((self._dir / "requests").glob("*.json"))
        for path in requests:
            try:
                request = json.loads(path.read_text())
            except (OSError, ValueError):
                request = {}
            log.info("deployer job %s", request.get("job"))
            try:
                result = self.handle(request if isinstance(request, dict) else {})
            except Exception as exc:  # report, never die
                result = JobResult(False, f"deployer error: {exc}")
            _write_json(
                self._dir / "results" / path.name, {"ok": result.ok, "message": result.message, "log": result.log}
            )
            path.unlink(missing_ok=True)
            log.info("deployer job %s: %s", request.get("job"), result.message)
        return len(requests)


def main(cfg: Config) -> int:
    jobs_dir = Path(os.environ.get("MANDATE_RUNNER_JOBS_DIR", "/jobs"))
    project = os.environ["MANDATE_VERIFY_DIR"]  # the mandate-verify checkout, at its host path
    deployer = Deployer(cfg, jobs_dir, reset_command=["make", "-C", project, "runner-reset-sandbox"])
    (jobs_dir / "requests").mkdir(parents=True, exist_ok=True)
    (jobs_dir / "results").mkdir(parents=True, exist_ok=True)
    log.info("mandate-runner-deployer up: jobs %s", ", ".join(JOBS))
    while True:
        deployer.process_pending()
        Path("/tmp/deployer-heartbeat").write_text(str(int(time.time())))
        time.sleep(1)
