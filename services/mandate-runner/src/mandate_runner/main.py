"""Entry point: load config and credentials, check identities, poll until stopped."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import config as config_mod
from . import deployer as deployer_mod
from . import proxy as proxy_mod
from .deployer import DeployerClient
from .executor import DockerSandboxExecutor
from .github import GitHubApp
from .logserver import serve
from .secrets import configure_logging, load_credentials
from .service import Runner
from .store import Store

log = logging.getLogger("mandate_runner")


class IdentityError(RuntimeError):
    pass


def verify_identities(gh: GitHubApp, cfg: config_mod.Config) -> None:
    """Refuse to start if the key is not the runner App's, or the challenger slug is not the configured App."""
    if gh.authenticated_app_id() != cfg.identities.runner_app_id:
        raise IdentityError("the mounted key does not belong to the configured runner App")
    if gh.app_id_for_slug(cfg.identities.challenger_app_slug) != cfg.identities.challenger_app_id:
        raise IdentityError(f"App '{cfg.identities.challenger_app_slug}' does not have the configured App ID")


def main(argv: list[str] | None = None) -> int:
    """`runner` (default), `proxy` (the sandbox's egress) or `deployer` (host Docker jobs)."""
    configure_logging()
    role = (argv if argv is not None else sys.argv[1:] or ["runner"])[0]
    if role == "proxy":
        return proxy_mod.main(int(os.environ.get("MANDATE_RUNNER_PROXY_PORT", "3128")))
    cfg = config_mod.load(Path(os.environ.get("MANDATE_RUNNER_CONFIG", "/etc/mandate-runner/runner.toml")))
    if role == "deployer":
        return deployer_mod.main(cfg)
    if role != "runner":
        raise SystemExit(f"unknown role {role!r}: runner, proxy or deployer")
    creds = load_credentials(Path(os.environ.get("MANDATE_RUNNER_SECRETS_DIR", "/run/secrets/mandate-runner")))
    if creds.app_id != cfg.identities.runner_app_id:
        raise IdentityError("app-id in the secrets folder is not the configured runner App ID")
    gh = GitHubApp(creds, cfg.status_context)
    verify_identities(gh, cfg)

    store = Store(cfg.data_dir)
    test_secrets = os.environ.get("MANDATE_RUNNER_TEST_SECRETS") == "1"
    jobs_dir = Path(os.environ.get("MANDATE_RUNNER_JOBS_DIR", "/jobs"))
    executor = DockerSandboxExecutor(
        cfg, gh.installation_token, test_secrets=test_secrets, deployer=DeployerClient(jobs_dir), jobs_dir=jobs_dir
    )
    serve(store.logs_dir, "0.0.0.0", int(os.environ.get("MANDATE_RUNNER_LOG_PORT", "8090")))
    # GitHub's clock: Docker Desktop's VM clock can lag after the laptop sleeps (PR 4).
    runner = Runner(cfg, gh, executor, store, clock=gh.now)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    heartbeat = cfg.data_dir / "heartbeat"
    dr = cfg.develop_run
    log.info(
        "mandate-runner up: %s every %ss; test secrets %s; develop run %s",
        ", ".join(r.name for r in cfg.repos),
        cfg.poll_interval_seconds,
        "on" if test_secrets else "off",
        f"{dr.repo}@{dr.branch} (deploy {'on' if dr.deploy.enabled else 'off'})" if dr else "off",
    )
    while not stop.is_set():
        started = time.monotonic()
        runner.poll_once()
        heartbeat.write_text(str(int(time.time())))
        stop.wait(max(1.0, cfg.poll_interval_seconds - (time.monotonic() - started)))
    runner.shutdown()
    log.info("mandate-runner stopped")
    return 0
