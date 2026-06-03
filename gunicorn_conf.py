"""Gunicorn configuration for container deployments."""

from __future__ import annotations

import multiprocessing
import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = _env_int("WEB_CONCURRENCY", 1)
threads = _env_int("GUNICORN_THREADS", 1)
worker_tmp_dir = "/dev/shm"
timeout = _env_int("GUNICORN_TIMEOUT", 120)
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5)
max_requests = _env_int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 100)
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")

if os.getenv("WEB_CONCURRENCY") is None and os.getenv("AUTO_WEB_CONCURRENCY", "").lower() in {"1", "true", "yes"}:
    workers = max(1, multiprocessing.cpu_count() * 2 + 1)
