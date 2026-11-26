#!/usr/bin/env python3
"""Live verification of mandate-runner (B0-01) before B0-02.

Runs ten scenarios against the RUNNING runner and checks each mandate/build-verified
status it posts. The runner must be started with the local override, which points it
at drivestream-lab/mandate-sandbox-svc only, with make setup, make check, make test:

    make runner-up RUNNER_LOCAL=1
    make verify-runner-live            # or: ARGS=--cleanup to sweep leftovers only

Who does what:
  - KD's gh (`gh` on this machine) pushes the test branches, adds the wave label and
    marks the wave PR ready.
  - mandate-builder (~/.mandate/secrets/build/mandate-builder/) opens every test PR,
    so KD is never the author and can approve.
  - mandate-challenger (~/.mandate/secrets/build/mandate-challenger/) posts the
    challenger Approves.
  - KD approves by hand when the script pauses (scenarios 6 and 8).

Every test PR is closed and every test branch deleted at the end, even on failure.
The script never prints keys or tokens. It reads the runner's logs through
`docker logs` and the log server on 127.0.0.1:8090.
"""

from __future__ import annotations

import argparse
import base64
import json
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
KD = "nikd10x"
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
KD_TIMEOUT = 60 * 60  # waiting for KD's hand
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
            token = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self._app_id}, self._key, algorithm="RS256")
            resp = self._http.post(
                f"{API}/app/installations/{self._installation}/access_tokens",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
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


def kd(*args: str) -> str:
    """KD's gh. Output is returned, not printed."""
    res = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise Fail(f"gh {' '.join(args[:3])}: {res.stderr.strip()[:300]}")
    return res.stdout


def kd_api(method: str, path: str, **fields: str) -> Any:
    args = ["api", "-X", method, path]
    for key, value in fields.items():
        args += ["-f", f"{key}={value}"]
    out = kd(*args)
    return json.loads(out) if out.strip() else None


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

    # --- branches and PRs ---------------------------------------------------------

    def develop_sha(self) -> str:
        return kd_api("GET", f"repos/{REPO}/git/ref/heads/develop")["object"]["sha"]

    def push_branch(self, key: str, files: dict[str, str | None]) -> tuple[str, str]:
        """Branch from develop with KD's gh; `None` content means append to the existing file."""
        branch = f"{PREFIX}{self.tag}-{key}"
        kd_api("POST", f"repos/{REPO}/git/refs", ref=f"refs/heads/{branch}", sha=self.develop_sha())
        head = ""
        for path, content in files.items():
            head = self.put_file(branch, path, content)
        return branch, head

    def put_file(self, branch: str, path: str, content: str | None) -> str:
        fields = {"message": f"verify-runner-live {self.tag}: {path}", "branch": branch}
        existing = subprocess.run(
            ["gh", "api", f"repos/{REPO}/contents/{path}?ref={branch}"], capture_output=True, text=True, timeout=60
        )
        if existing.returncode == 0:
            current = json.loads(existing.stdout)
            fields["sha"] = current["sha"]
            if content is None:
                content = base64.b64decode(current["content"]).decode() + f"\n# verify-runner-live {self.tag}\n"
        elif content is None:
            raise Fail(f"{path} does not exist on {branch}")
        fields["content"] = base64.b64encode(content.encode()).decode()
        return kd_api("PUT", f"repos/{REPO}/contents/{path}", **fields)["commit"]["sha"]

    def open_pr(
        self, key: str, branch: str, head: str, title: str, *, base: str = "develop", draft: bool = False
    ) -> TestPR:
        pr = self.builder.call(
            "POST",
            f"/repos/{REPO}/pulls",
            {
                "title": f"{TITLE} {self.tag} {title}",
                "head": branch,
                "base": base,
                "draft": draft,
                "body": "Opened by scripts/verify_runner_live.py (mandate-verify). Closed automatically; do not merge.",
            },
        )
        if pr["head"]["sha"] != head:
            raise Fail(f"PR #{pr['number']} head is {pr['head']['sha'][:12]}, expected {head[:12]}")
        test_pr = TestPR(key=key, number=pr["number"], branch=branch, head=head, heads=[head])
        self.prs[key] = test_pr
        say(f"opened #{test_pr.number} ({key}) by mandate-builder: https://github.com/{REPO}/pull/{test_pr.number}")
        return test_pr

    def task_pr(self, key: str, title: str, files: dict[str, str | None]) -> TestPR:
        branch, head = self.push_branch(key, files)
        return self.open_pr(key, branch, head, title)

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

    def wait_for_kd(self, pr: TestPR, body: str | None) -> None:
        cmd = f"gh pr review {pr.number} -R {REPO} --approve"
        if body is not None:
            cmd += " --body $'" + body.replace("\n", "\\n") + "'"
        say("=" * 72)
        say(f"PAUSED FOR KD: approve #{pr.number} at head {pr.head}")
        say(f"  https://github.com/{REPO}/pull/{pr.number}")
        say(f"  or run:  {cmd}")
        say("=" * 72)
        deadline = time.time() + KD_TIMEOUT
        while time.time() < deadline:
            reviews = self.builder.call("GET", f"/repos/{REPO}/pulls/{pr.number}/reviews", per_page=100)
            if any(
                r["user"]["login"] == KD and r["state"] == "APPROVED" and r["commit_id"] == pr.head for r in reviews
            ):
                say(f"KD approved #{pr.number}; resuming")
                return
            time.sleep(15)
        raise Fail(f"no Approve from {KD} on #{pr.number} within {KD_TIMEOUT // 60} minutes")

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


# --- scenarios --------------------------------------------------------------------------

Scenario = Callable[[Run], str]


def s1_runner_up(run: Run) -> str:
    health = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Health.Status}}", RUNNER_CONTAINER], capture_output=True, text=True
    ).stdout.strip()
    if health != "healthy":
        raise Fail(f"{RUNNER_CONTAINER} is '{health or 'missing'}', not healthy")
    if httpx.get(f"{LOG_SERVER}/healthz", timeout=10).text.strip() != "ok":
        raise Fail("log server does not answer")
    config = subprocess.run(
        ["docker", "exec", RUNNER_CONTAINER, "cat", "/etc/mandate-runner/runner.toml"],
        capture_output=True,
        text=True,
    ).stdout
    names = re.findall(r'^name = "([^"]+)"', config, re.M)
    if names != [REPO]:
        raise Fail(f"runner grades {names}; start it with `make runner-up RUNNER_LOCAL=1`")
    if '"make setup", "make check", "make test"' not in config:
        raise Fail("runner's command set is not make setup, make check, make test")
    logs = subprocess.run(["docker", "logs", RUNNER_CONTAINER], capture_output=True, text=True).stdout
    logs += subprocess.run(["docker", "logs", RUNNER_CONTAINER], capture_output=True, text=True).stderr
    found = leaks(logs, key_fragments((SECRETS / "mandate-runner" / "private-key.pem").read_bytes()))
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
    new = run.push(pr, f"docs/verify-runner/{run.tag}-second.md", f"verify-runner-live {run.tag}: second push\n")
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


