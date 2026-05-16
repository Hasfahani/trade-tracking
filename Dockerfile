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

# run.py reads PORT from os.environ directly — works in exec form (no shell needed).
CMD ["python", "run.py"]
