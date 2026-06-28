<!-- Summary: Introduces PolySignal and explains how to run it. -->
<!-- Details: It gives plain written instructions or reference notes for running, deploying, or understanding the project. -->
# PolySignal

PolySignal is a private FastAPI dashboard for tracking Polymarket wallet
activity. It keeps a watchlist of wallets, imports their public trades into a
local database, and gives you fast server-rendered pages for review, filtering,
alerts, exports, backups, operational checks, and optional AI-assisted analysis.

It is built for a simple workflow:

1. Add wallets you want to monitor.
2. Refresh one wallet, all active wallets, or historical data on demand.
3. Review trades, wallet behavior, market exposure, and signal scores.
4. Export, back up, or alert on the data you care about.

## Highlights

- Wallet watchlist with labels, tags, notes, pinning, archiving, editing, and
  deletion.
- CSV wallet import/export.
- Manual and optional scheduled refreshes from the Polymarket public data API.
- Dashboard summaries for activity, top wallets, top markets, recent trades,
  refresh health, retention, and signal model stats.
- Wallet detail pages with statistics, timelines, market breakdowns, trade
  history, and optional AI summaries.
- Global and wallet-specific trade views with pagination, sorting, filtering,
  date presets, side filters, wallet search, market search, and CSV export.
- Trade detail pages with related trades, signal model context, and optional AI
  analysis.
- Local observed-trade anomaly model with numpy-only inference in the running
  app.
- Admin model-training UI and scripts for local-only TensorFlow training.
- Telegram alert settings, test alerts, and notifications for newly imported
  trades.
- Sync status history, duplicate detection, duplicate cleanup, and operational
  diagnostics.
- Full JSON backup export/import for database moves and restores.
- Health, readiness, schema, sitemap, robots, and operations endpoints.
- Optional password protection, CSRF protection, rate limiting, security
  headers, request IDs, and structured production logging.
- SQLite by default, PostgreSQL supported for hosted or multi-user deployments.

## What It Is Not

- It is not a trading bot.
- It does not execute, recommend, or automate trades.
- It does not call external APIs while rendering normal pages.
- It does not require a frontend build pipeline.
- It is not an accounting system. Trade values and YES/NO summaries are review
  aids, not financial statements.

## Tech Stack

- Python 3.10+
- FastAPI, Starlette, Jinja2
- SQLAlchemy
- SQLite by default, PostgreSQL supported
- Server-rendered HTML, CSS, and lightweight JavaScript
- httpx for Polymarket and provider API calls
- SlowAPI for selected route limits
- APScheduler for optional auto-refresh
- numpy for local signal inference
- Optional Anthropic, Ollama, or Hugging Face analysis providers

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

### Refresh From the Command Line

```powershell
python refresh_now.py
```

This refreshes all watched wallets without using the web UI.

## First Run

1. Open `/wallets`.
2. Add a Polymarket wallet address.
3. Click `Refresh` for that wallet.
4. Open the wallet detail page or `/all-trades` to inspect stored activity.
5. Check `/admin/sync-status` for fetched, inserted, duplicate, skipped, and
   error counts.

## Common Commands

```powershell
# Start the development server
.\start_dev.ps1

# Start with another port
$env:PORT = "8010"
.\start_dev.ps1

# Initialize or migrate the database
python scripts/init_db.py

# Refresh all wallets
python refresh_now.py

# Export a JSON backup
python scripts/export_backup.py

# Import a backup with a dry run first
python scripts/import_backup.py backups/YOUR_BACKUP.json --dry-run
python scripts/import_backup.py backups/YOUR_BACKUP.json --yes

# Run tests
pytest -q
```

## Project Structure

