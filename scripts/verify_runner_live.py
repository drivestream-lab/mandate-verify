#!/usr/bin/env python3
"""Live verification of mandate-runner (B0-01) before B0-02.

Runs thirteen scenarios against the RUNNING runner and checks each mandate/build-verified
status it posts, plus the develop run and merge freeze (B0-02). The runner must be
started with the local override, which points it at drivestream-lab/mandate-sandbox-svc
only, with make setup, make check, make test, a develop run on a test branch, and a
the same App signer as the real config:

    make deploy RUNNER_LOCAL=1
    make verify                        # or: make verify ARGS=--cleanup to sweep leftovers only

Who does what (GitHub Apps only; no person's account is used, KD 27 Nov 2026):
  - mandate-builder (~/.mandate/secrets/build/mandate-builder/) pushes the test
    branches, opens every test PR, adds the wave label and marks the wave PR ready.
  - mandate-challenger (~/.mandate/secrets/build/mandate-challenger/) posts every
    review: Challenger: first, Challenger: second (trust-critical and protected paths,
    the fix for a red develop) and the wave signoff (Signoff: wave plus attestation).
    Scenario 1 checks that both configs name the App as signer and no person.

Every test PR is closed and every test branch deleted at the end, even on failure.
The script never prints keys or tokens. It reads the runner's logs through
`docker logs` and the log server on 127.0.0.1:8090.
"""

from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

REPO = "drivestream-lab/mandate-sandbox-svc"
API = "https://api.github.com"
CONTEXT = "mandate/build-verified"
RUNNER_BOT = "mandate-runner[bot]"
SIGNER = "mandate-challenger[bot]"  # the wave signer, in both configs
PEOPLE = ("nikd10x", "0xbeefdead")  # never named as a signer, never used by this script
INITIATIVE = "INIT-MANDATE-001"
WAVE_LABEL = "wave-accepted"
SECRETS = Path.home() / ".mandate" / "secrets" / "build"
RUNNER_CONTAINER = "lab-mandate-runner"
LOG_SERVER = "http://127.0.0.1:8090"
# Matches the sandbox repos' policy-branch-name check: <prefix>/<slug>, prefix chore.
PREFIX = "chore/verify-runner-"
TITLE = "[verify-runner-live]"

RUN_TIMEOUT = 30 * 60  # first run builds the job image and runs make setup
RULE_TIMEOUT = 3 * 60  # a review change shows within a poll or two
SETTLE = 75  # two polls: long enough to see that nothing changes

LEAK_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)


class Fail(Exception):
    pass


def say(text: str) -> None:
    print(f"{datetime.now(UTC).strftime('%H:%M:%S')} {text}", flush=True)


# --- identities ---------------------------------------------------------------------


