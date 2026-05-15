FROM python:3.11-slim

WORKDIR /app

# System deps: gcc for C extensions, postgresql-client for pg_isready health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies before copying source (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Capture build-time git commit (Railway sets RAILWAY_GIT_COMMIT_SHA)
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

EXPOSE 8000

# Initialise DB (idempotent: creates tables + runs schema migrations) then start server.
# The PORT env var is set by Railway; fall back to 8000 for local Docker usage.
CMD ["sh", "-c", "python scripts/init_db.py && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