```text
app/
  main.py              FastAPI app creation, middleware, lifespan, scheduler
  settings.py          Environment configuration
  db.py                Engine, sessions, schema initialization, migrations
  models.py            SQLAlchemy models
  ingest.py            Polymarket fetch, normalization, refresh, deduplication
  ai_analysis.py       Local signal and optional LLM analysis integration
  alerts.py            Telegram alert formatting and delivery
  analytics.py         Dashboard and wallet analytics helpers
  retention.py         Retention event logging and metrics
  formatting.py        Template-safe formatting helpers
  queries.py           Reusable trade and wallet query builders
  ml/
    features.py        Leakage-safe feature engineering
    model.py           numpy-only model loading and inference
    scoring.py         On-demand scoring for existing trades
    train.py           Local-only training orchestration
  routes/
    auth.py            Login and logout
    core.py            Dashboard, health, readiness, ops, sitemap
    wallets.py         Wallet CRUD, refresh, import, AI wallet summary
    trades.py          Trade lists, trade detail, AI trade analysis
    exports.py         CSV exports and JSON backup import/export
    alerts.py          Settings, sync status, admin refresh
    retention.py       Retention metrics endpoint
    _shared.py         Shared route helpers
  templates/           Jinja2 pages
  static/              CSS, JavaScript, favicon
scripts/               Database, backup, migration, model, and utility scripts
tests/                 Pytest coverage
data/                  Local SQLite database and exported model weights
backups/               Generated JSON backups
```

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
| `RUNTIME_PLATFORM` | auto-detected | Runtime label such as `local`, `render`, `railway`, or `docker`. |
| `HOST` | `0.0.0.0` | Server bind address used by scripts and deployment. |
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
| `PUBLIC_READ_ONLY` | `false` | Allows public GET access to browsing pages and trained-model trade analysis while keeping admin pages and mutations password-protected. |
| `SESSION_SECRET_KEY` | development default | Session signing secret. Set a strong value for deployment. |
| `SESSION_COOKIE_NAME` | `polysignal_session` | Session cookie name. |
| `SESSION_COOKIE_SECURE` | `true` in production, else `false` | Secure flag for the session cookie. |
| `CSRF_COOKIE_SECURE` | `true` in production, else `false` | Secure flag for the CSRF cookie. |

Security behavior includes session middleware, CSRF middleware for form posts,
security headers, no-store caching for HTML, static asset caching, request IDs,
selected endpoint rate limits, login throttling, and production-safe readiness
checks.

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

## AI and Signal Model

PolySignal can explain trades in two ways:

