# mandate-verify
Central reusable verification workflow. Signs release evidence

This repo holds verification for both of Mandate's modes (PL-16, BP-2):

| Mode | What | Where |
| --- | --- | --- |
| Governed programmes | The reusable verify workflow: build the exact commit, run the protected test set, attest the evidence bundle | [.github/workflows/verify.yml](.github/workflows/verify.yml) |
| Build mode (Mandate built by its swarm) | `mandate-runner`: grades the swarm's PRs and posts `mandate/build-verified` | [services/mandate-runner](services/mandate-runner) |

## mandate-runner

Builds Mandate's swarm, wave B0, task B0-01 (Mandate engineering-ready PRD PL-16). This is plain Python, with no model. Every 30 seconds it polls open PRs on `drivestream-lab/mandate` and `drivestream-lab/mandate-control-tower`. For each PR head into `develop` or `main` it posts the commit status `mandate/build-verified`.

It runs as two containers, `lab-mandate-runner` and its sandbox `lab-mandate-runner-dind`. They join the lab's existing `lab_local` network (from `dev-infra-lab`) and use none of the lab's services.

```bash
make runner-up      # needs ~/.mandate/secrets/build/mandate-runner/{app-id,installation-id,private-key.pem}
make runner-logs
make runner-test    # unit tests, in a container
```

Code: [services/mandate-runner](services/mandate-runner). Compose: [docker-compose.runner.yml](docker-compose.runner.yml).

**What it runs.** The command set for each base branch comes from [config/mandate-runner/runner.toml](config/mandate-runner/runner.toml), never from the PR. Today it is `make check`, `make test` and `make journeys`, with the full journey set on every graded PR. The runner checks out the exact head commit into a fresh workspace and runs the commands in a job container inside its sandbox. The sandbox is wiped between runs: no containers, networks or volumes are kept.

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

**Limits.** PR code controls the sandbox's Docker engine; that's how `make journeys` brings up its own stack. The job image runs by image id, so a PR can't swap it for a later run. A PR that compromises the sandbox engine itself could still affect later runs, though. `make runner-reset-sandbox` rebuilds the sandbox from scratch. The sandbox sits on `lab_local`, so PR code can reach the lab services.
