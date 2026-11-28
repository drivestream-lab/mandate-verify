# mandate-verify
Central reusable verification workflow. Signs release evidence

This repo holds verification for both of Mandate's modes (PL-16, BP-2):

| Mode | What | Where |
| --- | --- | --- |
| Governed programmes | The reusable verify workflow: build the exact commit, run the protected test set, attest the evidence bundle | [.github/workflows/verify.yml](.github/workflows/verify.yml) |
| Build mode (Mandate built by its swarm) | `mandate-runner`: grades the swarm's PRs and posts `mandate/build-verified` | [services/mandate-runner](services/mandate-runner) |

## The evidence bundle (governed programmes)

`verify.yml` has two jobs. `test` has `contents: read` only. It never runs a Makefile or any script of the target repo. The workflow itself builds the environment, with every interpreter call run as `python -I` from `$RUNNER_TEMP` by absolute path, so the checkout is never on an installer's import path: Python 3.12.8 (`setup-python`), a venv outside the checkout, pytest 8.3.5 (and pytest-asyncio 0.26.0 when requested) with hashes into a separate `tools` directory, then the commit's `poetry.lock` converted to requirements by a trusted script and installed with `pip --isolated --index-url https://pypi.org/simple --require-hashes --only-binary=:all: --no-deps`. The converter accepts only a strict name (`[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?`), a PEP 440 version, `sha256:<64 hex>` hashes and markers that the pinned `packaging` parses and that hold no newline, and it rejects any package with a `source` entry. Every interpreter start after the lock install is the runner's own Python (`$PYBIN`), started `-I -S -B` from an empty environment by a fixed bootstrap written by the workflow; nothing under the venv's `bin` is ever executed after the lock install, and the venv's site-packages path is derived, not queried. Because site processing is off, no `.pth` in site-packages ever loads (so setuptools' `distutils-precedence.pth` is harmless). The bootstrap builds `sys.path` as the standard library, then `tools`, then site-packages, then the checkout, and refuses to start if any standard-library module name (except the unused `test` package) can be found in `tools`, site-packages or the checkout, for both the origin check and pytest. The step fails if any `sitecustomize*` or `usercustomize*` exists in site-packages, `tools` or the checkout root, or if a `*.pth` exists in the checkout root. The test job fails if the commit tracks any `*.pyc`, `*.pyo`, `*.so`, `*.pyd` or a `__pycache__/` path (no allowlist), every interpreter call runs with `-B`, and every pytest or origin-check start uses `-X pycache_prefix=<a fresh directory from mktemp, created just before that call and never reused>` so no bytecode beside sources is read. A sha256 of the whole `tools` tree is taken right after its install and re-verified before collection, before the run and after the run; it is recorded as `tools_digest`, and the install must equal the constant `VERIFY_TOOLS_DIGEST` in the workflow, so the pinned tool set is itself pinned; the attest job compares the reported `tools_digest` with the same constant and refuses to attest on a mismatch. The tools are installed with `--no-compile`, and the step fails, naming the path, if the tools hold any `__pycache__` or `*.pyc` after the install, before collection, before the run or after the run. Format of the tree hash: files below `tools` walked in sorted order; one line `file <relative path> <sha256 of the content>` per file (or `link <relative path> <target>` for a symlink); the digest is the sha256 hex of those lines. The constant is valid for the pinned runner image and path (`ubuntu-24.04`, `/home/runner/work/_temp`), because the installed console scripts name the interpreter. A trusted plugin in `tools` (`verify_guard`, loaded with `-p`) fails the run if any loaded module's file is outside the standard library, `tools`, site-packages or the checkout, or if a standard-library module name is loaded from anywhere but the standard library. pytest runs as `pytest.main` from that bootstrap with a trusted config (`-c`, `-o addopts=`, `--rootdir`, `--confcutdir`, `--import-mode=importlib`, no cache provider) and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Before the run, `pytest`, `_pytest` and every plugin are resolved and must lie inside `tools`, never the checkout, and `sitecustomize` and `usercustomize` must not be importable. The process baseline is taken before the first venv or pip call; after collection, after the run and again immediately before the upload, any process started since the baseline that is still alive fails the job. This survivor check is best effort: it cannot catch code that finishes its work before the check runs, and hosted runners give code passwordless `sudo`. The environment manifest is written by the runner's own Python from the installed distributions' metadata; the venv's python is not started for it, and `runner_image` is read from the runner's `ImageOS` and `ImageVersion`.

