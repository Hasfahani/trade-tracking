# PolySignal

A focused, server-rendered watchlist for tracking Polymarket wallet activity.

PolySignal is intentionally narrow:
- Add wallets
- Refresh on demand
- Review trade history from local storage

No background polling. No page-time API calls. No strategy engine.

## Why This App Exists

Most dashboards optimize for breadth. PolySignal optimizes for speed and operational clarity:
- Fast page loads from SQLite
- Manual refresh controls with visible results
- Clear wallet organization with tags, notes, pinning, and archive
- Export-ready trade views

## Product Boundaries

Included:
- Wallet watchlist with labels, tags, notes, pin, archive
- Manual refresh per wallet or all active wallets
- Sync status history and duplicate cleanup tooling
- Wallet and all-trades views with filtering, sorting, pagination, date presets
- CSV import and export
- Telegram alert settings and test action

Not included:
- Copy trading
- PnL or strategy analytics
- Auto-refresh on page render
- External API calls during page render

## Core Rules

- Page rendering reads from SQLite only
- Ingestion side effects stay outside page routes
- Trade deduplication is keyed by trade_id
- Manual refresh is the operating model

## Tech Stack

- FastAPI
- SQLAlchemy
- SQLite (default) or PostgreSQL
- Jinja2 templates
- Server-rendered HTML and CSS

## Quick Start (Windows PowerShell)

1. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies

```powershell
pip install -r requirements.txt
```

3. Initialize the database

```powershell
python scripts/init_db.py
```

4. Start the app

```powershell
./start_dev.ps1
```

5. Open:
- http://localhost:8000/wallets

## Launch Options

PowerShell:
- ./start_dev.ps1
- ./start_server.ps1

Batch:
- ./start_dev.bat
- ./start_server.bat

Direct uvicorn:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Notes:
- Launch scripts resolve Python from venv or .venv
- Launch scripts respect PORT
- Dev launcher watches app and tests, excluding venv/.venv/.venv313/data for better Windows watch performance

## First-Run Workflow

1. Go to /wallets
2. Add one wallet address
3. Click Refresh on that wallet
4. Open View trades
5. Check /admin/sync-status for refresh event details

## Architecture

Main modules:
- app/main.py: app factory, middleware, static mount, startup lifecycle
- app/routes/core.py: root, health, readiness, dashboard, ops pages
- app/routes/wallets.py: watchlist pages and wallet CRUD flows
- app/routes/trades.py: wallet trades, all trades, trade detail
- app/routes/exports.py: CSV exports
- app/routes/alerts.py: settings, sync status, admin refresh
- app/routes/_shared.py: route helpers
- app/ingest.py: fetch and ingest logic
- app/db.py: engine/session and lightweight schema migrations
- app/models.py: SQLAlchemy models
- app/view_helpers.py: reusable formatting and query helpers

UI runtime:
- Active stylesheet: app/static/style_v2.css
- Shared client behavior: app/static/base.js
- Primary templates are v2 templates, while a small set of legacy-compatible templates still exists in the repository

Design intent:
- Keep routes explicit and maintainable
- Keep external API work in refresh flows only
- Favor predictable local reads over background complexity

## Runtime Configuration

All configuration is read from environment variables at startup.

### Application

| Variable | Default | Description |
|---|---|---|
| APP_NAME | PolySignal | App title in UI. |
| APP_ENV | development | Environment label. production enables production-oriented cookie defaults. |
| LOG_LEVEL | INFO | Python log level. |
| HOST | 0.0.0.0 | Server bind address. |
| PORT | 8000 | Server port. |
| DATABASE_URL | sqlite:///./data/trades.db | SQLAlchemy URL. SQLite and PostgreSQL are supported. |
| STARTUP_DB_MAX_ATTEMPTS | 3 | Startup DB retry attempts before degraded readiness. |
| STARTUP_DB_RETRY_SECONDS | 1.0 | Delay between startup DB retries. |

### Authentication and Session

