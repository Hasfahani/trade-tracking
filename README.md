# Polymarket Wallet Trades Watchlist

A focused, server-rendered watchlist app for tracking Polymarket wallet trades.

The product is intentionally narrow: add wallets, refresh trades manually, and inspect stored trade history quickly from local SQLite.

## Product Scope

Included:
- Wallet watchlist with optional labels
- Wallet notes, tags, pinning, and archive state
- Manual refresh per wallet or for all wallets
- Trade storage in local SQLite
- Trade filtering, sorting, pagination, date presets, CSV export
- Trade value summaries (YES/NO/total value, avg price) per active filter set
- Sync status and refresh event visibility
- Refresh result visibility per wallet, including last refresh time/status/error

Explicitly excluded:
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
- app/view_helpers.py: Query/filter/date-preset/view helper logic used by routes_v2
- app/templates/: Jinja templates
- app/static/style_v2.css: Shared design system and responsive UI

Active runtime path:
- The app currently mounts `routes_v2` from `app/main.py`
- `_v2` templates and `style_v2.css` are the active UI stack
- Legacy `routes.py`, `style.css`, and non-`_v2` templates have been removed

Design decisions:
- Keep ingestion side effects out of page rendering paths
- Keep routes simple and explicit
- Prefer local query-driven pages over background complexity
- Favor maintainability over feature volume

Operational model:
- Page renders never call external APIs
- Refresh happens only when a user triggers it from the UI or admin endpoints
- Refresh results are stored in SQLite and shown later on the wallet and sync pages
- Archived wallets stay in SQLite but are hidden from the default wallet list

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
./start_dev.ps1
```

Alternative launchers (Windows):
- PowerShell launcher: `./start_server.ps1`
- Batch launcher: `./start_server.bat`
- Dev launcher with scoped reload: `./start_dev.ps1`

The PowerShell launcher:
- Resolves virtualenv Python from `venv` or `.venv`
- Checks that `uvicorn` is installed
- Starts on `PORT` (default 8000)
- Restarts server automatically if it exits

The dev launcher:
- Enables `--reload`
- Watches only `app` and `tests`
- Excludes `venv`, `.venv`, `.venv313`, and `data` to avoid Windows file-watch slowdowns

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
- POLYMARKET_CONNECT_TIMEOUT_SECONDS: Polymarket API connect timeout in seconds (default 5.0)
- POLYMARKET_READ_TIMEOUT_SECONDS: Polymarket API read timeout in seconds (default 15.0)
- POLYMARKET_WRITE_TIMEOUT_SECONDS: Polymarket API write timeout in seconds (default 15.0)
- POLYMARKET_POOL_TIMEOUT_SECONDS: Polymarket API connection pool timeout in seconds (default 5.0)

Refresh/API query parameters:
- `POST /wallets/{identifier}/refresh?limit=<int>` (1-1000)
- `POST /wallets/refresh-all?limit=<int>` (1-1000)
- `POST /admin/refresh?address=<wallet>&limit_per_wallet=<int>`
- `POST /admin/refresh-all?address=<wallet>&limit_per_wallet=<int>`

Notes:
- `admin/refresh-all` triggers full-history pagination mode (`fetch_all=True`) in ingestion
- `address` on admin endpoints scopes refresh to one wallet; omit to process all wallets

Example (PowerShell):

```powershell
$env:PORT = "8010"
./start_dev.ps1
```

## Usage

1. Add wallet on /wallets
- Address must be 0x + 40 hex chars
- Optional label for readability
- Optional tags and notes for organization

2. Refresh data manually
- Use Refresh this wallet or Refresh all wallets in UI
- Or call admin API endpoints
- Refresh status, inserted trade count, and errors are written to SQLite for later review
- Refresh calls require outbound network access to Polymarket public data API (`https://data-api.polymarket.com`)
- Archived wallets are excluded from refresh-all flows until restored

3. Review trades
- Open the wallet profile page (/wallets/{identifier}) for a summary: trade count, first/latest trade date, value summary (YES/NO/total, VWAP), refresh status, and activity timeline
- Open wallet trades page or /all-trades for a unified view across all wallets
- Filter by side/date/market search
- Use date presets like today / 7d / 30d when helpful
- Sort by newest, oldest, largest size, or highest value
- Use copy buttons for wallet, trade, and condition IDs where helpful
- Export filtered trades to CSV using the Export CSV button on any trades page
- Wallet search on /all-trades matches both wallet address and wallet label

