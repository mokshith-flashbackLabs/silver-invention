# Mirrors the Flashback agent service Dockerfile (AgentMeeMaw/Dockerfile):
# venv-in-builder, slim runtime, non-root user, uvicorn --factory entrypoint.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8000

WORKDIR /app

RUN groupadd --system imageshield \
    && useradd --system --gid imageshield --home-dir /app imageshield

COPY --from=builder /venv /venv
COPY migrations ./migrations
COPY scripts ./scripts

USER imageshield

EXPOSE 8000

CMD ["sh", "-c", "uvicorn imageshield.http.app:create_app --factory --host ${HTTP_HOST} --port ${HTTP_PORT}"]