| Variable | Default | Description |
|---|---|---|
| DASHBOARD_PASSWORD | unset | If set, enables password protection and redirects unauthenticated users to /login. |
| SESSION_SECRET_KEY | change-me-in-production-use-a-long-random-secret | Session signing secret. Change for any non-local deployment. |
| SESSION_COOKIE_SECURE | true in production, else false | Secure flag for session cookie. |
| CSRF_COOKIE_SECURE | true in production, else false | Secure flag for CSRF cookie. |

### Pagination and Refresh

| Variable | Default | Description |
|---|---|---|
| DEFAULT_PAGE_SIZE | 50 | Default page size on trade views. |
| MAX_PAGE_SIZE | 200 | Maximum allowed page_size query value. |
| DEFAULT_REFRESH_LIMIT | 500 | Fetch limit per wallet refresh request. |

### Polymarket API Timeouts

| Variable | Default | Description |
|---|---|---|
| POLYMARKET_CONNECT_TIMEOUT_SECONDS | 5.0 | TCP connect timeout. |
| POLYMARKET_READ_TIMEOUT_SECONDS | 15.0 | Response read timeout. |
| POLYMARKET_WRITE_TIMEOUT_SECONDS | 15.0 | Request write timeout. |
| POLYMARKET_POOL_TIMEOUT_SECONDS | 5.0 | Connection pool wait timeout. |

### Telegram Alerts

Telegram settings are configured in /settings and stored in the app_settings table.

| Setting | Description |
|---|---|
| Bot Token | Bot token from @BotFather. Leave blank during save to preserve the existing token. |
| Chat ID | Target chat or channel id. |
| Alert minimum size | Lower-value trades are skipped. |
| Alerts enabled | Master on or off switch. |

Security note:
- In the current implementation, Telegram credentials are stored in the database and are not managed via environment variables.
- Protect database access and filesystem permissions in deployment environments.

## Main Routes

Core pages:
- GET /wallets
- GET /dashboard
- GET /all-trades
- GET /admin/sync-status
- GET /settings
- GET /admin/ops-ui

Health and readiness:
- GET /healthz (liveness)
- GET /readyz (readiness with DB check)

Wallet and trade actions:
- POST /wallets
- POST /wallets/{identifier}/refresh
- POST /wallets/refresh-all
- GET /wallets/export
- POST /wallets/import
- GET /all-trades/export

Admin refresh APIs:
- POST /admin/refresh
- POST /admin/refresh-all
- POST /admin/sync-status/cleanup

## Query Parameters You Will Use Most

- limit on wallet refresh endpoints
- limit_per_wallet on admin refresh endpoints
- address on admin refresh endpoints to target one wallet

## Data and Schema Notes

- Lightweight schema compatibility migrations run at startup
- Applied migrations are tracked in schema_migrations
- No external migration framework is required

Fields used by refresh status include:
- wallets.last_checked_at
- wallets.last_refresh_status
- wallets.last_refresh_count
- wallets.last_error_at
- wallets.last_error_message

Useful indexes include:
- wallets(is_archived, is_pinned, created_at)
- trades(wallet_address, traded_at)
- trades(wallet_address, side, traded_at)
- trades(wallet_address, market_title)
- sync_events(wallet_address, created_at)

## Troubleshooting

Port already in use:

```powershell
$env:PORT = "8010"
./start_dev.ps1
```

Virtual environment issues:
- Confirm interpreter exists under .venv/Scripts/python.exe or venv/Scripts/python.exe
- Reinstall dependencies with pip install -r requirements.txt

No new trades after refresh:
- This can be valid when upstream has no new records
- Check /admin/sync-status for details

Archived wallets missing:
- Enable Show archived wallets filter on /wallets

Refresh connectivity failures:
- Verify outbound access to https://data-api.polymarket.com
- Review errors in /admin/sync-status

Windows auto-start:
- setup_autostart.ps1 registers scheduled task PolymarketTracker
- Run as Administrator to create or update task

## Testing

Run all tests:

```powershell
pytest -q
```

Coverage includes:
- App factory wiring and middleware behavior
- Wallet and trades route behavior
- Refresh and sync status flows
- View helper formatting and filtering
- Schema migration idempotency
- Guardrail checks that page renders do not trigger ingestion side effects

## CLI Utility

refresh_now.py refreshes all wallets from command line:

```powershell
python refresh_now.py
```

This is useful for one-off refresh runs without using the web UI.
