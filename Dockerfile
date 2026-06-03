# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    APP_ENV=production \
    DOCKER_CONTAINER=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libpq5 \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

FROM base AS runtime

ENV PATH="/opt/venv/bin:$PATH"

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown -R app:app /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app run.py gunicorn_conf.py requirements.txt ./

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python scripts/container_healthcheck.py

CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-c", "gunicorn_conf.py"]
