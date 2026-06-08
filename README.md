# PolySignal

PolySignal is a focused FastAPI dashboard for tracking Polymarket wallet activity.
It stores watched wallets and fetched trades locally, then gives you fast
server-rendered views for wallet review, trade filtering, alerts, exports, and
operational diagnostics.

The app is designed for a simple operating model:

- Add Polymarket wallet addresses to a watchlist.
- Refresh one wallet, all active wallets, or a full trade history on demand.
- Review the stored data from SQLite or PostgreSQL-backed pages.
- Optionally enable scheduled refreshes, Telegram alerts, and AI-assisted trade
  analysis.

## What It Does

- Wallet watchlist with labels, tags, notes, pinning, archiving, editing, and
  deletion.
- Wallet import/export through CSV.
- Manual wallet refresh from the Polymarket public data API.
- Optional background auto-refresh scheduler controlled by environment
  variables.
- Dashboard with activity, top wallets, top markets, recent trades, refresh
  health, and retention summaries.
- Wallet detail pages with trade statistics, activity timelines, market
  breakdowns, and optional AI wallet summaries.
- Wallet-specific and global trade views with pagination, sorting, filtering,
  date presets, side filters, market search, wallet search, and CSV export.
- Trade detail pages with related trades and optional AI trade analysis.
- Telegram alert settings, alert test action, and alert dispatch for newly
  imported trades.
- Sync status history, duplicate detection, and duplicate cleanup tooling.
- Full JSON backup export/import for moving or restoring app data.
- Health, readiness, schema, and operations endpoints for deployment checks.
- Optional password protection, CSRF protection, rate limiting, security
  headers, request IDs, and structured production logging.
- Lightweight built-in schema compatibility migrations for SQLite and
  PostgreSQL.

## What It Is Not

- It is not a copy-trading bot.
- It does not execute trades.
- It does not call external APIs while rendering normal pages.
- It does not require a separate frontend build step.
- It is not a full accounting system; trade values and simple YES/NO summaries
  are review aids, not financial statements.

## Tech Stack

- Python 3.10+
- FastAPI
- SQLAlchemy
- SQLite by default, PostgreSQL supported
- Jinja2 templates
- Server-rendered HTML, CSS, and small JavaScript helpers
- httpx for Polymarket API access
- SlowAPI for selected rate limits
- APScheduler for optional auto-refresh
- Anthropic, Ollama, or HuggingFace for optional AI analysis

## Quick Start

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/init_db.py
.\start_dev.ps1
```

Open:

- http://localhost:8000/wallets
- http://localhost:8000/dashboard

### Direct Uvicorn

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### Command Line Refresh

```powershell
python refresh_now.py
```

This refreshes all wallets from the command line without using the web UI.

## First-Run Workflow

1. Open `/wallets`.
2. Add a Polymarket wallet address.
3. Click `Refresh` for that wallet.
4. Open the wallet or trade views to inspect stored activity.
5. Check `/admin/sync-status` for refresh results and errors.

## Launch Scripts

PowerShell:

- `.\start_dev.ps1`
- `.\start_server.ps1`
- `.\start_public_free.ps1`

Batch:

- `start_dev.bat`
- `start_server.bat`
- `start_public_free.bat`

Notes:

- Launch scripts resolve Python from `venv`, `.venv`, or the active
  interpreter.
- Launch scripts respect the `PORT` environment variable.
- The development launcher watches app and test files while excluding common
  virtual environment and data directories.

## Runtime Configuration

All configuration is read from environment variables at startup.

### Application

| Variable | Default | Description |
| --- | --- | --- |
| `APP_NAME` | `PolySignal` | App title shown in the UI and health responses. |
| `APP_VERSION` | current git commit or `dev` | Build/version label. |
| `APP_ENV` | `development`, or hosted-platform production default | Runtime environment. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `PUBLIC_BASE_URL` | `RENDER_EXTERNAL_URL` or empty | Canonical external URL for sitemap output. |
| `RUNTIME_PLATFORM` | auto-detected | `local`, `render`, `railway`, or `docker` style platform label. |
| `HOST` | `0.0.0.0` | Server bind address used by scripts/deployment. |
| `PORT` | `8000` | Server port. |
| `DATABASE_URL` | `sqlite:///./data/app.db` | SQLAlchemy database URL. SQLite and PostgreSQL are supported. |

