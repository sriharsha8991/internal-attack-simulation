# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv from the official image
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first for cache reuse
COPY pyproject.toml uv.lock* ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev || uv sync --no-dev

# App content
COPY config/ ./config/
COPY skills/ ./skills/
COPY docs/ ./docs/

ENV PATH="/opt/venv/bin:${PATH}" \
    BAS_ENGAGEMENTS_DIR=/data/engagements \
    LOG_LEVEL=info

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "bas.api:app", "--host", "0.0.0.0", "--port", "8765"]
