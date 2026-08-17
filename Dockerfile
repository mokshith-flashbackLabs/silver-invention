# Mirrors the Flashback agent service Dockerfile (AgentMeeMaw/Dockerfile):
# venv-in-builder, slim runtime, non-root user, uvicorn --factory entrypoint.
#
# linux/arm64 is not a preference: the dev host is Graviton (t4g.medium) and an
# amd64 image fails with an exec-format error that reads like a broken
# entrypoint, which is an hour of debugging the wrong thing.
FROM --platform=linux/arm64 python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install .

# linux/arm64 is not a preference: the dev host is Graviton (t4g.medium) and an
# amd64 image fails with an exec-format error that reads like a broken
# entrypoint, which is an hour of debugging the wrong thing.
FROM --platform=linux/arm64 python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8081

WORKDIR /app

RUN groupadd --system imageshield \
    && useradd --system --gid imageshield --home-dir /app imageshield

COPY --from=builder /venv /venv
COPY migrations ./migrations
COPY scripts ./scripts

USER imageshield

# 8081 on the host — networkMode: host means the container binds the host
# interface directly, and `api` already holds 8080.
EXPOSE 8081

CMD ["sh", "-c", "uvicorn imageshield.http.app:create_app --factory --host ${HTTP_HOST} --port ${HTTP_PORT}"]
