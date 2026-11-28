# mandate-runner. Plain Python, no model. Talks to the sandbox (Docker-in-Docker)
# over TLS; never runs PR code itself. One image, three roles (see docker-compose.runner.yml):
#   runner    grades PRs and runs develop; holds the GitHub key
#   proxy     the sandbox's only way out: public addresses only
#   deployer  the host Docker jobs (sandbox rebuild, deploy); no key, no PR code
FROM docker:29-cli@sha256:34507eb5069d8be82c95dd3b6987d4250fd5fee49b82e2509b7d7a7030740d6d AS docker-cli

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates make \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

FROM base AS test
RUN pip install --no-cache-dir ".[test]"
COPY tests ./tests
COPY collector ./collector
COPY --from=config runner.toml /etc/mandate-runner/runner.toml
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
RUN pip install --no-cache-dir . && rm -rf /app/src
COPY job-image /app/job-image
# The runner's docker client config for the sandbox: hands the egress proxy to every
# build and container it starts there (DOCKER_CONFIG, set for the runner role only).
COPY sandbox-docker-config /app/sandbox-docker-config
COPY src/mandate_runner/healthcheck.py /app/healthcheck.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["python", "-m", "mandate_runner"]
