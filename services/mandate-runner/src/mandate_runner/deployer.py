"""The deployer: the only container with the host's Docker engine (B0-02).

It holds no GitHub key and never runs PR code. The runner asks it for one of four
named jobs by dropping a JSON file into the jobs volume, and waits for the result:

- reset-sandbox: rebuild the sandbox from scratch with the existing
  `make runner-reset-sandbox` (drops the sandbox's images and state).
- deploy: load the merged head's image (built and saved by the runner inside the
  sandbox) and run it on the lab stack as the Mandate service with the lab .env
  (its entrypoint runs the migrations), then wait for health.
- verify: from the merged head's checkout, run the develop run's commands
  (`make setup`, `make verify`) in a job container on the lab network, with the same
  .env and the service's URL, so every journey runs against the deployed service.
  The container gets no Docker engine and no secrets beyond that .env.
- purge: delete every row in the lab database and keep the schema (and the
  migration history), for when waves are not compatible. Only on request.

Nothing in a request names a command, a container or a path: those come from the
runner's config. A request only says which job, which head sha and which run.
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
VERIFY = "verify"
PURGE = "purge"
JOBS = (RESET, DEPLOY, VERIFY, PURGE)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
HOST_JOB_IMAGE = "drivestream-lab/mandate-runner-job:host"
PURGE_IMAGE = "postgres:16-alpine"
# Every table except the migration history: the schema stays, the data goes.
PURGE_SQL = (
    "DO $$ DECLARE r record; BEGIN "
    "FOR r IN SELECT schemaname, tablename FROM pg_tables "
    "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND tablename <> 'alembic_version' LOOP "
    "EXECUTE format('TRUNCATE TABLE %I.%I RESTART IDENTITY CASCADE', r.schemaname, r.tablename); "
    "END LOOP; END $$;"
)
_LOADED = re.compile(r"^Loaded image(?: ID)?: (\S+)$", re.MULTILINE)


class DeployerError(RuntimeError):
    """The deployer did not answer, or could not do the job for a reason that is not the head's fault."""