def s6_protected_path_needs_kd(run: Run) -> str:
    pr = run.prs["protected"]
    run.challenger_approve(pr, "Challenger: first")
    status = run.wait(pr.head, "failure", "Makefile is protected")
    run.wait_for_kd(pr, None)
    run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    return f"#{pr.number}: '{status['description']}', success after KD's Approve"


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
    labels = kd("label", "list", "-R", REPO, "--limit", "200", "--json", "name", "-q", ".[].name").split()
    if WAVE_LABEL not in labels:
        kd("label", "create", WAVE_LABEL, "-R", REPO, "--description", "Wave close signed by KD", "--color", "0E8A16")
        run.created_label = True
        say(f"created label {WAVE_LABEL} on {REPO} (removed at the end)")
    kd("pr", "edit", str(pr.number), "-R", REPO, "--add-label", WAVE_LABEL)
    steps.append(run.wait(pr.head, "failure", "draft", timeout=RULE_TIMEOUT)["description"])
    kd("pr", "ready", str(pr.number), "-R", REPO)
    steps.append(run.wait(pr.head, "failure", f"Approve from {KD}", timeout=RULE_TIMEOUT)["description"])
    run.wait_for_kd(pr, f"Wave accepted (verify-runner-live).\n\nInitiative: {INITIATIVE}\nHead: {pr.head}")
    run.wait(pr.head, "success", timeout=RULE_TIMEOUT)
    return f"#{pr.number}: " + " -> ".join(f"'{s}'" for s in steps) + " -> success after KD's attested Approve"


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
    (2, "clean PR without review fails on Challenger: first", s2_clean_pr_needs_challenger),
    (3, "challenger Approve flips to success, no second run", s3_approve_flips_without_rerun),
    (4, "new push needs a fresh approval", s4_new_push_needs_fresh_approval),
    (5, "broken test fails on make test, stays failed", s5_broken_test_stays_failed),
    (6, "protected path (Makefile) needs KD's Approve", s6_protected_path_needs_kd),
    (7, "trust-critical path needs Challenger: first and second", s7_trust_critical_needs_two),
    (8, "develop into main needs label, ready, attested Approve", s8_wave_pr_needs_signoff),
    (9, "job sees no build key or token", s9_no_secrets_in_job),
    (10, "every status created by mandate-runner[bot]", s10_statuses_by_runner),
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
    doc = f"docs/verify-runner/{run.tag}.md"
    run.task_pr("clean", "clean change", {doc: f"verify-runner-live {run.tag}: clean change\n"})
    run.task_pr("broken", "broken test", {"tests/unit/test_verify_runner_broken.py": BROKEN_TEST})
    run.task_pr("protected", "protected path (Makefile)", {"Makefile": None})
    run.task_pr("critical", "trust-critical path", {"tests/unit/test_verify_runner_ledger_probe.py": LEDGER_TEST})
    run.task_pr("secrets", "secrets probe", {"tests/unit/test_verify_runner_secrets_probe.py": SECRETS_PROBE})
    head = run.develop_sha()
    run.open_pr("wave", "develop", head, "wave close (develop into main)", base="main", draft=True)