class App:
    """A GitHub App from its secrets folder. Its key and tokens never leave this object."""

    def __init__(self, name: str) -> None:
        folder = SECRETS / name
        self.name = name
        self._app_id = (folder / "app-id").read_text().strip()
        self._installation = (folder / "installation-id").read_text().strip()
        self._key = (folder / "private-key.pem").read_bytes()
        self._token = ""
        self._expires = 0.0
        self._http = httpx.Client(timeout=30.0, headers={"User-Agent": "verify-runner-live"})

    def __repr__(self) -> str:
        return f"App({self.name})"

    def _bearer(self) -> str:
        if time.time() > self._expires - 300:
            now = int(time.time())
            token = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self._app_id},
                self._key,
                algorithm="RS256",
            )
            resp = self._http.post(
                f"{API}/app/installations/{self._installation}/access_tokens",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code != 201:
                raise Fail(f"{self.name}: cannot mint an installation token (HTTP {resp.status_code})")
            self._token = resp.json()["token"]
            self._expires = time.time() + 50 * 60
        return self._token

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, **params: Any) -> Any:
        for attempt in range(4):
            try:
                resp = self._http.request(
                    method,
                    f"{API}{path}",
                    json=body,
                    params=params or None,
                    headers={
                        "Authorization": f"token {self._bearer()}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise Fail(f"{self.name}: {method} {path}: {type(exc).__name__}") from None
                time.sleep(3)
                continue
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(3)
                continue
            if resp.status_code >= 400:
                message = resp.json().get("message", "") if resp.content else ""
                raise Fail(f"{self.name}: {method} {path}: HTTP {resp.status_code} {message}")
            return resp.json() if resp.content else None
        raise Fail(f"{self.name}: {method} {path}: gave up")

    def key_fragments(self) -> list[str]:
        """Slices of the key body, to search logs for. Never printed."""
        return key_fragments(self._key)


def key_fragments(pem: bytes) -> list[str]:
    body = [ln for ln in pem.decode().splitlines() if ln and not ln.startswith("-----")]
    joined = "".join(body)
    return [joined[i : i + 32] for i in range(16, len(joined) - 32, max(1, len(joined) // 8))]


_REPO_APP: list[App] = []


def repo_api(method: str, path: str, **fields: str) -> Any:
    """Branches, files and refs on the sandbox repo, as mandate-builder."""
    app = _REPO_APP[0]
    path = "/" + path.lstrip("/")
    if method == "GET":
        return app.call(method, path, **fields)
    return app.call(method, path, dict(fields) or None)


# --- the run's state ------------------------------------------------------------------


@dataclass
class TestPR:
    key: str
    number: int
    branch: str  # "develop" for the wave PR, never deleted
    head: str
    heads: list[str] = field(default_factory=list)


@dataclass
class Run:
    tag: str
    builder: App
    challenger: App
    prs: dict[str, TestPR] = field(default_factory=dict)
    created_label: bool = False
    had_freeze_label: bool = True
    freeze_issue: int | None = None
    develop_heads: list[str] = field(default_factory=list)

    # --- branches and PRs ---------------------------------------------------------

    def develop_sha(self) -> str:
        return repo_api("GET", f"repos/{REPO}/git/ref/heads/develop")["object"]["sha"]

    def push_branch(self, key: str, files: dict[str, str | None]) -> tuple[str, str]:
        """Branch from develop as mandate-builder; `None` content means append to the existing file."""
        branch = f"{PREFIX}{self.tag}-{key}"
        repo_api(
            "POST",
            f"repos/{REPO}/git/refs",
            ref=f"refs/heads/{branch}",
            sha=self.develop_sha(),
        )
        head = ""
        for path, content in files.items():
            head = self.put_file(branch, path, content)
        return branch, head

    def put_file(self, branch: str, path: str, content: str | None) -> str:
        fields = {"message": f"verify-runner-live {self.tag}: {path}", "branch": branch}
        try:
            current = repo_api("GET", f"repos/{REPO}/contents/{path}", ref=branch)
        except Fail as exc:
            if "HTTP 404" not in str(exc):
                raise
            current = None
        if current is not None:
            fields["sha"] = current["sha"]
            if content is None:
                content = base64.b64decode(current["content"]).decode() + f"\n# verify-runner-live {self.tag}\n"
        elif content is None:
            raise Fail(f"{path} does not exist on {branch}")
        fields["content"] = base64.b64encode(content.encode()).decode()
        return repo_api("PUT", f"repos/{REPO}/contents/{path}", **fields)["commit"]["sha"]

    def open_pr(
        self,
        key: str,
        branch: str,
        head: str,
        title: str,
        *,
        base: str = "develop",
        draft: bool = False,
        body: str = "",
    ) -> TestPR:
        pr = self.builder.call(
            "POST",
            f"/repos/{REPO}/pulls",
            {
                "title": f"{TITLE} {self.tag} {title}",
                "head": branch,
                "base": base,
                "draft": draft,
                "body": (body + "\n\n" if body else "")
                + "Opened by scripts/verify_runner_live.py (mandate-verify). Closed automatically; do not merge.",
            },
        )
        if pr["head"]["sha"] != head:
            raise Fail(f"PR #{pr['number']} head is {pr['head']['sha'][:12]}, expected {head[:12]}")
        test_pr = TestPR(key=key, number=pr["number"], branch=branch, head=head, heads=[head])
        self.prs[key] = test_pr
        say(f"opened #{test_pr.number} ({key}) by mandate-builder: https://github.com/{REPO}/pull/{test_pr.number}")
        return test_pr

    def task_pr(self, key: str, title: str, files: dict[str, str | None], body: str = "") -> TestPR:
        branch, head = self.push_branch(key, files)
        return self.open_pr(key, branch, head, title, body=body)

    def push(self, pr: TestPR, path: str, content: str) -> str:
        head = self.put_file(pr.branch, path, content)
        pr.head = head
        pr.heads.append(head)
        say(f"pushed {head[:12]} to #{pr.number}")
        return head

    # --- reviews ------------------------------------------------------------------

    def challenger_approve(self, pr: TestPR, marker: str) -> None:
        self.challenger.call(
            "POST",
            f"/repos/{REPO}/pulls/{pr.number}/reviews",
            {
                "commit_id": pr.head,
                "event": "APPROVE",
                "body": f"verify-runner-live {self.tag}: approve for the runner check.\n\n{marker}",
            },
        )
        say(f"mandate-challenger approved #{pr.number} at {pr.head[:12]} ({marker})")

    def add_label(self, pr: TestPR, label: str) -> None:
        self.builder.call("POST", f"/repos/{REPO}/issues/{pr.number}/labels", {"labels": [label]})

    def mark_ready(self, pr: TestPR) -> None:
        node = self.builder.call("GET", f"/repos/{REPO}/pulls/{pr.number}")["node_id"]
        out = self.builder.call(
            "POST",
            "/graphql",
            {
                "query": "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
                "{ pullRequest { isDraft } } }",
                "variables": {"id": node},
            },
        )
        if out.get("errors"):
            raise Fail(f"mandate-builder could not mark #{pr.number} ready: {out['errors'][0].get('message', '')}")

    # --- statuses -----------------------------------------------------------------

    def statuses(self, sha: str) -> list[dict[str, Any]]:
        items = self.builder.call("GET", f"/repos/{REPO}/commits/{sha}/statuses", per_page=100)
        return [s for s in items if s["context"] == CONTEXT]

    def latest(self, sha: str) -> dict[str, Any] | None:
        found = self.statuses(sha)
        return found[0] if found else None  # newest first

    def wait(self, sha: str, state: str, contains: str = "", timeout: int = RUN_TIMEOUT) -> dict[str, Any]:
        """Wait until the newest status on sha has this state and description text."""
        deadline = time.time() + timeout
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            last = self.latest(sha)
            if last and last["state"] == state and contains in (last.get("description") or ""):
                say(f"  {sha[:12]}: {state}: {last.get('description')}")
                return last
            time.sleep(10)
        got = f"{last['state']}: {last.get('description')}" if last else "no status"
        raise Fail(f"{sha[:12]}: wanted {state} containing '{contains}', last was {got}")

    def hold(self, sha: str, state: str, contains: str) -> dict[str, Any]:
        """The status stays as it is for two more polls."""
        time.sleep(SETTLE)
        return self.wait(sha, state, contains, timeout=5)


def run_logs_for(sha: str) -> list[str]:
    out = subprocess.run(
        ["docker", "exec", RUNNER_CONTAINER, "ls", "/var/lib/mandate-runner/logs"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise Fail(f"cannot list run logs: {out.stderr.strip()[:200]}")
    return [name for name in out.stdout.split() if f"-{sha[:12]}-" in name]


def fetch_log(url: str) -> str:
    if not url.startswith(LOG_SERVER.replace("127.0.0.1", "localhost")) and not url.startswith(LOG_SERVER):
        raise Fail(f"status links somewhere unexpected: {url}")
    resp = httpx.get(url.replace("localhost", "127.0.0.1"), timeout=30)
    if resp.status_code != 200:
        raise Fail(f"run log {url}: HTTP {resp.status_code}")
    return resp.text


def leaks(text: str, fragments: list[str]) -> list[str]:
    found = [rx.pattern[:24] for rx in LEAK_PATTERNS if rx.search(text)]
    if any(f in text for f in fragments):
        found.append("a fragment of a build App key")
    return found


def one_run(sha: str) -> None:
    logs = run_logs_for(sha)
    if len(logs) != 1:
        raise Fail(f"{sha[:12]} has {len(logs)} run logs, expected exactly 1: {logs}")


# --- file contents for the test PRs ------------------------------------------------------

BROKEN_TEST = '''"""verify-runner-live: a deliberately failing test."""


def test_verify_runner_broken() -> None:
    assert 1 + 1 == 3
'''

LEDGER_TEST = '''"""verify-runner-live: a passing test on a trust-critical path (tests/**/*ledger*)."""


def test_verify_runner_ledger_probe() -> None:
    assert 1 + 1 == 2
'''

SECRETS_PROBE = r'''"""verify-runner-live: the job must see no build key and no GitHub token.

Prints the environment and lists /run/secrets, ~/.mandate, /secrets and /certs, then
searches them, and the sandbox engine's own filesystem, for keys and tokens.
"""

import os
import re
import shutil
import subprocess
import warnings
from pathlib import Path

TOKEN = re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}")
TOKEN_ENV = {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APP_PRIVATE_KEY", "MANDATE_RUNNER_SECRETS_DIR"}
PLACES = [Path("/run/secrets"), Path.home() / ".mandate", Path("/secrets"), Path("/certs")]


def listing() -> list[str]:
    found: list[str] = []
    for place in PLACES:
        if place.exists():
            found.append(str(place))
            found.extend(str(p) for p in sorted(place.rglob("*")))
    return found


def secret_in(text: str) -> bool:
    return "PRIVATE KEY" in text or TOKEN.search(text) is not None


def leaks() -> list[str]:
    found: list[str] = []
    for name, value in os.environ.items():
        if name in TOKEN_ENV or secret_in(value):
            found.append(f"env:{name}")
    for item in listing():
        path = Path(item)
        if path.is_file():
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            if secret_in(text) or path.name == "private-key.pem":
                found.append(f"file:{item}")
    return found


def engine_leaks() -> list[str]:
    """Through the sandbox's Docker engine: any App key on the engine's own filesystem."""
    if shutil.which("docker") is None:
        warnings.warn("SECRETS_PROBE_ENGINE: no docker CLI, engine not searched")
        return []
    script = (
        "find /host -xdev \\( -path /host/proc -o -path /host/sys -o -path /host/var/lib/docker \\) "
        "-prune -o \\( -name private-key.pem -o -path '*/.mandate/*' \\) -print 2>/dev/null"
    )
    res = subprocess.run(
        ["docker", "run", "--rm", "-v", "/:/host:ro", "busybox:1.37", "sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if res.returncode != 0:
        warnings.warn(f"SECRETS_PROBE_ENGINE: not searched ({res.stderr.strip()[:120]})")
        return []
    warnings.warn("SECRETS_PROBE_ENGINE: searched the sandbox engine's filesystem")
    return [f"engine:{line}" for line in res.stdout.split() if line]


def shown(value: str) -> str:
    return "<token-like value>" if secret_in(value) else value


def test_job_sees_no_build_key_or_token() -> None:
    env = {name: shown(value) for name, value in sorted(os.environ.items())}
    places = listing()
    found = leaks() + engine_leaks()
    warnings.warn(f"SECRETS_PROBE_ENV: {env}")
    warnings.warn(f"SECRETS_PROBE_LISTING: {places or 'nothing under ' + str(PLACES)}")
    if found:
        warnings.warn(f"SECRETS_PROBE_RESULT: FOUND {found}")
    else:
        warnings.warn("SECRETS_PROBE_RESULT: clean")
    assert not found
'''

ISOLATION_PROBE = r'''"""verify-runner-live: PR code in the sandbox must not reach the lab's services.

Tries each lab service directly (by its lab_local address and through Docker
Desktop's host address, where the lab publishes its ports), then from the sandbox
engine's own network (docker run --network host), then through the egress proxy.
Only the proxy's way to the public internet may work.
"""

import os
import socket
import subprocess
import warnings
from urllib.parse import urlsplit

TARGETS = __TARGETS__


def reach(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False


def proxy_connect(host: str, port: int) -> str:
    proxy = urlsplit(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
    if not proxy.hostname:
        return "no proxy configured"
    with socket.create_connection((proxy.hostname, proxy.port or 3128), timeout=10) as s:
        s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        return s.recv(200).split(b"\r\n", 1)[0].decode(errors="replace")


def hostnet_reached() -> list[str]:
    script = "; ".join(
        f"nc -z -w3 {h} {p} && echo REACHED {label}" for label, h, p in TARGETS if not h.startswith("host.")
    )
    res = subprocess.run(
        ["docker", "run", "--rm", "--network", "host", "busybox:1.37", "sh", "-c", script + "; true"],
        capture_output=True, text=True, timeout=600,
    )
    if res.returncode != 0:
        raise AssertionError(f"could not probe from the engine's network: {res.stderr.strip()[:200]}")
    warnings.warn("ISOLATION_PROBE_HOSTNET: searched")
    return [line.split(" ", 1)[1] for line in res.stdout.splitlines() if line.startswith("REACHED ")]


def test_sandbox_cannot_reach_the_lab() -> None:
    reached = []
    for label, host, port in TARGETS:
        ok = reach(host, port)
        warnings.warn(f"ISOLATION_PROBE {label} {host}:{port} {'REACHABLE' if ok else 'blocked'}")
        if ok:
            reached.append(label)
    reached += [f"hostnet:{label}" for label in hostnet_reached()]
    via_proxy = proxy_connect("192.168.65.254", 443)
    warnings.warn(f"ISOLATION_PROBE_PROXY_LAB: {via_proxy}")
    if " 200 " in f"{via_proxy} ":
        reached.append("proxy:192.168.65.254:443")
    public = proxy_connect("pypi.org", 443)
    warnings.warn(f"ISOLATION_PROBE_PROXY_PUBLIC: {public}")
    assert " 200 " in f"{public} ", "the proxy should still reach the public internet"
    warnings.warn(f"ISOLATION_PROBE_RESULT: {'REACHED ' + str(reached) if reached else 'blocked'}")
    assert not reached
'''


def lab_targets() -> list[tuple[str, str, int]]:
    """The lab's services by their lab_local address, and through Docker Desktop's host address."""
    targets: list[tuple[str, str, int]] = []
    for name, port in (
        ("lab-postgres", 5432),
        ("lab-redis", 6379),
        ("lab-litellm", 8000),
        ("lab-paperclip", 3100),
    ):
        ip = subprocess.run(
            ["docker", "inspect", "-f", '{{(index .NetworkSettings.Networks "lab_local").IPAddress}}', name],
            capture_output=True, text=True,
        ).stdout.strip()  # fmt: skip
        if ip:
            targets.append((f"{name}@lab_local", ip, port))
    for label, port in (
        ("postgres", 5432),
        ("redis", 6379),
        ("litellm", 4000),
        ("paperclip", 3100),
    ):
        targets.append((f"{label}@host", "192.168.65.254", port))
    targets.append(("postgres@host.docker.internal", "host.docker.internal", 5432))
    return targets


def isolation_probe() -> str:
    return ISOLATION_PROBE.replace("__TARGETS__", repr(lab_targets()))


DEVELOP_BRANCH = "chore/verify-runner-develop"  # the override's develop_run.branch
FREEZE_LABEL = "develop-red"
DEPLOYED = "lab-mandate-verify"  # the override's develop_run.deploy.container
DEPLOYED_DB = "mandate_verify_lab"  # the override's develop_run.deploy.database
# The test branch's `make verify`: sandbox-svc's one feature is its health endpoint, so its
# journey is that the deployed service answers it. The red one is a journey that fails.
RED_VERIFY = "\nverify:\n\t@echo 'verify-runner-live: a journey fails'; exit 1\n"
GREEN_VERIFY = (
    "\nverify:\n"
    '\t@curl -fsS "$$MANDATE_SERVICE_URL/health" | grep -q \'"status":"ok"\'\n'
    '\t@echo "verify-runner-live: $$MANDATE_SERVICE_URL/health answers ok"\n'
)


# --- scenarios --------------------------------------------------------------------------

Scenario = Callable[[Run], str]


def s1_runner_up(run: Run) -> str:
    health = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Health.Status}}", RUNNER_CONTAINER],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if health != "healthy":
        raise Fail(f"{RUNNER_CONTAINER} is '{health or 'missing'}', not healthy")
    if httpx.get(f"{LOG_SERVER}/healthz", timeout=10).text.strip() != "ok":
        raise Fail("log server does not answer")
    config = subprocess.run(
        ["docker", "exec", RUNNER_CONTAINER, "sh", "-c", 'cat "$MANDATE_RUNNER_CONFIG"'],
        capture_output=True,
        text=True,
    ).stdout
    names = re.findall(r'^name = "([^"]+)"', config, re.M)
    if names != [REPO]:
        raise Fail(f"runner grades {names}; start it with `make deploy RUNNER_LOCAL=1`")
    if '"make setup", "make check", "make test"' not in config:
        raise Fail("runner's command set is not make setup, make check, make test")
    real = (Path(__file__).resolve().parent.parent / "config" / "mandate-runner" / "runner.toml").read_text()
    for which, text in (("the running config", config), ("runner.toml", real)):
        if f'signer_login = "{SIGNER}"' not in text:
            raise Fail(f"{which} does not name {SIGNER} as the signer")
        if any(person in text for person in PEOPLE):
            raise Fail(f"{which} names a person; the flow uses GitHub Apps only")
    if f'branch = "{DEVELOP_BRANCH}"' not in config or f'container = "{DEPLOYED}"' not in config:
        raise Fail(f"runner's develop_run is not {DEVELOP_BRANCH} deploying {DEPLOYED}; update runner.local.toml")
    if networks(RUNNER_CONTAINER) != ["lab_local", "mandate_runner_sandbox"]:
        raise Fail(f"{RUNNER_CONTAINER} is on {networks(RUNNER_CONTAINER)}")
    logs = subprocess.run(["docker", "logs", RUNNER_CONTAINER], capture_output=True, text=True).stdout
    logs += subprocess.run(["docker", "logs", RUNNER_CONTAINER], capture_output=True, text=True).stderr
    found = leaks(
        logs,
        key_fragments((SECRETS / "mandate-runner" / "private-key.pem").read_bytes()),
    )
    if found:
        raise Fail(f"runner log contains {found}")
    return f"healthy, grading only {REPO}, {len(logs.splitlines())} log lines with no key, token or JWT"


def s2_clean_pr_needs_challenger(run: Run) -> str:
    pr = run.prs["clean"]
    status = run.wait(pr.head, "failure", "Challenger: first")
    return f"#{pr.number}: failure, '{status['description']}'"


def s3_approve_flips_without_rerun(run: Run) -> str:
    pr = run.prs["clean"]
    before = run.wait(pr.head, "failure", "Challenger: first", timeout=5)
    run.challenger_approve(pr, "Challenger: first")
    after = run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    if after.get("target_url") != before.get("target_url"):
        raise Fail(f"run log changed: {before.get('target_url')} -> {after.get('target_url')}")
    one_run(pr.head)
    return f"#{pr.number}: success on the same run ({after['target_url'].rsplit('/', 1)[-1]}), one run log"


def s4_new_push_needs_fresh_approval(run: Run) -> str:
    pr = run.prs["clean"]
    old = pr.head
    new = run.push(
        pr,
        f"docs/verify-runner/{run.tag}-second.md",
        f"verify-runner-live {run.tag}: second push\n",
    )
    status = run.wait(new, "failure", "Challenger: first")
    run.wait(old, "success", timeout=5)  # the old head keeps its own result
    run.challenger_approve(pr, "Challenger: first")
    run.wait(new, "success", timeout=RULE_TIMEOUT)
    one_run(new)
    return f"#{pr.number}: new head {new[:12]} failed '{status['description']}' until a fresh Approve, then success"


def s5_broken_test_stays_failed(run: Run) -> str:
    pr = run.prs["broken"]
    first = run.wait(pr.head, "failure", "make test failed")
    run.challenger_approve(pr, "Challenger: first")
    later = run.hold(pr.head, "failure", "make test failed")
    if later.get("target_url") != first.get("target_url"):
        raise Fail("the head was graded again after the approval")
    one_run(pr.head)
    return f"#{pr.number}: '{first['description']}', unchanged after the challenger's Approve, one run log"


def s6_protected_path_needs_a_second_challenger(run: Run) -> str:
    pr = run.prs["protected"]
    run.challenger_approve(pr, "Challenger: first")
    status = run.wait(pr.head, "failure", "Makefile is protected")
    run.challenger_approve(pr, "Challenger: second")
    run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    return f"#{pr.number}: '{status['description']}', success after the second challenger Approve"


def s7_trust_critical_needs_two(run: Run) -> str:
    pr = run.prs["critical"]
    run.challenger_approve(pr, "Challenger: first")
    status = run.wait(pr.head, "failure", "Challenger: second")
    run.challenger_approve(pr, "Challenger: second")
    run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    return f"#{pr.number}: '{status['description']}', success after the second Approve"


def s8_wave_pr_needs_signoff(run: Run) -> str:
    pr = run.prs["wave"]
    steps = [run.wait(pr.head, "failure", WAVE_LABEL)["description"]]
    labels = [label["name"] for label in run.builder.call("GET", f"/repos/{REPO}/labels", per_page=100)]
    if WAVE_LABEL not in labels:
        run.builder.call(
            "POST",
            f"/repos/{REPO}/labels",
            {"name": WAVE_LABEL, "description": "Wave close signed by the signer App", "color": "0E8A16"},
        )
        run.created_label = True
        say(f"created label {WAVE_LABEL} on {REPO} (removed at the end)")
    run.add_label(pr, WAVE_LABEL)
    steps.append(run.wait(pr.head, "failure", "draft", timeout=RULE_TIMEOUT)["description"])
    run.mark_ready(pr)
    steps.append(run.wait(pr.head, "failure", "Signoff: wave", timeout=RULE_TIMEOUT)["description"])
    run.challenger_approve(pr, "Challenger: first")  # a review without the signoff marker does not sign
    run.hold(pr.head, "failure", "Signoff: wave")
    run.challenger_approve(pr, f"Signoff: wave\n\nInitiative: {INITIATIVE}\nHead: {pr.head}")
    run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    return f"#{pr.number}: " + " -> ".join(f"'{s}'" for s in steps) + " -> success after the App's attested signoff"


def s9_no_secrets_in_job(run: Run) -> str:
    pr = run.prs["secrets"]
    status = run.wait(pr.head, "failure", "Challenger: first")  # i.e. the commands, probe included, passed
    log = fetch_log(status["target_url"])
    if "SECRETS_PROBE_RESULT: clean" not in log:
        raise Fail("the probe's result is missing from the run log")
    if "SECRETS_PROBE_RESULT: FOUND" in log:
        raise Fail("the probe found secrets")
    if "SECRETS_PROBE_ENGINE: searched" not in log:
        raise Fail("the probe could not search the sandbox engine's filesystem")
    fragments = run.builder.key_fragments() + run.challenger.key_fragments()
    fragments += key_fragments((SECRETS / "mandate-runner" / "private-key.pem").read_bytes())
    found = leaks(log, fragments)
    if found:
        raise Fail(f"run log contains {found}")
    return f"#{pr.number}: probe passed in make test (env, mounts and sandbox engine searched); run log clean"


def networks(container: str) -> list[str]:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", container],
        capture_output=True, text=True,
    ).stdout  # fmt: skip
    return sorted(out.split())


def s11_sandbox_cannot_reach_the_lab(run: Run) -> str:
    dind = networks("lab-mandate-runner-dind")
    if dind != ["mandate_runner_sandbox"]:
        raise Fail(f"the sandbox is on {dind}, expected only mandate_runner_sandbox")
    internal = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "-f",
            "{{.Internal}}",
            "mandate_runner_sandbox",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if internal != "true":
        raise Fail("mandate_runner_sandbox is not an internal network")
    pr = run.prs["isolation"]
    status = run.wait(pr.head, "failure", "Challenger: first")  # i.e. the commands, probe included, passed
    log = fetch_log(status["target_url"])
    if "ISOLATION_PROBE_RESULT: blocked" not in log or "ISOLATION_PROBE_HOSTNET: searched" not in log:
        raise Fail("the isolation probe's result is missing from the run log")
    probes = re.findall(r"ISOLATION_PROBE (\S+) \S+ (blocked|REACHABLE)", log)
    if not probes or any(state != "blocked" for _, state in probes):
        raise Fail(f"reachable from the sandbox: {[label for label, state in probes if state != 'blocked']}")
    lab = re.search(r"ISOLATION_PROBE_PROXY_LAB: (.*)", log)
    return (
        f"#{pr.number}: sandbox only on the internal network; {len(probes)} lab targets blocked directly and from "
        f"the engine's host network; proxy to the lab: '{lab.group(1).strip() if lab else '?'}', to PyPI: 200"
    )


def freeze_issues(run: Run, state: str = "open") -> list[dict[str, Any]]:
    items = run.builder.call("GET", f"/repos/{REPO}/issues", state=state, labels=FREEZE_LABEL, per_page=100)
    return [i for i in items if "pull_request" not in i and i["user"]["login"] == RUNNER_BOT]


def wait_for(what: str, check: Callable[[], Any], timeout: int = RUN_TIMEOUT) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = check()
        if found:
            return found
        time.sleep(10)
    raise Fail(f"timed out waiting for {what}")


def s12_red_develop_freezes(run: Run) -> str:
    clean = run.prs["clean"]
    run.wait(clean.head, "success", timeout=5)  # passes before the freeze
    makefile = base64.b64decode(repo_api("GET", f"repos/{REPO}/contents/Makefile?ref=develop")["content"]).decode()
    repo_api(
        "POST",
        f"repos/{REPO}/git/refs",
        ref=f"refs/heads/{DEVELOP_BRANCH}",
        sha=run.develop_sha(),
    )
    red = run.put_file(DEVELOP_BRANCH, "Makefile", makefile + RED_VERIFY)
    run.develop_heads.append(red)
    say(f"  {DEVELOP_BRANCH} at {red[:12]}: a verify target whose journey fails (stands in for a red merge)")
    issue = wait_for("the runner's freeze issue", lambda: freeze_issues(run))[0]
    run.freeze_issue = issue["number"]
    say(f"  {RUNNER_BOT} opened #{issue['number']}: {issue['title']}")
    body = issue.get("body") or ""
    if f"https://github.com/{REPO}/commit/{red}" not in body:
        raise Fail(f"#{issue['number']} does not link the merge commit {red[:12]}")
    log_url = re.search(r"Run log: (\S+)", body)
    if not log_url or "make verify failed" not in fetch_log(log_url.group(1)):
        raise Fail(f"#{issue['number']} does not link a run log showing make verify failed")
    status = run.wait(clean.head, "failure", "develop is red", timeout=RULE_TIMEOUT)
    if status.get("target_url") != issue["html_url"]:
        raise Fail(f"the freeze status links {status.get('target_url')}, not the issue")
    run.builder.call(
        "PATCH",
        f"/repos/{REPO}/pulls/{clean.number}",
        {"body": f"Fixes #{issue['number']} (no signer Approve)"},
    )
    run.hold(clean.head, "failure", "develop is red")
    wave = run.prs["wave"]
    run.wait(wave.head, "success", timeout=5)  # a wave PR is not a task PR
    red_run = log_url.group(1).rsplit("/", 1)[-1].removesuffix(".log")

    def rebuilt() -> bool:
        out = subprocess.run(["docker", "logs", RUNNER_CONTAINER], capture_output=True, text=True)
        return f"sandbox rebuilt (after {red_run})" in out.stdout + out.stderr

    wait_for("the sandbox rebuild after the red run", rebuilt, timeout=10 * 60)
    return (
        f"red run on {red[:12]} opened #{issue['number']} (merge commit and log linked); #{clean.number} that passed "
        f"now fails '{status['description']}', also with 'Fixes #{issue['number']}' but no signer Approve; wave PR "
        "unaffected; sandbox rebuilt after the red run"
    )


def s13_green_run_clears_the_freeze(run: Run) -> str:
    if run.freeze_issue is None:
        raise Fail("no freeze issue from scenario 12")
    clean = run.prs["clean"]
    makefile = base64.b64decode(repo_api("GET", f"repos/{REPO}/contents/Makefile?ref=develop")["content"]).decode()
    green = run.put_file(DEVELOP_BRANCH, "Makefile", makefile + GREEN_VERIFY)
    run.develop_heads.append(green)
    say(f"  {DEVELOP_BRANCH} at {green[:12]}: the journey passes (stands in for the merged fix)")

    def closed() -> dict[str, Any] | None:
        issue = run.builder.call("GET", f"/repos/{REPO}/issues/{run.freeze_issue}")
        return issue if issue["state"] == "closed" else None

    issue = wait_for(f"#{run.freeze_issue} to be closed", closed)
    closer = (issue.get("closed_by") or {}).get("login")
    if closer != RUNNER_BOT:
        raise Fail(f"#{run.freeze_issue} was closed by {closer}, not {RUNNER_BOT}")
    comments = run.builder.call("GET", f"/repos/{REPO}/issues/{run.freeze_issue}/comments", per_page=100)
    if not any(green[:12] in c["body"] and c["user"]["login"] == RUNNER_BOT for c in comments):
        raise Fail(f"#{run.freeze_issue} has no runner comment naming the green head {green[:12]}")
    run.wait(clean.head, "success", timeout=RULE_TIMEOUT)
    fix = run.prs.get("fix")
    if fix is not None:
        run.wait(fix.head, "success", timeout=5)
    return f"green run on {green[:12]}: {RUNNER_BOT} closed #{run.freeze_issue}; #{clean.number} back to success"


def s14_signed_fix_gets_through_the_freeze(run: Run) -> str:
    if run.freeze_issue is None:
        raise Fail("no freeze issue from scenario 12")
    number = run.freeze_issue
    doc = f"docs/verify-runner/{run.tag}-fix.md"
    pr = run.task_pr("fix", "the fix for the red develop", {doc: f"verify-runner-live {run.tag}: the fix\n"},
                     body=f"Fixes #{number}")  # fmt: skip
    run.challenger_approve(pr, "Challenger: first")
    frozen = run.wait(pr.head, "failure", "develop is red")  # the fix too, until a second challenger approves
    run.challenger_approve(pr, "Challenger: second")
    run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    still = run.latest(run.prs["clean"].head) or {}
    if "develop is red" not in (still.get("description") or ""):
        raise Fail("another task PR was let through with the fix")
    return (
        f"#{pr.number} ('Fixes #{number}') failed '{frozen['description']}' until the second Approve, then "
        f"success while #{number} stayed open; #{run.prs['clean'].number} still frozen"
    )


def s15_merge_deploys_and_verifies(run: Run) -> str:
    """After the green merge: the head runs on lab_local as the service, migrated, and verify passed against it."""
    green = run.develop_heads[-1] if run.develop_heads else None
    if green is None:
        raise Fail("no develop head from scenarios 12-13")
    inspect = subprocess.run(
        ["docker", "inspect", "-f", '{{.State.Running}} {{index .Config.Labels "mandate.head"}}', DEPLOYED],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    if inspect != ["true", green]:
        raise Fail(f"{DEPLOYED} is {inspect or 'missing'}, expected running at {green[:12]}")
    if networks(DEPLOYED) != ["lab_local"]:
        raise Fail(f"{DEPLOYED} is on {networks(DEPLOYED)}, expected lab_local")
    health = subprocess.run(
        ["docker", "exec", DEPLOYED, "curl", "-fsS", "http://localhost:8000/health"], capture_output=True, text=True
    ).stdout.strip()
    if '"status":"ok"' not in health.replace(" ", ""):
        raise Fail(f"{DEPLOYED} /health answers {health[:80]!r}")
    migrated = subprocess.run(
        ["docker", "exec", "lab-postgres", "psql", "-U", "mandate_owner", "-d", DEPLOYED_DB, "-Atc",
         "select to_regclass('public.alembic_version') is not null"],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    if migrated != "t":  # the entrypoint's migration step ran (sandbox-svc has no revisions yet)
        raise Fail(f"{DEPLOYED_DB} has no migration table ({migrated!r})")
    logs = run_logs_for(green)
    log = "\n".join(fetch_log(f"{LOG_SERVER}/runs/{name}") for name in logs)
    for needle in (f"runs {green[:12]} and answers", "make setup, make verify passed against", "/health answers ok"):
        if needle not in log:
            raise Fail(f"the develop run log for {green[:12]} lacks '{needle}'")
    purge = subprocess.run(
        ["docker", "exec", RUNNER_CONTAINER, "python", "-c",
         "from pathlib import Path; from mandate_runner.deployer import DeployerClient as C; "
         "r = C(Path('/jobs')).purge(); print(r.ok, r.message)"],
        capture_output=True, text=True, timeout=600,
    ).stdout.strip()  # fmt: skip
    kept = subprocess.run(
        ["docker", "exec", "lab-postgres", "psql", "-U", "mandate_owner", "-d", DEPLOYED_DB, "-Atc",
         "select to_regclass('public.alembic_version') is not null"],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    if not purge.startswith("True") or kept != "t":
        raise Fail(f"purge: {purge!r}; migration table afterwards: {kept!r}")
    return (
        f"{DEPLOYED} runs {green[:12]} on lab_local and answers /health; {DEPLOYED_DB} migrated; make verify passed "
        f"against it ({len(logs)} run log); purge deleted the data and kept the schema"
    )


def s10_statuses_by_runner(run: Run) -> str:
    total = 0
    for pr in run.prs.values():
        for sha in pr.heads:
            statuses = run.statuses(sha)
            if not statuses:
                raise Fail(f"#{pr.number} {sha[:12]} has no {CONTEXT} status")
            others = {s["creator"]["login"] for s in statuses} - {RUNNER_BOT}
            if others:
                raise Fail(f"#{pr.number} {sha[:12]} has {CONTEXT} statuses from {others}")
            total += len(statuses)
    return f"{total} statuses on {sum(len(p.heads) for p in run.prs.values())} heads, all by {RUNNER_BOT}"


SCENARIOS: list[tuple[int, str, Scenario]] = [
    (1, "runner up, log has no secrets", s1_runner_up),
    (
        2,
        "clean PR without review fails on Challenger: first",
        s2_clean_pr_needs_challenger,
    ),
    (
        3,
        "challenger Approve flips to success, no second run",
        s3_approve_flips_without_rerun,
    ),
    (4, "new push needs a fresh approval", s4_new_push_needs_fresh_approval),
    (5, "broken test fails on make test, stays failed", s5_broken_test_stays_failed),
    (6, "protected path (Makefile) needs the signer's Approve", s6_protected_path_needs_a_second_challenger),
    (
        7,
        "trust-critical path needs Challenger: first and second",
        s7_trust_critical_needs_two,
    ),
    (
        8,
        "develop into main needs label, ready, attested Approve",
        s8_wave_pr_needs_signoff,
    ),
    (9, "job sees no build key or token", s9_no_secrets_in_job),
    (
        11,
        "PR code in the sandbox can't reach lab_local services",
        s11_sandbox_cannot_reach_the_lab,
    ),
    (
        12,
        "a red develop freezes a task PR that would otherwise pass",
        s12_red_develop_freezes,
    ),
    (14, "while frozen, the signed fix gets through", s14_signed_fix_gets_through_the_freeze),
    (13, "a green run clears the freeze", s13_green_run_clears_the_freeze),
    (15, "a merge is deployed to the lab, migrated, and verified against", s15_merge_deploys_and_verifies),
    (
        10,
        "every status created by mandate-runner[bot]",
        s10_statuses_by_runner,
    ),  # last: covers every head
]


# --- setup and cleanup --------------------------------------------------------------------


def open_test_prs(run: Run) -> None:
    open_wave = [
        p
        for p in run.builder.call("GET", f"/repos/{REPO}/pulls", state="open", base="main", per_page=100)
        if p["head"]["ref"] == "develop"
    ]
    if open_wave:
        raise Fail(f"a develop->main PR is already open (#{open_wave[0]['number']}); close it first")
    if freeze_issues(run):
        raise Fail(f"a {FREEZE_LABEL} issue is already open on {REPO}; run with ARGS=--cleanup first")
    labels = run.builder.call("GET", f"/repos/{REPO}/labels", per_page=100)
    run.had_freeze_label = any(label["name"] == FREEZE_LABEL for label in labels)
    doc = f"docs/verify-runner/{run.tag}.md"
    run.task_pr("clean", "clean change", {doc: f"verify-runner-live {run.tag}: clean change\n"})
    run.task_pr(
        "broken",
        "broken test",
        {"tests/unit/test_verify_runner_broken.py": BROKEN_TEST},
    )
    run.task_pr("protected", "protected path (Makefile)", {"Makefile": None})
    run.task_pr(
        "critical",
        "trust-critical path",
        {"tests/unit/test_verify_runner_ledger_probe.py": LEDGER_TEST},
    )
    run.task_pr(
        "secrets",
        "secrets probe",
        {"tests/unit/test_verify_runner_secrets_probe.py": SECRETS_PROBE},
    )
    run.task_pr(
        "isolation",
        "sandbox isolation probe",
        {"tests/unit/test_verify_runner_isolation_probe.py": isolation_probe()},
    )
    head = run.develop_sha()
    run.open_pr(
        "wave",
        "develop",
        head,
        "wave close (develop into main)",
        base="main",
        draft=True,
    )


def cleanup(run: Run | None, builder: App) -> list[str]:
    """Close every test PR and delete every test branch, including leftovers from earlier runs."""
    done: list[str] = []
    for pr in builder.call("GET", f"/repos/{REPO}/pulls", state="open", per_page=100):
        if pr["title"].startswith(TITLE):
            builder.call("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", {"state": "closed"})
            done.append(f"closed #{pr['number']}")
    refs = repo_api("GET", f"repos/{REPO}/git/matching-refs/heads/{PREFIX}") or []
    for ref in refs:
        name = ref["ref"].removeprefix("refs/heads/")
        if not name.startswith(PREFIX):
            continue  # never develop or main
        repo_api("DELETE", f"repos/{REPO}/git/refs/heads/{name}")
        done.append(f"deleted branch {name}")
    for issue in builder.call("GET", f"/repos/{REPO}/issues", state="open", labels=FREEZE_LABEL, per_page=100):
        if "pull_request" not in issue and issue["user"]["login"] == RUNNER_BOT:
            builder.call("PATCH", f"/repos/{REPO}/issues/{issue['number']}", {"state": "closed"})
            done.append(f"closed freeze issue #{issue['number']}")
    if run is not None and run.created_label:
        builder.call("DELETE", f"/repos/{REPO}/labels/{WAVE_LABEL}")
        done.append(f"deleted label {WAVE_LABEL}")
    gone = subprocess.run(["docker", "rm", "-f", DEPLOYED], capture_output=True, text=True)
    if gone.returncode == 0 and gone.stdout.strip():
        done.append(f"removed {DEPLOYED} (its database {DEPLOYED_DB} is kept for the next run)")
    if run is not None and not run.had_freeze_label:
        try:
            builder.call("DELETE", f"/repos/{REPO}/labels/{FREEZE_LABEL}")
        except Fail:
            pass
        else:
            done.append(f"deleted label {FREEZE_LABEL} (created by the runner during this run)")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="only close leftover test PRs and delete their branches",
    )
    args = parser.parse_args()

    builder, challenger = App("mandate-builder"), App("mandate-challenger")
    _REPO_APP.append(builder)
    if args.cleanup:
        for line in cleanup(None, builder) or ["nothing to clean up"]:
            say(line)
        return 0

    run = Run(
        tag=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        builder=builder,
        challenger=challenger,
    )
    results: list[tuple[int, str, bool, str]] = []
    say(f"verify-runner-live {run.tag} against {REPO}")
    try:
        number, name, scenario = SCENARIOS[0]
        try:
            results.append((number, name, True, scenario(run)))
        except Fail as exc:
            results.append((number, name, False, str(exc)))
            raise Fail("runner is not ready; stopping before any PR is opened") from None
        say(f"PASS 1 {results[0][3]}")
        open_test_prs(run)
        for number, name, scenario in SCENARIOS[1:]:
            say(f"--- scenario {number}: {name}")
            try:
                detail = scenario(run)
                results.append((number, name, True, detail))
                say(f"PASS {number} {detail}")
            except Fail as exc:
                results.append((number, name, False, str(exc)))
                say(f"FAIL {number} {exc}")
            except Exception as exc:  # report and carry on with the next scenario
                results.append((number, name, False, f"{type(exc).__name__}: {exc}"))
                say(f"FAIL {number} {type(exc).__name__}: {exc}")
                traceback.print_exc()
    except Fail as exc:
        say(f"STOPPED: {exc}")
    finally:
        say("--- cleanup")
        try:
            for line in cleanup(run, builder):
                say(f"  {line}")
        except Exception as exc:
            say(f"  CLEANUP INCOMPLETE: {exc}; rerun with ARGS=--cleanup")

    say("=" * 72)
    for number, name, ok, detail in results:
        say(f"{'PASS' if ok else 'FAIL'}  {number:>2}. {name}: {detail}")
    passed = sum(ok for *_, ok, _ in results)
    say(f"{passed}/{len(SCENARIOS)} passed")
    return 0 if passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    sys.exit(main())