- A local observed-trade anomaly model scores how unusual each public trade is
  versus the wallet's own prior behavior, using **behavioral and contextual
  signals only** (trading pace, timing gaps, market focus, price extremity, the
  wallet's past outlier rate). It is an anomaly score, not a profit forecast.
- Optional external providers can generate natural-language analysis when
  configured.

Both the local and external analysis are **grounded in the wallet's real
resolved-market track record** (win rate, realized ROI/PnL) when that data
exists, so the verdicts reflect actual outcomes rather than stored value alone.
"Analyze with AI" deep links appear on the trades tables and dashboard activity
(not just the trade detail page) and run the analysis on arrival.

The model is a single sigmoid neuron (the course's `Dense(1, sigmoid)`), trained
locally with TensorFlow and deployed as plain-numpy weights. It is **leakage-safe
by default**: the training label is a 2-sigma threshold on a trade's value
relative to the wallet's history, so the current-trade value features the label
is derived from (`log1p_trade_value`, `log_value_vs_prior_mean`,
`value_zscore_capped`) are **excluded** from the deployed model. Training refuses
to re-introduce them (see `assert_leakage_safe`). Reported metrics are therefore
honest point estimates of skill (test ROC-AUC ~0.7), not the ROC-AUC 1.0 a
leaky model produces by rediscovering its own label.

Provider priority for optional external analysis is Claude, then Ollama, then
Hugging Face.

| Variable | Default | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables Claude-backed analysis. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model name. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL. |
| `OLLAMA_MODEL` | `mistral:latest` | Ollama model name. |
| `OLLAMA_TIMEOUT_SECONDS` | `60.0` | Ollama request timeout. |
| `HUGGINGFACE_API_KEY` | unset | Enables Hugging Face-backed analysis. |
| `AI_CACHE_TTL_HOURS` | `72` | AI cache freshness window. |
| `AI_RATE_LIMIT` | `20/minute` | Rate limit for AI endpoints. |
| `REFRESH_RATE_LIMIT` | `10/minute` | Configured refresh rate-limit value. |

See [AI_SETUP.md](AI_SETUP.md) for provider setup notes and
[AI_EXAMPLES.py](AI_EXAMPLES.py) for examples.

### Training the Local Model

The running app does not import TensorFlow (never add it to
`requirements.txt`). Training is local-only and exports lightweight weights to
`data/model_weights.json`. Install TensorFlow locally first
(`pip install tensorflow`).

```powershell
# Default: leakage-safe "improved" model (BCE + class weights), deployed
python scripts/train_model.py
python scripts/score_all_trades.py --overwrite

# Exact lecture math (single sigmoid neuron, MSE + SGD 0.1, no class weights)
python scripts/train_model.py --lecture --output data/model_weights.lecture.json

# Leaky baseline on all 15 features (comparison only - do NOT deploy)
python scripts/train_model.py --leaked --output data/model_weights.leaked.json
```

To deploy a different model, copy its file over `data/model_weights.json` (the
only file the app loads) and restart. The app starts and runs normally **without**
any weights file — scoring is simply disabled.

Use `/admin/train-model` to run training from the admin UI when local training
dependencies are available; that page also shows the feature set, the honest
metrics, and a precision/recall-vs-threshold table.

## Resolved-Market Performance (real PnL / ROI / win rate)

Wallet pages and the leaderboard show **realized** PnL, ROI, win rate, and
markets won/lost — computed only from markets that have actually resolved, never
fabricated. Until a wallet has resolved-market data, the UI shows an honest
"Not enough resolved market data yet" placeholder.

How it works:

- Each trade stores the Yes/No `outcome_token` it was on (orthogonal to `side`,
  which encodes buy/sell). Resolved outcomes are fetched per market from the
  Polymarket CLOB API (`/markets/{condition_id}`, the token with `winner=true`)
  and stored in `market_resolutions`.
- PnL uses a transparent **net-position-held-to-resolution** model per
  market × token: `pnl = sell_proceeds − buy_cost + max(net_shares, 0) × (1 if
  the token won else 0)`. ROI is over gross buy cost; a market counts as won or
  lost by the sign of its summed PnL.
- Resolution fetching is wired into wallet refresh but fully guarded: if it
  fails, the refresh still succeeds and stored trades are unaffected.

Backfill existing data (the running app also fills this in over time on refresh):

```powershell
# Backfill outcome tokens (re-fetches trades) and resolve traded markets
python scripts/backfill_resolutions.py

# Scope to one wallet / cap the number of markets resolved per run
python scripts/backfill_resolutions.py --wallet 0x... --limit 200
```

## Telegram Alerts

Telegram settings are stored in the `app_settings` database table:

- Bot token
- Chat ID
- Minimum trade size (default: `$1000` of `price * size`)
- Alerts enabled or disabled

You can edit these any time in `/settings`. On startup the app also **seeds**
working credentials so a plain deploy is self-configuring: it reads
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from the environment when set,
otherwise falls back to the baked-in defaults, enables alerts, and applies the
`$1000` minimum. Prefer the environment variables in any real deployment so the
token is not committed in source and can be rotated without a code change.

On the **first** time alerts are configured, the existing trade backlog is
marked as already-alerted so going live never floods the chat with historical
trades on the first refresh — only trades ingested afterward can fire an alert.
Alerts are sent during wallet refresh, capped per wallet per refresh, deduped so
each trade alerts at most once, and skipped for trades older than 24 hours.

Alert credentials live in the database. Protect database access, backup files,
and filesystem permissions in any deployment.

## Main Routes

### Pages

- `GET /` redirects to `/wallets`
- `GET /login`
- `GET /logout`
- `GET /tutorial`
- `GET /wallets`
- `GET /wallets/import`
- `GET /wallets/{identifier}`
- `GET /wallets/{identifier}/edit`
- `GET /wallets/{identifier}/delete-confirm`
- `GET /wallets/{identifier}/trades`
- `GET /leaderboard`
- `GET /all-trades`
- `GET /trades/{trade_id}`
- `GET /dashboard`
- `GET /settings`
- `GET /admin/sync-status`
- `GET /admin/ops-ui`
- `GET /admin/import-backup`
- `GET /admin/train-model`

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
- `POST /admin/train-model`
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

- `wallets`: watched wallet metadata and refresh state.
- `trades`: normalized Polymarket trade records keyed by unique `trade_id`.
- `sync_events`: refresh status history, counts, duplicates, duration, and
  errors.
- `app_settings`: Telegram alert settings.
- `trade_analysis`: cached AI trade analysis.
- `event_log`: raw retention event stream.
- `retention_daily` and `retention_weekly`: precomputed retention summaries.
- `schema_migrations`: applied lightweight migration versions.

Important behavior:

- Normal page rendering reads from the database only.
- External Polymarket API calls happen in refresh flows and scheduler jobs.
- Trade deduplication is based on `trade_id`; cleanup tooling can also remove
  semantic duplicates.
- Startup applies compatibility migrations automatically.
- Old sync events are pruned during startup maintenance.

## Backups and Restore

Export a full JSON backup while logged in:

```text
/admin/backup.json
```

Or export from the command line:

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

See [RENDER_DEPLOY.md](RENDER_DEPLOY.md) and [render.yaml](render.yaml).

Use Render Postgres for persistent data. Do not rely on SQLite storage on Render
free web services.

### Railway

See [RAILWAY_SETUP.md](RAILWAY_SETUP.md) and [railway.json](railway.json).

### Free Public Access

See [FREE_HOSTING.md](FREE_HOSTING.md). A simple no-payment option is running
locally with the default SQLite database and exposing it through a Cloudflare
Tunnel.

## Production Checklist

- Set `APP_ENV=production`.
- Set `DASHBOARD_PASSWORD`.
- Set a strong `SESSION_SECRET_KEY` with at least 32 characters.
- Use PostgreSQL for hosted deployments.
- Confirm `DATABASE_URL` is reachable.
- Keep `SESSION_COOKIE_SECURE=true` and `CSRF_COOKIE_SECURE=true` behind HTTPS.
- Protect `data/`, `backups/`, logs, and environment variables.
- Check `/readyz`, `/healthz`, and `/admin/ops` after deploy.

## Testing

Run all tests:

```powershell
pytest -q
```

Coverage includes:

- App factory, lifespan, middleware, and readiness behavior
- Authentication, CSRF, security headers, and rate-limit contracts
- Wallet and trade route behavior
- CSV export and backup behavior
- Refresh, ingestion, sync status, duplicate handling, and alerts
- AI analysis availability and caching
- Local signal model feature engineering and numpy inference
- Admin model-training guardrails
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
- Check `/admin/sync-status` for fetched, inserted, duplicate, skipped, and
  error counts.

### Archived Wallets Missing

- Enable the archived-wallet filter on `/wallets`.

### Polymarket Refresh Failures

- Verify outbound access to `https://data-api.polymarket.com`.
- Check `/admin/sync-status` for timeout, rate-limit, or API errors.
- Try a lower refresh `limit` if refreshes are slow.

### AI Analysis Is Unavailable

- The local model requires `data/model_weights.json` before it can score.
- External analysis requires at least one configured provider.
- Use `debug_ollama.py` to check local Ollama reachability.

### Production Startup Fails

- Set `DASHBOARD_PASSWORD`.
- Set a strong `SESSION_SECRET_KEY`.
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
- Keep TensorFlow out of application imports and deployment requirements.
- Prefer focused tests for route, migration, security, model, and ingestion
  changes.