@dataclass(frozen=True)
class JobResult:
    ok: bool
    message: str
    log: str = ""
    failed_command: str | None = None


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

    def request(
        self, job: str, *, sha: str | None = None, run_id: str | None = None, timeout: float = 900.0
    ) -> JobResult:
        if job not in JOBS:
            raise ValueError(f"unknown job {job}")
        name = f"{time.time_ns()}-{job}-{secrets.token_hex(4)}.json"
        _write_json(self._dir / "requests" / name, {"job": job, "sha": sha, "run_id": run_id})
        result_path = self._dir / "results" / name
        waited = 0.0
        while waited < timeout:
            if result_path.exists():
                data = json.loads(result_path.read_text())
                result_path.unlink(missing_ok=True)
                return JobResult(
                    ok=bool(data["ok"]),
                    message=str(data["message"]),
                    log=str(data.get("log", "")),
                    failed_command=data.get("failed_command"),
                )
            self._sleep(self._poll)
            waited += self._poll
        (self._dir / "requests" / name).unlink(missing_ok=True)
        raise DeployerError(f"the deployer did not finish {job} within {int(timeout)}s")

    def reset_sandbox(self) -> JobResult:
        return self.request(RESET, timeout=900.0)

    def deploy(self, sha: str) -> JobResult:
        return self.request(DEPLOY, sha=sha, timeout=900.0)

    def verify(self, sha: str, run_id: str, timeout: float) -> JobResult:
        return self.request(VERIFY, sha=sha, run_id=run_id, timeout=timeout)

    def purge(self) -> JobResult:
        return self.request(PURGE, timeout=300.0)


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
        work_volume: str = "mandate_runner_work",
        job_image_context: str = "/app/job-image",
        run: Run = _run,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._cfg = cfg
        self._dir = jobs_dir
        self._reset_command = tuple(reset_command)
        self._work_volume = work_volume
        self._job_image_context = job_image_context
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
        if job in (VERIFY, PURGE):
            if self._cfg.develop_run is None or not self._cfg.develop_run.deploy.enabled:
                return JobResult(False, "deploy is not enabled in the runner config")
            rules = self._cfg.develop_run.deploy
            if job == PURGE:
                return self.purge(rules)
            sha, run_id = request.get("sha"), request.get("run_id")
            if not isinstance(sha, str) or not _SHA.match(sha):
                return JobResult(False, "verify needs a 40-character head sha")
            if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
                return JobResult(False, "verify needs a run id")
            return self.verify(rules, sha, run_id)
        return JobResult(False, f"unknown job {job!r}")

    def _env_file(self, rules: DeployRules) -> tuple[Path | None, JobResult | None]:
        env_file = Path(rules.env_file)
        if not env_file.is_file():
            return None, JobResult(False, f"the lab env file {env_file.name} is missing (~/.mandate/lab/)")
        if env_database(env_file) != rules.database:
            return None, JobResult(False, f"the lab env file must set POSTGRES_DB={rules.database}")
        return env_file, None

    def reset_sandbox(self) -> JobResult:
        code, out = self._run(self._reset_command, 600)
        if code != 0:
            return JobResult(False, f"sandbox reset failed (exit {code})", out)
        return JobResult(True, "sandbox rebuilt", out)

    def deploy(self, rules: DeployRules, sha: str) -> JobResult:
        tar = deploy_tar(self._dir, sha)
        if not tar.is_file():
            return JobResult(False, f"no image saved for {sha[:12]}")
        env_file, refused = self._env_file(rules)
        if refused is not None or env_file is None:
            tar.unlink(missing_ok=True)
            return refused or JobResult(False, "no env file")
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

    def verify(self, rules: DeployRules, sha: str, run_id: str) -> JobResult:
        """The develop run's commands from the merged head, on the lab network, against the service."""
        env_file, refused = self._env_file(rules)
        if refused is not None or env_file is None:
            return refused or JobResult(False, "no env file")
        assert self._cfg.develop_run is not None
        commands = self._cfg.develop_run.commands
        code, out = self._run(["docker", "image", "inspect", HOST_JOB_IMAGE], 60)
        if code != 0:
            code, out = self._run(["docker", "build", "-t", HOST_JOB_IMAGE, self._job_image_context], 1800)
            if code != 0:
                return JobResult(False, f"job image build failed (exit {code})", out)
        container = f"mandate-verify-{run_id}"[:120]
        workspace = f"/work/{run_id}"
        log_parts: list[str] = []
        create = [
            "docker", "create", "--name", container, "--network", rules.network,
            "--env-file", str(env_file),
            "-e", f"MANDATE_SERVICE_URL={rules.service_url}", "-e", f"MANDATE_RUNNER_HEAD_SHA={sha}", "-e", "CI=1",
            "-v", f"{self._work_volume}:/work", "-w", workspace,
            HOST_JOB_IMAGE, "sleep", "infinity",
        ]  # fmt: skip
        try:
            for args, what in ((create, "verify container create"), (["docker", "start", container], "start")):
                code, out = self._run(args, 120)
                if code != 0:
                    return JobResult(False, f"{what} failed (exit {code})", out)
            for command in commands:
                code, out = self._run(
                    ["docker", "exec", container, "bash", "-lc", command], self._cfg.command_timeout_seconds
                )
                log_parts.append(f"=== {command}: exit {code}\n{out}")
                if code != 0:
                    why = "timed out" if code == 124 else "failed"
                    return JobResult(
                        False, f"{command} {why} against {rules.service_url}", "\n".join(log_parts), command
                    )
            return JobResult(True, f"{', '.join(commands)} passed against {rules.service_url}", "\n".join(log_parts))
        finally:
            self._run(["docker", "rm", "-f", container], 60)

    def purge(self, rules: DeployRules) -> JobResult:
        """Delete the data, keep the schema and the migration history."""
        env_file, refused = self._env_file(rules)
        if refused is not None or env_file is None:
            return refused or JobResult(False, "no env file")
        script = (
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" '
            '-U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$0"'
        )
        code, out = self._run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                rules.network,
                "--env-file",
                str(env_file),
                PURGE_IMAGE,
                "sh",
                "-c",
                script,
                PURGE_SQL,
            ],  # fmt: skip
            300,
        )
        if code != 0:
            return JobResult(False, f"purge of {rules.database} failed (exit {code})", out)
        return JobResult(True, f"purged {rules.database}: data deleted, schema and migrations kept", out)

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
                self._dir / "results" / path.name,
                {
                    "ok": result.ok,
                    "message": result.message,
                    "log": result.log,
                    "failed_command": result.failed_command,
                },
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