### Database and Startup

| Variable | Default | Description |
| --- | --- | --- |
| `STARTUP_DB_MAX_ATTEMPTS` | `3` | Startup database initialization attempts. |
| `STARTUP_DB_RETRY_SECONDS` | `1.0` | Delay between startup DB retries. |
| `STARTUP_SEED_WALLETS` | `true` | Seed bundled watchlist wallets during startup maintenance. |
| `DB_POOL_SIZE` | `5` | PostgreSQL pool size. |
| `DB_MAX_OVERFLOW` | `10` | PostgreSQL max overflow connections. |
| `DB_POOL_TIMEOUT` | `30.0` | PostgreSQL pool wait timeout in seconds. |
| `DB_POOL_RECYCLE` | `1800` | PostgreSQL pool recycle seconds. |

### Authentication and Security

| Variable | Default | Description |
| --- | --- | --- |
| `DASHBOARD_PASSWORD` | unset | Enables password login when set. Required in production. |
| `SESSION_SECRET_KEY` | development default | Session signing secret. Change for any deployment. |
| `SESSION_COOKIE_NAME` | `polysignal_session` | Session cookie name. |
| `SESSION_COOKIE_SECURE` | `true` in production, else `false` | Secure flag for session cookie. |
| `CSRF_COOKIE_SECURE` | `true` in production, else `false` | Secure flag for CSRF cookie. |

Security behavior includes session middleware, CSRF middleware for forms,
security headers, no-store caching for HTML, static asset caching, selected
endpoint rate limits, login attempt throttling, and request ID headers.

### Pagination and Refresh

| Variable | Default | Description |
| --- | --- | --- |
| `DEFAULT_PAGE_SIZE` | `50` | Default page size for trade views. |
| `MAX_PAGE_SIZE` | `200` | Maximum accepted `page_size`. |
| `DEFAULT_REFRESH_LIMIT` | `200` | Default Polymarket records fetched per wallet refresh request. |

### Polymarket API Timeouts

| Variable | Default | Description |
| --- | --- | --- |
| `POLYMARKET_CONNECT_TIMEOUT_SECONDS` | `5.0` | TCP connect timeout. |
| `POLYMARKET_READ_TIMEOUT_SECONDS` | `15.0` | Response read timeout. |
| `POLYMARKET_WRITE_TIMEOUT_SECONDS` | `15.0` | Request write timeout. |
| `POLYMARKET_POOL_TIMEOUT_SECONDS` | `5.0` | Connection pool wait timeout. |

### Auto-Refresh

Auto-refresh is disabled by default. Set an interval greater than zero to enable
the in-app scheduler.

| Variable | Default | Description |
| --- | --- | --- |
| `AUTO_REFRESH_INTERVAL_MINUTES` | `0` | Minutes between scheduled refresh jobs. `0` disables the scheduler. |
| `AUTO_REFRESH_MAX_WALLETS` | `50` | Maximum active wallets refreshed per scheduled job. |
| `AUTO_REFRESH_ALLOW_MULTI_WORKER` | `false` | Allow scheduler when multiple web workers are running. |
| `WEB_CONCURRENCY` | `UVICORN_WORKERS` or `1` | Worker count used to guard duplicate scheduled jobs. |

### AI Analysis

AI features are optional. Provider priority is Claude, then Ollama, then
HuggingFace.