`attest` is the only job with `id-token` and `attestations: write`, and it runs no code from the target repo. It recomputes the digests from git objects, derives the result from the raw junit and collected ids, tars only the files it wrote, attests `evidence.tgz`, uploads it as `evidence-<commit>-<run_attempt>`, and fails the workflow unless the result is `pass`.

Inputs: `commit` (`^[0-9a-f]{40}$`), `test-set` (space-separated plain relative tracked paths, passed after `--`; empty, leading `-`, `=`, `..`, absolute paths and globs are rejected), `quarantine` (node ids, each with `::`, each exactly one collected test and not contained in another; the count is recorded) and `plugins` (allowlist: `pytest_asyncio` only). `poetry.lock` and `pyproject.toml` must be tracked.

Bundle files (all in `evidence.tgz`, each hashed in `binding.json`): `binding.json`, `report.json`, `environment.json`, `junit.xml`, `collected.txt`, `digest-inputs.txt`.

`binding.json` carries: `repository` (the caller), `commit`, `caller_sha`, `workflow_ref`, `event_name`, `run_id`, `run_attempt`, `test_set`, `test_set_digest`, `dependencies_digest`, `tools_digest`, `collected_digest`, `quarantine`, `plugins`, `python`, `pytest`, `pytest_asyncio`, `worktree_clean_advisory`, `result`, `artifacts` (sha256 per file).

**Result.** `pass` only when pytest exit and collection exit are 0, there is at least one `<testcase>`, no testcase has a `failure` or `error`, none is skipped (a skipped test is not a pass; quarantined tests are deselected and listed in the quarantine), the testsuite attributes are non-negative integers that equal what the testcases show, and the multiset of junit (classname, name) equals the collected ids minus the quarantine. Counts come from the `<testcase>` elements.

**Test-set digest format.** Computed from git objects at `commit`, never the working tree. Inputs: every tracked blob under a `test-set` path; the `conftest.py` at the root and in every directory above a `test-set` path (a tracked `conftest.py` in the test-set directories is already included); and one `plugin:<name>` line per requested plugin. File lines are `<sha256 hex of the blob content>`, two spaces, `<path>`, newline, sorted by path bytes. Plugin lines follow, sorted, as `plugin:<name>` plus newline. Only modes 100644 and 100755 are allowed. The digest is the sha256 hex of that exact text, which is `digest-inputs.txt`. Project configuration (`pyproject.toml`, `pytest.ini` and so on) is not in it: the workflow's own config is used. Test helpers outside the `test-set` paths are not covered, so declare the whole test root as the test set.

**`dependencies_digest`.** sha256 hex of the text `<sha256 of poetry.lock blob>  poetry.lock\n<sha256 of pyproject.toml blob>  pyproject.toml\n`, from git objects. A dependency bump changes it, not the test-set digest.

**`collected_digest`.** sha256 hex of the sorted collected node ids, each followed by a newline, so Mandate can compare the collected set with the one fixed at sign-off (minus the quarantine).

**`worktree_clean_advisory`** is advisory: it comes from the test job, where the tests ran, so Mandate does not gate on it.

**Environment manifest.** Runner image, OS, Python, every installed package. Service versions and seeded data are deferred until a target needs services (decision pending with KD).

**Accepted risk.** The test code and the application code it imports run on the test runner with the raw outputs in reach, so they can misreport: patch pytest in process, rewrite junit or the exit code, or start a process that does. The workflow does not run the target's build scripts, resolves pytest outside the checkout and fails on surviving processes, but it cannot make code that runs on the runner honest. The backstops are that such code is in the PR diff, which challengers read, and that the protected test paths need a second challenger. Dependency changes (`poetry.lock`, and the imports that use them) are part of what challengers must read: a locked package's import-time code runs on the runner. It cannot forge a signature: the test job holds no OIDC token and the attest job never runs repo code.

## mandate-runner

Builds Mandate's swarm, wave B0, tasks B0-01 and B0-02 (Mandate engineering-ready PRD PL-16). This is plain Python, with no model. Every 30 seconds it polls open PRs on `drivestream-lab/mandate` and `drivestream-lab/mandate-control-tower`. For each PR head into `develop` or `main` it posts the commit status `mandate/build-verified`.

It runs as four containers, all from one image except the sandbox:

