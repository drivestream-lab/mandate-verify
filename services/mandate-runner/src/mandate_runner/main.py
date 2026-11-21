"""Entry point: load config and credentials, check identities, poll until stopped."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

from . import config as config_mod
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


def main() -> int:
    configure_logging()
    cfg = config_mod.load(Path(os.environ.get("MANDATE_RUNNER_CONFIG", "/etc/mandate-runner/runner.toml")))
    creds = load_credentials(Path(os.environ.get("MANDATE_RUNNER_SECRETS_DIR", "/run/secrets/mandate-runner")))
    if creds.app_id != cfg.identities.runner_app_id:
        raise IdentityError("app-id in the secrets folder is not the configured runner App ID")
    gh = GitHubApp(creds, cfg.status_context)
    verify_identities(gh, cfg)

    store = Store(cfg.data_dir)
    test_secrets = os.environ.get("MANDATE_RUNNER_TEST_SECRETS") == "1"
    executor = DockerSandboxExecutor(cfg, gh.installation_token, test_secrets=test_secrets)
    serve(store.logs_dir, "0.0.0.0", int(os.environ.get("MANDATE_RUNNER_LOG_PORT", "8090")))
    runner = Runner(cfg, gh, executor, store)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    heartbeat = cfg.data_dir / "heartbeat"
    log.info(
        "mandate-runner up: %s every %ss; test secrets %s",
        ", ".join(r.name for r in cfg.repos),
        cfg.poll_interval_seconds,
        "on" if test_secrets else "off",
    )
    while not stop.is_set():
        started = time.monotonic()
        runner.poll_once()
        heartbeat.write_text(str(int(time.time())))
        stop.wait(max(1.0, cfg.poll_interval_seconds - (time.monotonic() - started)))
    runner.shutdown()
    log.info("mandate-runner stopped")
    return 0