4. Review the dashboard
- Visit /dashboard for a quick overview: wallet counts, total stored trades, last refresh timestamps
- See the 20 most recent trades across all wallets
- See the top 5 wallets by trade count

5. Import and export wallets
- Export all wallets to CSV via /wallets/export (includes address, label, tags, notes, pin/archive state)
- Import wallets in bulk via /wallets/import — invalid addresses and duplicates are skipped automatically
- Tags are semicolon-separated in the CSV (e.g. `tag1;tag2`)

6. Organize the watchlist
- Search wallets by label, address, tags, and notes
- Pin important wallets to the top
- Archive wallets to hide them without deleting stored trades
- Use the edit page to update labels, tags, notes, and pin state

## API Endpoints

Core:
- GET /dashboard
- GET /wallets
- POST /wallets
- GET /wallets/{identifier}
- GET /wallets/{identifier}/edit
- POST /wallets/{identifier}/edit
- POST /wallets/{identifier}/pin
- POST /wallets/{identifier}/archive
- POST /wallets/{identifier}/unarchive
- POST /wallets/{identifier}/refresh
- POST /wallets/refresh-all
- GET /wallets/{identifier}/trades
- GET /wallets/{identifier}/trades/export
- GET /all-trades
- GET /all-trades/export
- GET /trades/{trade_id}
- GET /wallets/export
- GET /wallets/import
- POST /wallets/import
- GET /wallets/{identifier}/delete-confirm
- POST /wallets/{identifier}/delete

Operational:
- POST /admin/refresh
- POST /admin/refresh-all
- GET /admin/sync-status (paginated; default page_size=50)
- POST /admin/sync-status/cleanup (redirects to /admin/sync-status with flash confirmation)

Common query params:
- `limit` on wallet refresh routes controls per-request fetch size
- `limit_per_wallet` on admin refresh routes controls per-wallet fetch size
- `address` on admin routes targets one wallet (id/address resolution supported)

## Schema Notes

The app performs lightweight SQLite compatibility backfills at startup for missing wallet columns, including refresh metadata columns.

Newer schema fields used by refresh status:
- wallets.last_checked_at
- wallets.last_refresh_status
- wallets.last_refresh_count
- wallets.last_error_at
- wallets.last_error_message

Additional wallet compatibility fields:
- wallets.tags
- wallets.notes
- wallets.is_pinned
- wallets.is_archived
- sync_events.duplicate_count
- sync_events.duration_ms

Indexes added for responsiveness:
- wallets(is_archived, is_pinned, created_at)
- trades(wallet_address, traded_at)
- trades(wallet_address, side, traded_at)
- trades(wallet_address, market_title)
- sync_events(wallet_address, created_at)

No external migration framework is required for this project.

## CLI Utilities

`refresh_now.py` — standalone script to refresh all wallets from the command line without the web server:

```powershell
python refresh_now.py
```

Outputs per-wallet trade counts to stdout. Useful for one-off bulk refreshes outside the UI.

## Troubleshooting

Port already in use:
- Change port: set `PORT` and run `./start_dev.ps1`
- Or set PORT and rerun
- PowerShell example:

```powershell
$env:PORT = "8010"
./start_dev.ps1
```

- Batch/launcher scripts also respect `PORT`

Virtual environment issues:
- Confirm interpreter exists under .venv/Scripts/python.exe or venv/Scripts/python.exe
- Reinstall dependencies with pip install -r requirements.txt

No new trades after refresh:
- This can be normal if nothing new is available
- Check /admin/sync-status for refresh events and errors

Archived wallets not visible:
- Archived wallets are hidden from the default `/wallets` view
- Use the wallet filters and enable `Show archived wallets` to restore them

Refresh/API connectivity failures:
- Manual/admin refresh depends on Polymarket data API reachability
- Confirm outbound access to `https://data-api.polymarket.com`
- If failures persist, review `/admin/sync-status` for recorded error messages

Windows auto-start (optional):
- `setup_autostart.ps1` registers scheduled task `PolymarketTracker`
- Run as Administrator to create/update the startup task

## Testing

Run tests:

```powershell
pytest -q
```

Tests cover:
- Wallet address validation behavior
- Wallet creation with tags/notes
- Trade dedup logic
- Trade normalization behavior
- Core route behavior for wallet/trades pages
- Manual refresh route messaging
- Wallet archive/edit behavior
- Trade date preset filtering