| Variable | Default | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables Claude-backed analysis. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model name. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL. |
| `OLLAMA_MODEL` | `mistral:latest` | Ollama model name. |
| `OLLAMA_TIMEOUT_SECONDS` | `60.0` | Ollama request timeout. |
| `HUGGINGFACE_API_KEY` | unset | Enables HuggingFace-backed analysis. |
| `AI_CACHE_TTL_HOURS` | `72` | AI cache freshness window. |
| `AI_RATE_LIMIT` | `20/minute` | Rate limit for AI endpoints. |
| `REFRESH_RATE_LIMIT` | `10/minute` | Configured refresh rate-limit value. |

See [AI_SETUP.md](AI_SETUP.md) for provider setup notes and
[AI_EXAMPLES.py](AI_EXAMPLES.py) for examples.

### Telegram Alerts

Telegram settings are configured in `/settings` and stored in the
`app_settings` database table:

- Bot token
- Chat ID
- Minimum trade size
- Alerts enabled/disabled

Alert credentials are stored in the database, so protect database access and
filesystem permissions in deployments.

### Retention Metrics

| Variable | Default | Description |
| --- | --- | --- |
| `RETENTION_METRICS_ENABLED` | `true` | Enables event logging and retention metric endpoints. |

Use `python scripts/backfill_retention.py` to compute daily/weekly retention
summary tables from the raw event log.

## Main Routes

### Pages

- `GET /` redirects to `/wallets`
- `GET /login`
- `GET /logout`
- `GET /wallets`
- `GET /wallets/import`
- `GET /wallets/{identifier}`
- `GET /wallets/{identifier}/edit`
- `GET /wallets/{identifier}/delete-confirm`
- `GET /wallets/{identifier}/trades`
- `GET /all-trades`
- `GET /trades/{trade_id}`
- `GET /dashboard`
- `GET /settings`
- `GET /admin/sync-status`
- `GET /admin/ops-ui`
- `GET /admin/import-backup`

### Actions and APIs

- `POST /login`
- `POST /wallets`
- `POST /wallets/import`
- `POST /wallets/refresh-all`
- `POST /wallets/{identifier}/refresh`
- `POST /wallets/{identifier}/edit`
- `POST /wallets/{identifier}/pin`
- `POST /wallets/{identifier}/archive`
- `POST /wallets/{identifier}/unarchive`
- `POST /wallets/{identifier}/delete`
- `POST /settings`
- `POST /settings/test-alert`
- `POST /admin/refresh`
- `POST /admin/refresh-all`
- `POST /admin/sync-status/cleanup`
- `POST /admin/import-backup`
- `GET /api/last-sync`
- `GET /api/wallets/{identifier}/ai-summary`
- `GET /api/trades/{trade_id}/ai-analysis`
- `POST /api/trades/{trade_id}/ai-analysis/invalidate`
- `GET /admin/metrics/retention`

### Exports and Backups

- `GET /wallets/export`
- `GET /wallets/{identifier}/trades/export`
- `GET /all-trades/export`
- `GET /admin/backup.json`

### Operations

- `GET /healthz`
- `GET /readyz`
- `GET /admin/ops`
- `GET /admin/ops-ui`
- `GET /admin/schema-version`
- `GET /robots.txt`
- `GET /sitemap.xml`

## Data Model

Main tables:

- `wallets`: watched wallet metadata and latest refresh state.
- `trades`: normalized Polymarket trade records keyed by unique `trade_id`.
- `sync_events`: refresh status history, counts, duplicates, duration, and
  errors.
- `app_settings`: Telegram alert settings.
- `trade_analysis`: cached AI trade analysis.
- `event_log`: raw retention event stream.
- `retention_daily` and `retention_weekly`: precomputed retention summaries.
- `schema_migrations`: applied lightweight migration versions.

Important implementation notes:

- Normal page rendering reads from the database.
- External Polymarket API calls happen in refresh flows and scheduler jobs.
- Trade deduplication is based on `trade_id`; duplicate cleanup can also remove
  semantic duplicates.
- Startup applies compatibility migrations automatically.
- Old sync events are pruned during startup maintenance.

## Project Structure

