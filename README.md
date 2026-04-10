# Polymarket Trades Watchlist V1

A minimal, server-rendered web app to track Polymarket wallet trades. No analytics, no PnL, no positions - just trades storage and viewing.

## Features

- Add/remove wallet addresses with optional labels
- Fetch and store trades in local SQLite database (append-only)
- View paginated trades per wallet
- Mobile-friendly responsive design
- Server-rendered HTML (no JavaScript frontend)

## Hard Rules

- **NO external API calls during page render** - all page loads read from SQLite only
- **NO positions, PnL, profit, win/loss, analytics, or copy trading**
- Ingestion is isolated from web routes (see `app/ingest.py`)
- Trade deduplication via unique `trade_id`

## Setup

### 1. Create virtual environment

```bash
python -m venv venv
```

### 2. Activate virtual environment

Windows:
```bash
venv\Scripts\activate
```

Linux/Mac:
```bash
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Initialize database

```bash
python scripts/init_db.py
```

This creates `./data/app.db` with the required schema.

## Run

```bash
uvicorn app.main:app --reload
```

Server runs at: http://localhost:8000

## Usage

### 1. Add wallets

- Navigate to http://localhost:8000/wallets
- Enter wallet address (required) and optional label
- Click "Add Wallet"

### 2. Fetch trades

Trades are NOT fetched automatically. You must manually trigger ingestion:

```bash
# Refresh all wallets (limit 200 trades per wallet)
curl -X POST http://localhost:8000/admin/refresh?limit_per_wallet=200

# Refresh specific wallet
curl -X POST "http://localhost:8000/admin/refresh?address=0x..."
```

Returns JSON stats: `{"status": "success", "wallets_refreshed": N, "results": {...}}`

### 3. View trades

- Click on any wallet in the list
- View paginated trades (50 per page by default)
- Trades display: timestamp (UTC), market title, side (YES/NO), price, size

### 4. Delete wallet

- Click "Delete" button on wallet list
- Deletes wallet and all associated trades

## Database Schema

### wallets
- `id` - Primary key
- `address` - Unique wallet address (lowercase)
- `label` - Optional label
- `created_at` - Timestamp

### trades
- `id` - Primary key
- `wallet_address` - Foreign reference to wallet
- `trade_id` - Unique trade identifier (prevents duplicates)
- `condition_id` - Market identifier
- `market_title` - Market name (if available)
- `side` - 'YES' or 'NO'
- `price` - Trade price
- `size` - Trade size
- `traded_at` - Trade timestamp (UTC)
- `inserted_at` - Record creation timestamp

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | / | Redirect to /wallets |
| GET | /wallets | List all wallets + add form |
| POST | /wallets | Add new wallet |
| POST | /wallets/{address}/delete | Delete wallet + trades |
| GET | /wallets/{address}/trades | View paginated trades |
| POST | /admin/refresh | Fetch & store trades |

## Important Notes

### Polymarket API

The ingestion code in `app/ingest.py` contains a **placeholder** for the Polymarket API endpoint. The actual API URL and response format need to be verified and updated:

```python
BASE_URL = "https://api.polymarket.com/v1"  # TODO: Verify actual endpoint
```

You must update:
1. `fetch_trades_for_wallet()` - API endpoint and parameters
2. `normalize_trade()` - Field mappings based on actual API response

### Trade Deduplication

Trades are deduplicated using `trade_id`:
- If the API provides an `id`, that is used
- Otherwise, a deterministic hash is computed from (wallet, condition_id, side, price, size, timestamp)

### NO Real-time Updates

This is V1 - manual refresh only. No background jobs, no websockets, no automatic updates.

### Page Load Performance

All page renders read **exclusively from SQLite**. No external API calls during page load. This ensures fast, reliable page loads.

## Project Structure

```
polymarket-trades-v1/
├── app/
│   ├── main.py           # FastAPI app initialization
│   ├── routes.py         # Web routes and handlers
│   ├── db.py             # Database session management
│   ├── models.py         # SQLAlchemy models
│   ├── ingest.py         # Trade fetching and ingestion
│   ├── settings.py       # Configuration
│   ├── templates/        # Jinja2 templates
│   │   ├── base.html
│   │   ├── wallets.html
│   │   └── trades.html
│   └── static/
│       └── style.css     # Mobile-friendly CSS
├── scripts/
│   └── init_db.py        # Database initialization
├── data/                 # SQLite database (created at runtime)
│   └── app.db
├── requirements.txt
└── README.md
```

## Technology Stack

- Python 3.11+
- FastAPI
- SQLAlchemy (ORM)
- SQLite
- Jinja2 templates
- httpx (for API requests)
- Uvicorn (ASGI server)

## What's NOT Included (by design)

- Authentication/users
- Background job schedulers
- Database migrations
- Docker/containers
- Caching layers
- WebSockets
- React/Vue/frontend frameworks
- Positions tracking
- PnL calculations
- Win/loss ratios
- Copy trading execution
- Strategy analysis
- Real-time updates

Keep it simple. Keep it boring. Keep it correct.