| Container | Does | Networks | Holds |
| --- | --- | --- | --- |
| `lab-mandate-runner` | Polls, grades, posts statuses, runs develop after each merge; the only one that calls GitHub | `lab_local`, `mandate_runner_sandbox` | The runner App's key (read-only) |
| `lab-mandate-runner-dind` | The sandbox: its own Docker engine, where PR code runs | `mandate_runner_sandbox` only (internal, no route out) | No secrets |
| `lab-mandate-runner-proxy` | The sandbox's only way out: HTTP(S) to public addresses on ports 80 and 443 | `mandate_runner_sandbox`, `mandate_runner_egress` | Nothing |
| `lab-mandate-runner-deployer` | Host Docker jobs the runner asks for: rebuild the sandbox, deploy the merged head | none | The host Docker socket; no key, no PR code |

Like every repo, it has five targets:

```bash
make setup     # .venv-verify: the live check's packages and ruff
make check     # format check and lint
make test      # the runner's unit tests, in a container
make deploy    # build the runner from this branch and run it in Docker
               # (needs ~/.mandate/secrets/build/mandate-runner/{app-id,installation-id,private-key.pem})
make verify    # every journey against the deployed runner (see "Live check")
```

Operating it: `make runner-logs`, `make runner-ps`, `make runner-down`, `make runner-reset-sandbox`.

Code: [services/mandate-runner](services/mandate-runner). Compose: [docker-compose.runner.yml](docker-compose.runner.yml).

**What it runs.** The command set for each base branch comes from [config/mandate-runner/runner.toml](config/mandate-runner/runner.toml), never from the PR. It is the five targets every repo has: `make setup`, `make check`, `make test`, `make deploy` and `make verify` (every journey against what deploy started). On a PR they run in the sandbox, against the sandbox's own engine. The swarm's verifier judges verify's output, along with the deployed app's tables and logs. The runner checks out the exact head commit into a fresh workspace and runs the commands in a job container inside its sandbox. The sandbox is wiped between runs: no containers, networks or volumes are kept.

**When it posts success.** The commands passed and every review rule holds on the current head. Every rule is met by GitHub Apps; no person's review counts (KD, 27 Nov 2026):

| PR | Rule |
| --- | --- |
| Into `develop` | An Approve from `mandate-challenger[bot]` whose body has the line `Challenger: first` |
| Touches a trust-critical path (ledger, signoff check, evidence verification, merge control) | A second, separate `mandate-challenger[bot]` Approve with the line `Challenger: second`. A single review carrying both lines counts for neither |
| Touches a protected path (PL-16 BP-6, including any `[tool.*]` change in `pyproject.toml`) | A second, separate `mandate-challenger[bot]` Approve with the line `Challenger: second` |
| `develop` into `main` (wave close) | Label `wave-accepted`, PR ready (not draft), and a signoff from the signer App `mandate-challenger[bot]`: an Approve whose body has the line `Signoff: wave` (and no challenger marker) plus `Initiative: INIT-MANDATE-001` and `Head: <full 40-character head sha>`. The swarm posts it after its verifier checked the deployed head's evidence |

Only reviews on the current head count. For each identity, and for each challenger marker, the latest Approve, Request changes or dismissal decides. Comment-only reviews never count. Path patterns live in the config.

**Grading once.** Commands run once per head and command set. The result is kept in the `mandate_runner_data` volume, so a restart doesn't run them again. While they run, the status is `pending`. A head whose commands failed stays failed until a new push. Review rules are re-checked on every poll, and a new status is posted only when the state, description or log link changes. Runner errors (sandbox down, fetch failed) are not cached and are retried.

**When it fails.** The status is `failure`, and the description names the failing command or rule. The status link opens that run's log. Logs are kept in the `mandate_runner_data` volume and served read-only on `127.0.0.1:8090`.

**Identity and secrets.**

- It authenticates as the GitHub App `mandate-runner` (App ID 5860839). At startup it refuses to run if the mounted key belongs to a different App, or if the slug `mandate-challenger` doesn't resolve to App ID 5860827.
- `~/.mandate/secrets/build/mandate-runner/` is mounted read-only into `lab-mandate-runner` only. Only the runner calls GitHub.
- The token reaches git only through one process's environment. It is never written to `.git/config`. Every log line and run log is redacted.
- PR code runs only in the sandbox, which has no build secrets and no GitHub credentials. The runner drives the sandbox over TLS, using a client certificate that only the runner holds.

**Test secrets for journeys (off by default).** `MANDATE_RUNNER_TEST_SECRETS=1`, in the environment or in a gitignored `.env`, mounts `~/.mandate/secrets/test/` read-only into the sandbox, where job containers see it at `/secrets/test`. See [docker-compose.runner-test-secrets.yml](docker-compose.runner-test-secrets.yml). `~/.mandate/secrets/build/` is never mounted there.

