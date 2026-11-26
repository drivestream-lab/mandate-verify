# mandate-verify
Central reusable verification workflow. Signs release evidence

This repo holds verification for both of Mandate's modes (PL-16, BP-2):

| Mode | What | Where |
| --- | --- | --- |
| Governed programmes | The reusable verify workflow: build the exact commit, run the protected test set, attest the evidence bundle | [.github/workflows/verify.yml](.github/workflows/verify.yml) |
| Build mode (Mandate built by its swarm) | `mandate-runner`: grades the swarm's PRs and posts `mandate/build-verified` | [services/mandate-runner](services/mandate-runner) |

## mandate-runner

Builds Mandate's swarm, wave B0, tasks B0-01 and B0-02 (Mandate engineering-ready PRD PL-16). This is plain Python, with no model. Every 30 seconds it polls open PRs on `drivestream-lab/mandate` and `drivestream-lab/mandate-control-tower`. For each PR head into `develop` or `main` it posts the commit status `mandate/build-verified`.

It runs as four containers, all from one image except the sandbox:

| Container | Does | Networks | Holds |
| --- | --- | --- | --- |
| `lab-mandate-runner` | Polls, grades, posts statuses, runs develop after each merge; the only one that calls GitHub | `lab_local`, `mandate_runner_sandbox` | The runner App's key (read-only) |
| `lab-mandate-runner-dind` | The sandbox: its own Docker engine, where PR code runs | `mandate_runner_sandbox` only (internal, no route out) | No secrets |
| `lab-mandate-runner-proxy` | The sandbox's only way out: HTTP(S) to public addresses on ports 80 and 443 | `mandate_runner_sandbox`, `mandate_runner_egress` | Nothing |
| `lab-mandate-runner-deployer` | Host Docker jobs the runner asks for: rebuild the sandbox, deploy the merged head | none | The host Docker socket; no key, no PR code |

```bash
make runner-up      # needs ~/.mandate/secrets/build/mandate-runner/{app-id,installation-id,private-key.pem}
make runner-logs
make runner-test    # unit tests, in a container
```

Code: [services/mandate-runner](services/mandate-runner). Compose: [docker-compose.runner.yml](docker-compose.runner.yml).

**What it runs.** The command set for each base branch comes from [config/mandate-runner/runner.toml](config/mandate-runner/runner.toml), never from the PR. Today it is `make setup`, `make check`, `make test` and `make journeys`, with the full journey set on every graded PR. The runner checks out the exact head commit into a fresh workspace and runs the commands in a job container inside its sandbox. The sandbox is wiped between runs: no containers, networks or volumes are kept.

**When it posts success.** The commands passed and every review rule holds on the current head:

| PR | Rule |
| --- | --- |
| Into `develop` | An Approve from `mandate-challenger[bot]` whose body has the line `Challenger: first` |
| Touches a trust-critical path (ledger, signoff check, evidence verification, merge control) | A second, separate `mandate-challenger[bot]` Approve with the line `Challenger: second`. A single review carrying both lines counts for neither |
| Touches a protected path (PL-16 BP-6, including any `[tool.*]` change in `pyproject.toml`) | An Approve from `nikd10x` |
| `develop` into `main` (wave close) | Label `wave-accepted`, PR ready (not draft), and an Approve from `nikd10x` whose body has the lines `Initiative: INIT-MANDATE-001` and `Head: <full 40-character head sha>`. No challenger review is needed |

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

**After every merge into `develop` on `mandate` (B0-02).** The runner checks the merged head out and, in a freshly rebuilt sandbox, builds its image. The deployer loads that image and runs it as `lab-mandate` on `lab_local`, then waits for `/health`. Then every journey runs (`make setup`, `make journeys`). If the head has no `Dockerfile` or no `journeys` target, the run records "nothing to deploy yet" and passes. PR code never touches the host engine: the image is built in the sandbox and handed over as a file.

The deployed service reads `~/.mandate/lab/mandate.env` (yours, outside the repos). It must set `POSTGRES_DB=mandate_lab`, its own database on `lab-postgres`, which you create once. Without that file the deploy fails, and develop goes red.

**Merge freeze.** A red develop run opens an issue on `mandate` as `mandate-runner[bot]`, labelled `develop-red`, linking the merge commit and the run log. While it is open, every PR into `develop` (on both repos) fails with "develop is red". The exception is the fix: a PR whose body says `Fixes #N` (from control-tower: `Fixes drivestream-lab/mandate#N`) and that has `nikd10x`'s Approve on its head is graded as usual. When a later develop run is green, the runner closes the issue, which lifts the freeze. Only issues the runner opened count. Wave PRs into `main` aren't frozen.

**Live check.** `make runner-up RUNNER_LOCAL=1` then `make verify-runner-live` runs 13 scenarios against the running runner on `mandate-sandbox-svc`, from [scripts/verify_runner_live.py](scripts/verify_runner_live.py). The gitignored override makes the develop run use a test branch with deploy off, and makes `0xbeefdead` the signer, so the check needs no one. The real config keeps `nikd10x`. `make runner-up` restores the real config.

**Limits.** PR code controls the sandbox's Docker engine; that's how `make journeys` brings up its own stack. The job image runs by image id, so a PR can't swap it for a later run. A PR that compromises the sandbox engine itself could still affect later runs, though. That is why it is rebuilt daily and after failures, and before every deploy build. The sandbox is a privileged container: PR code that escapes it would reach the Docker Desktop VM, and the network rules above don't hold against that.
