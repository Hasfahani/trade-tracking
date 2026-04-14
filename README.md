# Polymarket Wallet Trades Watchlist

A focused, server-rendered watchlist app for tracking Polymarket wallet trades.

The product is intentionally narrow: add wallets, refresh trades manually, and inspect stored trade history quickly from local SQLite.

## Product Scope

Included:
- Wallet watchlist with optional labels
- Manual refresh per wallet or for all wallets
- Trade storage in local SQLite
- Trade filtering, sorting, pagination, CSV export
- Sync status and refresh event visibility
- Refresh result visibility per wallet, including last refresh time/status/error

Explicitly excluded:
- PnL and position analytics
- Win/loss dashboards
- Copy trading
- Strategy analytics
- Live auto-refresh on page render

## Hard Rules

- No external API calls during page render
- All page loads read from SQLite only
- Ingestion stays isolated from web routes (via app/ingest.py)
- Trade deduplication via unique trade_id
- Manual refresh is the operating model

## Tech Stack

- FastAPI
- SQLAlchemy
- SQLite
- Jinja2 templates
- Server-rendered HTML/CSS

## Architecture

- app/main.py: App bootstrap and startup initialization
- app/routes_v2.py: HTTP routes and server-rendered page handlers
- app/ingest.py: Polymarket fetch, normalize, and ingest logic
- app/models.py: SQLAlchemy models
- app/db.py: Engine/session setup and lightweight schema backfill
- app/templates/: Jinja templates
- app/static/style_v2.css: Shared design system and responsive UI

Design decisions:
- Keep ingestion side effects out of page rendering paths
- Keep routes simple and explicit
- Prefer local query-driven pages over background complexity
- Favor maintainability over feature volume

Operational model:
- Page renders never call external APIs
- Refresh happens only when a user triggers it from the UI or admin endpoints
- Refresh results are stored in SQLite and shown later on the wallet and sync pages

## Setup

1. Create and activate virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies

```powershell
pip install -r requirements.txt
```

3. Initialize database

```powershell
python scripts/init_db.py
```

4. Run server

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open:
- http://localhost:8000/wallets

## Runtime Configuration

Environment variables:
- APP_NAME: app title shown in the UI
- LOG_LEVEL: application log level
- PORT: server port (default 8000)
- HOST: server host (default 0.0.0.0)
- DATABASE_URL: SQLAlchemy database URL
- DEFAULT_PAGE_SIZE: trade page size default
- MAX_PAGE_SIZE: trade page size cap
- DEFAULT_REFRESH_LIMIT: per-refresh fetch limit

Example (PowerShell):

```powershell
$env:PORT = "8010"
uvicorn app.main:app --reload --host 0.0.0.0 --port $env:PORT
```

## Usage

1. Add wallet on /wallets
- Address must be 0x + 40 hex chars
- Optional label for readability

2. Refresh data manually
- Use Refresh this wallet or Refresh all wallets in UI
- Or call admin API endpoints
- Refresh status, inserted trade count, and errors are written to SQLite for later review

3. Review trades
- Open wallet trades page
- Filter by side/date/market search
- Sort by newest, oldest, or largest size
- Use copy buttons for wallet, trade, and condition IDs where helpful

## API Endpoints

Core:
- GET /wallets
- POST /wallets
- POST /wallets/{identifier}/refresh
- POST /wallets/refresh-all
- GET /wallets/{identifier}/trades
- GET /wallets/{identifier}/trades/export
- GET /all-trades
- GET /trades/{trade_id}
- GET /wallets/{identifier}/delete-confirm
- POST /wallets/{identifier}/delete

Operational:
- POST /admin/refresh
- POST /admin/refresh-all
- GET /admin/sync-status
- POST /admin/sync-status/cleanup

## Schema Notes

The app performs lightweight SQLite compatibility backfills at startup for missing wallet columns, including refresh metadata columns.

Newer schema fields used by refresh status:
- wallets.last_checked_at
- wallets.last_refresh_status
- wallets.last_refresh_count
- wallets.last_error_at
- wallets.last_error_message
- sync_events.duplicate_count

Indexes added for responsiveness:
- trades(wallet_address, traded_at)
- trades(wallet_address, side, traded_at)
- trades(wallet_address, market_title)
- sync_events(wallet_address, created_at)

No external migration framework is required for this project.

## Troubleshooting

Port already in use:
- Change port: uvicorn app.main:app --reload --port 8010
- Or set PORT and rerun
- PowerShell example:

```powershell
$env:PORT = "8010"
uvicorn app.main:app --reload --host 0.0.0.0 --port $env:PORT
```

- Batch/launcher scripts also respect `PORT`

Virtual environment issues:
- Confirm interpreter exists under .venv/Scripts/python.exe or venv/Scripts/python.exe
- Reinstall dependencies with pip install -r requirements.txt

No new trades after refresh:
- This can be normal if nothing new is available
- Check /admin/sync-status for refresh events and errors

## Testing

Run tests:

```powershell
pytest -q
```

Tests cover:
- Wallet address validation behavior
- Trade dedup logic
- Trade normalization behavior
- Core route behavior for wallet/trades pages
- Manual refresh route messaging
