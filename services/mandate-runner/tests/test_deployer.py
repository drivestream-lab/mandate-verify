"""The deployer (B0-02): two named jobs, no PR code, no key. And the Makefile-target probe."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mandate_runner.deployer import (
    COLLECTOR_IMAGE,
    DEPLOY,
    PURGE,
    RESET,
    VERIFY,
    Deployer,
    DeployerClient,
    DeployerError,
    deploy_tar,
    env_database,
)
from mandate_runner.executor import has_make_target

SHA = "3" * 40
COLLECTOR_CONFIG_FILE = Path(__file__).parents[1] / "collector" / "config.yaml"


class FakeDocker:
    def __init__(self, fail: str | None = None, healthy_after: int = 1):
        self.calls: list[list[str]] = []
        self.fail = fail
        self.healthy_after = healthy_after
        self.health_checks = 0

    def __call__(self, args, timeout):
        args = list(args)
        self.calls.append(args)
        if self.fail and self.fail in " ".join(args):
            return 1, "boom"
        if args[:2] == ["docker", "load"]:
            return 0, f"Loaded image: mandate-deploy:{SHA}\n"
        if args[:2] == ["docker", "exec"]:
            self.health_checks += 1
            return (0, "ok") if self.health_checks >= self.healthy_after else (7, "refused")
        return 0, ""


@pytest.fixture
def lab(cfg, tmp_path):
    env = tmp_path / "mandate.env"
    env.write_text("POSTGRES_HOST=lab-postgres\nPOSTGRES_DB=mandate\nPOSTGRES_PASSWORD=not-logged\n")
    (tmp_path / "secrets").mkdir()
    for name in ("app-password", "owner-password"):
        (tmp_path / "secrets" / name).write_text("throwaway")
    deploy = replace(cfg.develop_run.deploy, env_file=str(env))
    cfg = replace(cfg, develop_run=replace(cfg.develop_run, deploy=deploy))
    jobs = tmp_path / "jobs"
    tar = deploy_tar(jobs, SHA)
    tar.parent.mkdir(parents=True)
    tar.write_bytes(b"image")
    return cfg, jobs, env, tar


def make(cfg, jobs, docker):
    collector_config = COLLECTOR_CONFIG_FILE
    return Deployer(
        cfg,
        jobs,
        reset_command=["make", "-C", "/mv", "runner-reset-sandbox"],
        run=docker,
        sleep=lambda s: None,
        lab_host_dir="/host/.mandate/lab",
        collector_config=str(collector_config),
    )


def test_reset_runs_the_existing_make_target(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    assert make(cfg, jobs, docker).handle({"job": RESET}).ok
    assert docker.calls == [["make", "-C", "/mv", "runner-reset-sandbox"]]


def test_reset_failure_is_reported(lab):
    cfg, jobs, _, _ = lab
    result = make(cfg, jobs, FakeDocker(fail="runner-reset-sandbox")).handle({"job": RESET})
    assert not result.ok and "sandbox reset failed" in result.message


def test_deploy_loads_the_image_and_runs_the_service_on_the_lab_network(lab):
    cfg, jobs, env, tar = lab
    docker = FakeDocker()
    result = make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    assert result.ok, result.message
    assert docker.calls[0] == ["docker", "load", "-i", str(tar)]
    assert docker.calls[1] == ["docker", "tag", f"mandate-deploy:{SHA}", f"drivestream-lab/mandate:{SHA[:12]}"]
    assert docker.calls[2] == ["docker", "rm", "-f", "lab-mandate"]
    assert docker.calls[3] == ["docker", "rm", "-f", "lab-mandate-otel-collector"]
    assert docker.calls[4] == ["docker", "volume", "rm", "-f", "lab-mandate-secrets"]
    assert docker.calls[5] == ["docker", "volume", "create", "lab-mandate-secrets"]
    assert docker.calls[6][:3] == ["docker", "run", "--rm"]
    assert docker.calls[7][:5] == ["docker", "run", "-d", "--name", "lab-mandate-otel-collector"]
    run = docker.calls[8]
    assert run[:3] == ["docker", "run", "-d"]
    assert ["--network", "lab_local"] == run[run.index("--network") : run.index("--network") + 2]
    assert ["--env-file", str(env)] == run[run.index("--env-file") : run.index("--env-file") + 2]
    assert f"mandate.head={SHA}" in run
    assert docker.calls[9] == ["docker", "exec", "lab-mandate", "curl", "-fsS", "http://localhost:8000/health"]
    assert not tar.exists()  # loaded, then removed


def test_deploy_waits_for_health_then_reports_logs_when_it_never_answers(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker(healthy_after=99)
    result = make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and "did not answer" in result.message
    assert docker.calls[-1][:2] == ["docker", "logs"]


def test_deploy_refuses_without_the_lab_env_file(lab):
    cfg, jobs, env, _ = lab
    env.unlink()
    result = make(cfg, jobs, FakeDocker()).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and "mandate.env" in result.message


def test_deploy_refuses_an_env_file_pointing_at_another_database(lab):
    cfg, jobs, env, _ = lab
    env.write_text("POSTGRES_DB=postgres\n")
    docker = FakeDocker()
    result = make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and "POSTGRES_DB=mandate" in result.message
    assert docker.calls == []


def test_deploy_failing_step_is_named(lab):
    cfg, jobs, _, _ = lab
    result = make(cfg, jobs, FakeDocker(fail="--name lab-mandate --network")).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and result.message.startswith("docker run failed")


def test_deploy_names_a_failed_password_install(lab):
    cfg, jobs, _, _ = lab
    result = make(cfg, jobs, FakeDocker(fail="install -m")).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and result.message.startswith("install the password files failed")


@pytest.mark.parametrize("request_", [{"job": DEPLOY, "sha": "../../etc"}, {"job": DEPLOY}, {"job": "rm -rf"}, {}])
def test_requests_cannot_name_anything_but_a_job_and_a_sha(lab, request_):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    assert not make(cfg, jobs, docker).handle(request_).ok
    assert docker.calls == []


def test_deploy_is_refused_when_disabled(lab):
    cfg, jobs, _, _ = lab
    off = replace(cfg, develop_run=replace(cfg.develop_run, deploy=replace(cfg.develop_run.deploy, enabled=False)))
    assert not make(off, jobs, FakeDocker()).handle({"job": DEPLOY, "sha": SHA}).ok


def test_env_database_reads_postgres_db_only(tmp_path):
    env = tmp_path / "e"
    env.write_text('# c\nPOSTGRES_USER=x\nPOSTGRES_DB="mandate_lab"\n')
    assert env_database(env) == "mandate_lab"


def test_client_and_deployer_round_trip_over_the_jobs_volume(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    deployer = make(cfg, jobs, docker)

    def serve_once(_):
        deployer.process_pending()

    client = DeployerClient(jobs, poll=0.0, sleep=serve_once)
    assert client.reset_sandbox().ok
    assert list((jobs / "requests").glob("*")) == [] and list((jobs / "results").glob("*")) == []


def test_client_gives_up_when_the_deployer_is_down(tmp_path):
    client = DeployerClient(tmp_path, poll=1.0, sleep=lambda s: None)
    with pytest.raises(DeployerError, match="did not finish"):
        client.request(RESET, timeout=3)
    assert list((tmp_path / "requests").glob("*")) == []


def test_deploy_tar_rejects_anything_but_a_sha(tmp_path):
    with pytest.raises(ValueError):
        deploy_tar(tmp_path, "../x")


# --- has_make_target -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("journeys:\n\tpytest tests/journeys\n", True),
        (".PHONY: journeys\njourneys: setup\n\tx\n", True),
        ("journeys::\n\tx\n", True),
        ("journeys := tests/journeys\n", False),
        ("journeys ::= x\n", False),
        ("test:\n\tpytest\n", False),
        ("  journeys:\n", False),
    ],
)
def test_has_make_target(tmp_path, text, found):
    (tmp_path / "Makefile").write_text(text)
    assert has_make_target(tmp_path / "Makefile", "journeys") is found


def test_has_make_target_without_a_makefile(tmp_path):
    assert not has_make_target(Path(tmp_path / "Makefile"), "journeys")


# --- verify and purge -------------------------------------------------------------------

RUN = "drivestream-lab__mandate-" + SHA[:12] + "-20261126T120000000000Z"


def test_verify_runs_the_develop_commands_on_the_lab_network_against_the_service(lab):
    cfg, jobs, env, _ = lab
    docker = FakeDocker()
    result = make(cfg, jobs, docker).handle({"job": VERIFY, "sha": SHA, "run_id": RUN})
    assert result.ok, result.message
    create = next(c for c in docker.calls if c[:2] == ["docker", "create"])
    assert ["--network", "lab_local"] == create[create.index("--network") : create.index("--network") + 2]
    assert ["--env-file", str(env)] == create[create.index("--env-file") : create.index("--env-file") + 2]
    assert "MANDATE_SERVICE_URL=http://lab-mandate:8000" in create
    assert ["-w", f"/work/{RUN}"] == create[create.index("-w") : create.index("-w") + 2]
    assert "/var/run/docker.sock" not in " ".join(create)  # no engine for the merged code
    # the merged code never gets the owner's migration keys
    assert "POSTGRES_MIGRATION_USER=" in create and "POSTGRES_MIGRATION_PASSWORD=" in create
    assert create.index("POSTGRES_MIGRATION_PASSWORD=") > create.index("--env-file")
    execs = [c[-1] for c in docker.calls if c[:2] == ["docker", "exec"]]
    assert execs == ["make setup", "make verify"]
    assert docker.calls[-1][:3] == ["docker", "rm", "-f"]  # always removed


def test_verify_names_the_failing_command(lab):
    cfg, jobs, _, _ = lab

    class FailVerify(FakeDocker):
        def __call__(self, args, timeout):
            if list(args)[:2] == ["docker", "exec"] and list(args)[-1] == "make verify":
                self.calls.append(list(args))
                return 2, "journey failed"
            return super().__call__(args, timeout)

    result = make(cfg, jobs, FailVerify()).handle({"job": VERIFY, "sha": SHA, "run_id": RUN})
    assert not result.ok and result.failed_command == "make verify"
    assert "journey failed" in result.log


def test_verify_builds_the_host_job_image_once(lab):
    cfg, jobs, _, _ = lab

    class NoImage(FakeDocker):
        def __call__(self, args, timeout):
            if list(args)[:3] == ["docker", "image", "inspect"]:
                self.calls.append(list(args))
                return 1, "no such image"
            return super().__call__(args, timeout)

    docker = NoImage()
    assert make(cfg, jobs, docker).handle({"job": VERIFY, "sha": SHA, "run_id": RUN}).ok
    assert any(c[:2] == ["docker", "build"] for c in docker.calls)


@pytest.mark.parametrize("bad", [{"sha": SHA, "run_id": "../x"}, {"sha": SHA}, {"run_id": RUN}])
def test_verify_rejects_anything_but_a_sha_and_a_run_id(lab, bad):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    assert not make(cfg, jobs, docker).handle({"job": VERIFY, **bad}).ok
    assert docker.calls == []


def test_purge_deletes_data_and_keeps_schema_and_migrations(lab):
    cfg, jobs, env, _ = lab
    docker = FakeDocker()
    result = make(cfg, jobs, docker).handle({"job": PURGE})
    assert result.ok and "schema, migrations and ledger kept" in result.message
    (call,) = docker.calls
    assert ["--network", "lab_local"] == call[call.index("--network") : call.index("--network") + 2]
    assert ["--env-file", str(env)] == call[call.index("--env-file") : call.index("--env-file") + 2]
    sql = call[-1]
    assert "TRUNCATE" in sql and "alembic_version" in sql and "DROP" not in sql.upper()


def test_purge_connects_as_the_owner_and_refuses_without_owner_keys(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    make(cfg, jobs, docker).handle({"job": PURGE})
    script = docker.calls[0][-2]
    assert '-U "$POSTGRES_MIGRATION_USER"' in script
    assert 'PGPASSWORD="$(cat /run/mandate/owner-password)"' in script
    assert '"$POSTGRES_USER"' not in script
    assert "exit 3" in script


def test_purge_never_touches_the_ledger(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    result = make(cfg, jobs, docker).handle({"job": PURGE})
    assert result.ok and "ledger kept" in result.message
    sql = docker.calls[0][-1]
    assert "'ledger'" in sql.split("NOT IN", 1)[1].split(")", 1)[0]


def test_purge_refuses_an_env_file_for_another_database(lab):
    cfg, jobs, env, _ = lab
    env.write_text("POSTGRES_DB=paperclip\n")
    docker = FakeDocker()
    assert not make(cfg, jobs, docker).handle({"job": PURGE}).ok
    assert docker.calls == []


def test_deploy_installs_the_password_files_into_a_volume_and_runs_as_root(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    install = next(c for c in docker.calls if c[:3] == ["docker", "run", "--rm"])
    assert "/host/.mandate/lab/secrets:/run/mandate:ro" in install
    assert "install -m 0600 -o 0 -g 0" in install[-1] and "install -m 0644" in install[-1]
    run = next(c for c in docker.calls if c[:3] == ["docker", "run", "-d"] and "lab-mandate" in c)
    volume = run[run.index("-v") + 1]
    assert volume.endswith("-secrets:/run/mandate:ro") and not volume.startswith("/")
    assert ["--user", "0:0"] == run[run.index("--user") : run.index("--user") + 2]
    assert docker.calls.index(install) < docker.calls.index(run)


def test_deploy_refuses_without_the_password_files(lab):
    cfg, jobs, env, _ = lab
    (env.parent / "secrets" / "owner-password").unlink()
    docker = FakeDocker()
    result = make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and "owner-password" in result.message
    assert not any(c[:3] == ["docker", "run", "-d"] for c in docker.calls)


def test_deploy_starts_the_collector_before_the_service_and_points_the_service_at_it(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    runs = [c for c in docker.calls if c[:3] == ["docker", "run", "-d"]]
    collector, service = runs
    assert collector[collector.index("--name") + 1] == "lab-mandate-otel-collector"
    assert ["--network", "lab_local"] == collector[collector.index("--network") : collector.index("--network") + 2]
    assert ["--restart", "unless-stopped"] == collector[collector.index("--restart") : collector.index("--restart") + 2]
    assert COLLECTOR_IMAGE == "otel/opentelemetry-collector:0.116.1"
    assert collector[-2:] == [COLLECTOR_IMAGE, "--config=/etc/otelcol/config.yaml"]
    assert f"{COLLECTOR_CONFIG_FILE}:/etc/otelcol/config.yaml:ro" in collector
    assert ["-e", "OTEL_EXPORTER_OTLP_ENDPOINT=http://lab-mandate-otel-collector:4318"] == service[
        service.index("-e") : service.index("-e") + 2
    ]


def test_deploy_names_a_failed_collector_start_and_does_not_start_the_service(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker(fail=COLLECTOR_IMAGE)
    result = make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and result.message.startswith("start the collector failed")
    assert not any(c[:4] == ["docker", "run", "-d", "--name"] and "lab-mandate" in c[4:5] for c in docker.calls)


def test_deploy_removes_the_previous_collector_with_the_previous_service(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker(fail="rm -f lab-mandate-otel-collector")
    result = make(cfg, jobs, docker).handle({"job": DEPLOY, "sha": SHA})
    assert not result.ok and result.message.startswith("remove the previous collector failed")


def test_purge_reads_the_owner_password_file(lab):
    cfg, jobs, _, _ = lab
    docker = FakeDocker()
    make(cfg, jobs, docker).handle({"job": PURGE})
    call = docker.calls[0]
    assert "/host/.mandate/lab/secrets:/run/mandate:ro" in call
    assert "cat /run/mandate/owner-password" in call[-2]
    assert "POSTGRES_MIGRATION_PASSWORD" not in call[-2]