**The sandbox can't reach the lab.** PR code can't connect to the lab's Postgres, Redis, LiteLLM or Paperclip: not by their `lab_local` addresses, and not through Docker Desktop's host address (`192.168.65.254`, where the lab publishes its ports). The sandbox is on an internal network with no route out. Image pulls, apt, pip and poetry go through the proxy, which dials only public addresses: a name that resolves to a private, loopback or host address is refused. The rule lives in the proxy, outside the sandbox, so PR code that is root in the sandbox can't change it. Journey stacks must add their own service names to `NO_PROXY`.

**A fresh sandbox.** The deployer rebuilds the sandbox from scratch with `make runner-reset-sandbox` once a day, by GitHub's clock (Docker Desktop's clock can lag after sleep), and after any run that did not pass. The runner asks over the `mandate_runner_jobs` volume and waits.

**After every merge into `develop` on `mandate` (B0-02).**
1. The runner checks the merged head out and builds its image in a freshly rebuilt sandbox.
2. The deployer loads that image and runs it as `lab-mandate` on `lab_local`, with `~/.mandate/lab/mandate.env` (non-secret settings only) and the database password files `~/.mandate/lab/secrets/app-password` and `owner-password` mounted read-only at `/run/mandate` (ADR-019). Each deploy installs them into the volume `lab-mandate-secrets` with enforced modes (owner 0600 root, app 0644), because a Docker Desktop bind mount lets any user read a file whatever its mode. It starts the container as root, so the entrypoint can read the owner file for the migration and then drop to the app user, which cannot. Without both files the deploy fails. Its entrypoint runs the migrations. Then it waits for `/health`.

   **Telemetry.** Before the service, the deployer starts an OpenTelemetry collector, `lab-mandate-otel-collector` (`otel/opentelemetry-collector:0.116.1`, pinned like Mandate's own deploy compose), on `lab_local`, with [services/mandate-runner/collector/config.yaml](services/mandate-runner/collector/config.yaml) mounted read-only from this checkout: an OTLP http receiver on 4318 and a debug exporter, so what the service sends shows in the collector's logs. The service gets `OTEL_EXPORTER_OTLP_ENDPOINT=http://lab-mandate-otel-collector:4318` from the deployer, not from `mandate.env`. A collector that fails to start fails the deploy, and each deploy replaces both containers.
3. The deployer runs `make setup` and `make verify` from the merged head, in a job container on `lab_local`, against the deployed service (`MANDATE_SERVICE_URL`), with the same `.env`. The container gets no Docker engine.

If the head has no `Dockerfile` or no `verify` target, the run records "nothing to deploy yet" and passes. The repo's own `make deploy` is never run with the host engine: what a Makefile runs there could read the runner's key.

The database is the one dev-infra-lab provisions, `mandate` (roles `mandate_owner` and `mandate_app`), and the env file must name it. It is kept across waves: each deploy migrates it. When waves aren't compatible, the deployer's `purge` job deletes the data and keeps the schema, the migration history and the decision ledger (`ledger` schema, append-only by ADR-020), then the next deploy migrates and verify runs again. Without the env file the deploy fails, and develop goes red.

**Merge freeze.** A red develop run opens an issue on `mandate` as `mandate-runner[bot]`, labelled `develop-red`, linking the merge commit and the run log. While it is open, every PR into `develop` (on both repos) fails with "develop is red". The exception is the fix: a PR whose body says `Fixes #N` (from control-tower: `Fixes drivestream-lab/mandate#N`) and that has a second, separate challenger Approve (`Challenger: second`) on its head is graded as usual. When a later develop run is green, the runner closes the issue, which lifts the freeze. Only issues the runner opened count. Wave PRs into `main` aren't frozen.

**Live check.** `make deploy RUNNER_LOCAL=1` then `make verify` runs every journey of the runner against the running runner, on `mandate-sandbox-svc`, from [scripts/verify_runner_live.py](scripts/verify_runner_live.py). The gitignored override does two things:
- makes the develop run use a test branch;
- deploys `mandate-sandbox-svc` as `lab-mandate-verify`, with `~/.mandate/lab/mandate-verify.env` and its reused database `mandate_verify_lab`;

Both configs name the same signer App. `make deploy` restores the real config.

**Limits.** PR code controls the sandbox's Docker engine; that's how `make journeys` brings up its own stack. The job image runs by image id, so a PR can't swap it for a later run. A PR that compromises the sandbox engine itself could still affect later runs, though. That is why it is rebuilt daily and after failures, and before every deploy build. The sandbox is a privileged container: PR code that escapes it would reach the Docker Desktop VM, and the network rules above don't hold against that.
