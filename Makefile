# mandate-verify: Mandate's verification trust anchor.
#   .github/workflows/verify.yml  reusable verify workflow for governed programmes
#   mandate-runner                build-mode grader (PL-16, B0-01): posts mandate/build-verified
# See `make help`.

.PHONY: help setup check test deploy verify runner-down runner-logs runner-ps runner-reset-sandbox

ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
# The deployer runs `make runner-reset-sandbox` against the host engine, so it needs this
# checkout at its host path.
export MANDATE_VERIFY_DIR := $(patsubst %/,%,$(ROOT))
RUNNER_SERVICES := mandate-runner-proxy mandate-runner-dind mandate-runner-deployer mandate-runner
LAB_NETWORK ?= lab_local

# Optional: MANDATE_RUNNER_TEST_SECRETS=1 in the environment or in a gitignored .env
# mounts ~/.mandate/secrets/test/ read-only into the runner's sandbox. Default off.
-include $(ROOT).env
MANDATE_RUNNER_TEST_SECRETS ?= 0

RUNNER_COMPOSE := docker compose --project-directory $(ROOT) -f $(ROOT)docker-compose.runner.yml
ifeq ($(strip $(MANDATE_RUNNER_TEST_SECRETS)),1)
RUNNER_COMPOSE += -f $(ROOT)docker-compose.runner-test-secrets.yml
endif
# Optional local override (gitignored): RUNNER_LOCAL=1 adds docker-compose.runner.local.yml,
# e.g. to point the running runner at a sandbox repo for scripts/verify_runner_live.py.
RUNNER_LOCAL ?= 0
ifeq ($(strip $(RUNNER_LOCAL)),1)
ifeq ($(wildcard $(ROOT)docker-compose.runner.local.yml),)
$(error RUNNER_LOCAL=1 but docker-compose.runner.local.yml is missing)
endif
RUNNER_COMPOSE += -f $(ROOT)docker-compose.runner.local.yml
endif
RUNNER_COMPOSE += --profile runner

help:
	@echo "mandate-verify, like every repo, has five targets:"
	@echo "  make setup    Set up the project: .venv-verify with the live check's packages and ruff"
	@echo "  make check    Format check and lint (ruff) of the runner and scripts"
	@echo "  make test     The runner's unit tests (in a container)"
	@echo "  make deploy   Build the runner from this branch and run it in Docker: runner, sandbox,"
	@echo "                egress proxy, deployer (RUNNER_LOCAL=1 adds the gitignored live-check override)"
	@echo "  make verify   Every journey against the deployed runner: scripts/verify_runner_live.py on"
	@echo "                mandate-sandbox-svc (needs make deploy RUNNER_LOCAL=1; opens and closes test PRs)"
	@echo ""
	@echo "Operating the deployed runner:"
	@echo "  make runner-down           Stop it all (KEEP volumes)"
	@echo "  make runner-logs           Follow the runner's logs (run logs: http://localhost:8090/runs/<id>.log)"
	@echo "  make runner-ps             Status"
	@echo "  make runner-reset-sandbox  Recreate the sandbox from scratch (the deployer also does it daily"
	@echo "                             and after a failed run)"
	@echo ""
	@echo "Test secrets for journeys: MANDATE_RUNNER_TEST_SECRETS=1 (now: $(MANDATE_RUNNER_TEST_SECRETS))"

VERIFY_VENV := $(ROOT).venv-verify
RUFF := $(VERIFY_VENV)/bin/ruff

setup:
	@test -x $(VERIFY_VENV)/bin/python || python3.12 -m venv $(VERIFY_VENV)
	@$(VERIFY_VENV)/bin/pip -q install "httpx==0.28.1" "PyJWT[crypto]==2.15.1" "ruff==0.15.0"
	@echo "setup done: $(VERIFY_VENV)"

check:
	@test -x $(RUFF) || { echo "run make setup first" >&2; exit 1; }
	@cd $(ROOT)services/mandate-runner && $(RUFF) format --check src tests && $(RUFF) check src tests
	@$(RUFF) format --check --line-length 120 $(ROOT)scripts && $(RUFF) check --line-length 120 $(ROOT)scripts

test:
	@docker build -q --target test --build-context config=$(ROOT)config/mandate-runner -t drivestream-lab/mandate-runner:test $(ROOT)services/mandate-runner >/dev/null
	@docker run --rm drivestream-lab/mandate-runner:test

deploy:
	@test -r "$$HOME/.mandate/secrets/build/mandate-runner/private-key.pem" || { echo "ERROR: ~/.mandate/secrets/build/mandate-runner/ is missing its key" >&2; exit 1; }
	@docker network inspect $(LAB_NETWORK) >/dev/null 2>&1 || { echo "Creating network $(LAB_NETWORK) (normally made by dev-infra-lab's make infra-up)..."; docker network create $(LAB_NETWORK) >/dev/null; }
	@mkdir -p "$$HOME/.mandate/lab"
	@$(RUNNER_COMPOSE) up -d --build --wait --wait-timeout 300 $(RUNNER_SERVICES)
	@# The config is read at start; restart so a changed runner.toml takes effect.
	@$(RUNNER_COMPOSE) restart mandate-runner-deployer mandate-runner >/dev/null
	@$(RUNNER_COMPOSE) up -d --wait --wait-timeout 300 mandate-runner >/dev/null
	@echo "mandate-runner deployed. Run logs: http://localhost:8090/runs/<run-id>.log"

verify:
	@test -x $(VERIFY_VENV)/bin/python || { echo "run make setup first" >&2; exit 1; }
	@$(VERIFY_VENV)/bin/python -u $(ROOT)scripts/verify_runner_live.py $(ARGS)

runner-down:
	@$(RUNNER_COMPOSE) stop $(RUNNER_SERVICES)

runner-logs:
	@$(RUNNER_COMPOSE) logs -f mandate-runner

runner-ps:
	@$(RUNNER_COMPOSE) ps -a

runner-reset-sandbox:
	@$(RUNNER_COMPOSE) rm -sf mandate-runner-dind
	@docker volume rm -f mandate_runner_dind_data >/dev/null
	@$(RUNNER_COMPOSE) up -d --wait mandate-runner-dind
