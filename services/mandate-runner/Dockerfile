# mandate-runner. Plain Python, no model. Talks to the sandbox (Docker-in-Docker)
# over TLS; never runs PR code itself.
FROM docker:29-cli@sha256:34507eb5069d8be82c95dd3b6987d4250fd5fee49b82e2509b7d7a7030740d6d AS docker-cli

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

FROM base AS test
RUN pip install --no-cache-dir ".[test]"
COPY tests ./tests
COPY --from=config runner.toml /etc/mandate-runner/runner.toml
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
RUN pip install --no-cache-dir . && rm -rf /app/src
COPY job-image /app/job-image
COPY src/mandate_runner/healthcheck.py /app/healthcheck.py
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["python", "-m", "mandate_runner"]