def cleanup(run: Run | None, builder: App) -> list[str]:
    """Close every test PR and delete every test branch, including leftovers from earlier runs."""
    done: list[str] = []
    for pr in builder.call("GET", f"/repos/{REPO}/pulls", state="open", per_page=100):
        if pr["title"].startswith(TITLE):
            builder.call("PATCH", f"/repos/{REPO}/pulls/{pr['number']}", {"state": "closed"})
            done.append(f"closed #{pr['number']}")
    refs = kd_api("GET", f"repos/{REPO}/git/matching-refs/heads/{PREFIX}") or []
    for ref in refs:
        name = ref["ref"].removeprefix("refs/heads/")
        if not name.startswith(PREFIX):
            continue  # never develop or main
        kd_api("DELETE", f"repos/{REPO}/git/refs/heads/{name}")
        done.append(f"deleted branch {name}")
    if run is not None and run.created_label:
        kd("label", "delete", WAVE_LABEL, "-R", REPO, "--yes")
        done.append(f"deleted label {WAVE_LABEL}")
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cleanup", action="store_true", help="only close leftover test PRs and delete their branches")
    args = parser.parse_args()

    if kd("api", "user", "--jq", ".login").strip() != KD:
        raise SystemExit(f"gh is not logged in as {KD}")
    builder, challenger = App("mandate-builder"), App("mandate-challenger")
    if args.cleanup:
        for line in cleanup(None, builder) or ["nothing to clean up"]:
            say(line)
        return 0

    run = Run(tag=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"), builder=builder, challenger=challenger)
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