```text
app/
  main.py              FastAPI app factory, middleware, lifespan, scheduler
  settings.py          Environment configuration
  db.py                Engine, sessions, schema initialization, migrations
  models.py            SQLAlchemy models
  ingest.py            Polymarket fetch, normalization, refresh, duplicates
  ai_analysis.py       Optional AI provider integration and cache logic
  alerts.py            Telegram alert logic
  analytics.py         Dashboard query helpers
  retention.py         Retention event logging and summaries
  routes/
    auth.py            Login/logout
    core.py            Dashboard, health, readiness, ops, sitemap
    wallets.py         Wallet CRUD, refresh, import, AI summary
    trades.py          Trade lists, detail, AI analysis
    exports.py         CSV and JSON backup import/export
    alerts.py          Settings, sync status, admin refresh
    retention.py       Retention metrics endpoint
  templates/           Jinja2 pages
  static/              CSS, JavaScript, favicon
scripts/               Database, backup, migration, retention, utility scripts
tests/                 Pytest coverage
```

## Backups

Export a full JSON backup while logged in:

```text
/admin/backup.json
```

Or from the command line:

```powershell
python scripts/export_backup.py
```

Restore with a dry run first:

```powershell
python scripts/import_backup.py backups/YOUR_BACKUP.json --dry-run
python scripts/import_backup.py backups/YOUR_BACKUP.json --yes
```

The web UI also provides `/admin/import-backup`.

## Deployment

### Docker

```powershell
docker compose up --build
```

See [DOCKER_DEPLOY.md](DOCKER_DEPLOY.md). The compose setup is intended for a
local PostgreSQL-backed stack.

### Render

See [RENDER_DEPLOY.md](RENDER_DEPLOY.md) and `render.yaml`.

Use Render Postgres for persistent data. Do not rely on SQLite storage on Render
free web services.

### Railway

See [RAILWAY_SETUP.md](RAILWAY_SETUP.md) and `railway.json`.

### Free Public Access

See [FREE_HOSTING.md](FREE_HOSTING.md). A simple no-payment option is running
locally with the default SQLite database and exposing it through a Cloudflare
Tunnel.

## Testing

Run all tests:

```powershell
pytest -q
```

Coverage includes:

- App factory and middleware behavior
- Authentication, CSRF, security headers, and smoke checks
- Wallet and trade route behavior
- CSV export and backup behavior
- Refresh, ingestion, sync status, duplicate handling, and alerts
- AI analysis availability and caching behavior
- Retention metrics
- Database migration idempotency
- Guardrails that normal page renders do not trigger ingestion side effects

## Troubleshooting

### Port Already in Use

```powershell
$env:PORT = "8010"
.\start_dev.ps1
```

### Virtual Environment Problems

- Confirm that `.venv\Scripts\python.exe` or `venv\Scripts\python.exe` exists.
- Reinstall dependencies with `pip install -r requirements.txt`.

### No New Trades After Refresh

- This can be valid when the upstream wallet has no new trade records.
- Check `/admin/sync-status` for fetched, inserted, duplicate, and error counts.

### Archived Wallets Missing

- Enable the archived-wallet filter on `/wallets`.

### Polymarket Refresh Failures

- Verify outbound access to `https://data-api.polymarket.com`.
- Check `/admin/sync-status` for timeout, rate-limit, or API errors.
- Try a lower `limit` query value if refreshes are slow.

### Production Startup Fails

- Set `DASHBOARD_PASSWORD`.
- Set a strong `SESSION_SECRET_KEY` with at least 32 characters.
- Confirm `DATABASE_URL` is reachable.
- Check `/readyz` and `/admin/ops`.

### Windows Auto-Start

`setup_autostart.ps1` registers a scheduled task named `PolymarketTracker`.
Run PowerShell as Administrator to create or update the task.

## Development Notes

- Keep ingestion side effects inside explicit refresh paths or scheduler jobs.
- Keep ordinary page rendering database-only.
- Keep route helpers in `app/routes/_shared.py` when multiple route modules need
  the same behavior.
- Prefer focused tests for route, migration, security, and ingestion changes.
