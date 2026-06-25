<!-- Summary: Explains how to deploy PolySignal with Docker. -->
<!-- Details: It gives plain written instructions or reference notes for running, deploying, or understanding the project. -->
# Docker Deployment

This repo includes a production Docker image and a Compose stack with Postgres.

## Quick Start

```powershell
docker compose up --build
```

Open `http://localhost:8000`.

Before using it for anything real, set strong secrets:

```powershell
$env:DASHBOARD_PASSWORD = "your-login-password"
$env:SESSION_SECRET_KEY = "a-random-32-plus-character-secret"
docker compose up --build
```

## Services

- `app`: FastAPI app running as a non-root user
- `db`: Postgres 16 with a persistent named volume
- `ollama`: optional local AI backend, enabled with the `ai-local` profile

Run with local Ollama:

```powershell
$env:OLLAMA_MODEL = "mistral:latest"
docker compose --profile ai-local up --build
```

## Production Notes

- Use Postgres for deployed containers. SQLite is fine for local testing, but container filesystems are often ephemeral.
- Set `APP_ENV=production`, `DASHBOARD_PASSWORD`, `SESSION_SECRET_KEY`, and `PUBLIC_BASE_URL`.
- The image has a built-in `/healthz` healthcheck.
- Docker uses Gunicorn with Uvicorn workers. `WEB_CONCURRENCY` controls worker processes; keep it at `1` on tiny/free instances.

## Build Only

```powershell
docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) -t polysignal:latest .
docker run --rm -p 8000:8000 `
  -e DASHBOARD_PASSWORD="your-login-password" `
  -e SESSION_SECRET_KEY="a-random-32-plus-character-secret" `
  -e DATABASE_URL="sqlite:////app/data/app.db" `
  polysignal:latest
```
