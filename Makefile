# mandate-verify: Mandate's verification trust anchor.
#   .github/workflows/verify.yml  reusable verify workflow for governed programmes
#   mandate-runner                build-mode grader (PL-16, B0-01): posts mandate/build-verified
# See `make help`.

.PHONY: help runner-up runner-down runner-logs runner-ps runner-test runner-reset-sandbox verify-runner-live

ROOT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
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
	@echo "mandate-runner (grades Mandate PRs, posts mandate/build-verified):"
	@echo "  make runner-up             Start the runner and its sandbox on $(LAB_NETWORK)"
	@echo "  make runner-down           Stop the runner and its sandbox (KEEP volumes)"
	@echo "  make runner-logs           Follow the runner's logs (run logs: http://localhost:8090/runs/<id>.log)"
	@echo "  make runner-ps             Status"
	@echo "  make runner-test           Unit tests for the runner (in a container)"
	@echo "  make runner-reset-sandbox  Recreate the sandbox from scratch (drops its images and state)"
	@echo ""
	@echo "Test secrets for journeys: MANDATE_RUNNER_TEST_SECRETS=1 (now: $(MANDATE_RUNNER_TEST_SECRETS))"
	@echo ""
	@echo "Live verification of the running runner (against mandate-sandbox-svc):"
	@echo "  make runner-up RUNNER_LOCAL=1   Run with the gitignored local override"
	@echo "  make verify-runner-live         Run scripts/verify_runner_live.py (opens and closes test PRs)"

runner-up:
	@test -r "$$HOME/.mandate/secrets/build/mandate-runner/private-key.pem" || { echo "ERROR: ~/.mandate/secrets/build/mandate-runner/ is missing its key" >&2; exit 1; }
	@docker network inspect $(LAB_NETWORK) >/dev/null 2>&1 || { echo "Creating network $(LAB_NETWORK) (normally made by dev-infra-lab's make infra-up)..."; docker network create $(LAB_NETWORK) >/dev/null; }
	@$(RUNNER_COMPOSE) up -d --build --wait --wait-timeout 300 mandate-runner-dind mandate-runner
	@echo "mandate-runner up. Run logs: http://localhost:8090/runs/<run-id>.log"

runner-down:
	@$(RUNNER_COMPOSE) stop mandate-runner mandate-runner-dind

runner-logs:
	@$(RUNNER_COMPOSE) logs -f mandate-runner

runner-ps:
	@$(RUNNER_COMPOSE) ps -a

runner-test:
	@docker build -q --target test --build-context config=$(ROOT)config/mandate-runner -t drivestream-lab/mandate-runner:test $(ROOT)services/mandate-runner >/dev/null
	@docker run --rm drivestream-lab/mandate-runner:test

runner-reset-sandbox:
	@$(RUNNER_COMPOSE) rm -sf mandate-runner-dind
	@docker volume rm -f mandate_runner_dind_data >/dev/null
	@$(RUNNER_COMPOSE) up -d --wait mandate-runner-dind

VERIFY_VENV := $(ROOT).venv-verify
verify-runner-live:
	@test -x $(VERIFY_VENV)/bin/python || { python3.12 -m venv $(VERIFY_VENV) && $(VERIFY_VENV)/bin/pip -q install "httpx==0.28.1" "PyJWT[crypto]==2.15.1"; }
	@$(VERIFY_VENV)/bin/python -u $(ROOT)scripts/verify_runner_live.py $(ARGS)
