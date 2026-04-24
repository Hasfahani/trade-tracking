from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.ingest import _fetch_trade_batch, normalize_trade, refresh_wallet
from app.models import Base, SyncEvent, Trade, Wallet
from app.routes_v2 import router
from app.watchlist_seed import SeedWallet, seed_watchlist_wallets


def build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(router)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), testing_session


def test_add_wallet_rejects_invalid_address():
    client, _ = build_client()
    response = client.post(
        "/wallets",
        data={"address": "bad-address", "label": "bad"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "level=error" in response.headers["location"]


def test_add_wallet_rejects_duplicate_address():
    client, session_factory = build_client()

    db = session_factory()
    db.add(Wallet(address="0x1111111111111111111111111111111111111111", label="seed"))
    db.commit()
    db.close()

    response = client.post(
        "/wallets",
        data={"address": "0x1111111111111111111111111111111111111111", "label": "dup"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Wallet%20already%20exists" in response.headers["location"]


def test_add_wallet_persists_tags_and_notes():
    client, session_factory = build_client()

    response = client.post(
        "/wallets",
        data={
            "address": "0x7777777777777777777777777777777777777777",
            "label": "Desk",
            "tags": "desk, whales, desk",
            "notes": "Primary watchlist wallet",
        },
        follow_redirects=False,
    )

    db = session_factory()
    wallet = db.query(Wallet).filter(Wallet.address == "0x7777777777777777777777777777777777777777").first()
    db.close()

    assert response.status_code == 303
    assert wallet is not None
    assert wallet.tags == "desk, whales"
    assert wallet.notes == "Primary watchlist wallet"


def test_normalize_trade_maps_buy_sell_and_validates():
    buy = normalize_trade(
        {
            "transactionHash": "0xabc",
            "asset": "x",
            "conditionId": "cond1",
            "title": "Market",
            "side": "BUY",
            "price": 0.52,
            "size": 10,
            "timestamp": 1713139200,
        },
        "0x2222222222222222222222222222222222222222",
    )
    sell = normalize_trade(
        {
            "transactionHash": "0xdef",
            "asset": "x",
            "conditionId": "cond1",
            "title": "Market",
            "side": "SELL",
            "price": 0.47,
            "size": 6,
            "timestamp": 1713139300,
        },
        "0x2222222222222222222222222222222222222222",
    )
    invalid = normalize_trade(
        {
            "transactionHash": "",
            "asset": "x",
            "conditionId": "cond1",
            "price": 0,
            "size": 0,
        },
        "0x2222222222222222222222222222222222222222",
    )

    assert buy is not None and buy["side"] == "YES"
    assert sell is not None and sell["side"] == "NO"
    assert invalid is None


def test_normalize_trade_prefers_external_id_and_iso_timestamp():
    normalized = normalize_trade(
        {
            "id": "pm-trade-123",
            "transactionHash": "0xabc",
            "asset": "asset-1",
            "conditionId": "cond-iso",
            "title": "ISO Market",
            "outcome": "NO",
            "price": 0.44,
            "size": 12,
            "timestamp": "2024-04-15T12:30:00Z",
        },
        "0x9999999999999999999999999999999999999999",
    )

    assert normalized is not None
    assert normalized["id"] == "pm-trade-123"
    assert normalized["side"] == "NO"
    assert normalized["traded_at"].isoformat() == "2024-04-15T12:30:00+00:00"


def test_refresh_wallet_deduplicates_trade_id(monkeypatch):
    _, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0x3333333333333333333333333333333333333333")
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    raw_trade = {
        "transactionHash": "0xsame",
        "asset": "asset",
        "conditionId": "c1",
        "title": "Election",
        "side": "BUY",
        "price": 0.55,
        "size": 3,
        "timestamp": 1713139200,
    }

    monkeypatch.setattr("app.ingest.fetch_trades_for_wallet", lambda *args, **kwargs: [raw_trade])

    first = refresh_wallet(db, wallet)
    second = refresh_wallet(db, wallet)

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert db.query(Trade).filter(Trade.wallet_address == wallet.address).count() == 1


def test_fetch_trade_batch_uses_local_ssl_context_and_fast_timeouts(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class DummyClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return DummyResponse()

    monkeypatch.setattr("app.ingest.httpx.Client", DummyClient)

    result = _fetch_trade_batch("0xabc", limit=25, offset=50)

    assert result == []
    assert captured["url"].endswith("/trades")
    assert captured["params"] == {"user": "0xabc", "limit": 25, "offset": 50}
    assert captured["verify"].check_hostname is True
    assert captured["timeout"].connect == 5.0
    assert captured["timeout"].read == 15.0


def test_refresh_route_surfaces_fetch_counts(monkeypatch):
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0x5555555555555555555555555555555555555555", label="Ops")
    db.add(wallet)
    db.commit()
    wallet_address = wallet.address
    db.close()

    monkeypatch.setattr(
        "app.routes_v2.refresh_wallet",
        lambda *args, **kwargs: {
            "wallet": wallet_address,
            "status": "success",
            "fetched": 7,
            "inserted": 3,
            "duplicates": 4,
            "error": None,
            "stats": {"total_trades": 3, "last_trade_date": None},
            "last_checked_at": None,
        },
    )

    response = client.post(f"/wallets/{wallet_address}/refresh", follow_redirects=False)

    assert response.status_code == 303
    assert "Added%203%20new%20trades%20from%207%20fetched%20records" in response.headers["location"]


def test_wallet_and_trade_routes_render():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0x4444444444444444444444444444444444444444", label="Desk")
    db.add(wallet)
    db.flush()
    db.add(
        Trade(
            wallet_address=wallet.address,
            trade_id="trade-1",
            condition_id="cond-1",
            market_title="Will X happen?",
            side="YES",
            price=0.61,
            size=14,
            traded_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    wallet_address = wallet.address
    db.close()

    wallets_response = client.get("/wallets")
    trades_response = client.get(f"/wallets/{wallet_address}/trades")

    assert wallets_response.status_code == 200
    assert "Tracked wallets" in wallets_response.text
    assert trades_response.status_code == 200
    assert "Will X happen?" in trades_response.text


def test_delete_confirm_route_renders_warning():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0x6666666666666666666666666666666666666666", label="Risk")
    db.add(wallet)
    db.commit()
    wallet_address = wallet.address
    db.close()

    response = client.get(f"/wallets/{wallet_address}/delete-confirm")

    assert response.status_code == 200
    assert "Type DELETE to confirm" in response.text


def test_wallet_archive_hides_wallet_from_default_list():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0x8888888888888888888888888888888888888888", label="Archive Me")
    db.add(wallet)
    db.commit()
    wallet_address = wallet.address
    db.close()

    archive_response = client.post(f"/wallets/{wallet_address}/archive", follow_redirects=False)
    wallets_response = client.get("/wallets")
    archived_response = client.get("/wallets?include_archived=1&status_filter=archived")

    assert archive_response.status_code == 303
    assert "Archive Me" not in wallets_response.text
    assert "Archive Me" in archived_response.text


def test_wallet_edit_updates_notes_tags_and_pin():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", label="Before")
    db.add(wallet)
    db.commit()
    wallet_address = wallet.address
    db.close()

    response = client.post(
        f"/wallets/{wallet_address}/edit",
        data={
            "label": "After",
            "tags": "ops, archive",
            "notes": "Updated note",
            "is_pinned": "1",
        },
        follow_redirects=False,
    )

    db = session_factory()
    wallet = db.query(Wallet).filter(Wallet.address == wallet_address).first()
    db.close()

    assert response.status_code == 303
    assert wallet is not None
    assert wallet.label == "After"
    assert wallet.tags == "ops, archive"
    assert wallet.notes == "Updated note"
    assert wallet.is_pinned == 1


def test_trades_date_preset_filters_query_results():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", label="Preset")
    db.add(wallet)
    db.flush()
    db.add(
        Trade(
            wallet_address=wallet.address,
            trade_id="trade-recent",
            condition_id="cond-recent",
            market_title="Recent Market",
            side="YES",
            price=0.5,
            size=10,
            traded_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        Trade(
            wallet_address=wallet.address,
            trade_id="trade-old",
            condition_id="cond-old",
            market_title="Old Market",
            side="NO",
            price=0.4,
            size=8,
            traded_at=datetime.now(timezone.utc) - timedelta(days=45),
        )
    )
    db.commit()
    wallet_address = wallet.address
    db.close()

    response = client.get(f"/wallets/{wallet_address}/trades?date_preset=7d")

    assert response.status_code == 200
    assert "Recent Market" in response.text
    assert "Showing 1-1 of 1 trades" in response.text


def test_trades_page_shows_wallet_activity_timeline():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0xdddddddddddddddddddddddddddddddddddddddd", label="Timeline")
    db.add(wallet)
    db.flush()
    db.add(
        Trade(
            wallet_address=wallet.address,
            trade_id="trade-timeline",
            condition_id="cond-timeline",
            market_title="Timeline Market",
            side="YES",
            price=0.55,
            size=4,
            traded_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        SyncEvent(
            wallet_address=wallet.address,
            status="success",
            fetched_count=3,
            inserted_count=1,
            duplicate_count=2,
            duration_ms=250,
        )
    )
    db.commit()
    wallet_address = wallet.address
    db.close()

    response = client.get(f"/wallets/{wallet_address}/trades")

    assert response.status_code == 200
    assert "Recent activity" in response.text
    assert "Timeline Market" in response.text
    assert "Refresh success" in response.text


def test_seed_watchlist_wallets_is_idempotent():
    _, session_factory = build_client()
    db = session_factory()

    wallets = [
        SeedWallet(
            address="0x1234567890abcdef1234567890abcdef12345678",
            label="Wallet One",
            tags="alpha, beta",
            notes="First seed",
        ),
        SeedWallet(
            address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            label="Wallet Two",
            tags="gamma",
            notes="Second seed",
        ),
    ]

    first = seed_watchlist_wallets(db, wallets)
    db.commit()
    second = seed_watchlist_wallets(db, wallets)
    db.commit()

    stored = db.query(Wallet).order_by(Wallet.address.asc()).all()
    db.close()

    assert first == {"added": 2, "updated": 0, "total": 2}
    assert second == {"added": 0, "updated": 0, "total": 2}
    assert len(stored) == 2
    assert stored[0].notes is not None


def test_core_routes_smoke_render_and_actions():
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(
        address="0x1234567890abcdef1234567890abcdef12345678",
        label="Theo",
        tags="desk",
        notes="seed wallet",
    )
    db.add(wallet)
    db.flush()
    db.add(
        Trade(
            wallet_address=wallet.address,
            trade_id="trade-smoke-1",
            condition_id="cond-smoke-1",
            market_title="Will it rain?",
            side="YES",
            price=0.61,
            size=14,
            traded_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        SyncEvent(
            wallet_address=wallet.address,
            status="success",
            fetched_count=3,
            inserted_count=1,
            duplicate_count=2,
            duration_ms=120,
        )
    )
    db.commit()
    wallet_address = wallet.address
    db.close()

    checks = [
        ("GET", "/wallets", 200, "text/html"),
        ("GET", "/wallets?status_filter=fresh", 200, "text/html"),
        ("GET", f"/wallets/{wallet_address}/trades", 200, "text/html"),
        ("GET", f"/wallets/{wallet_address}/trades?date_preset=7d", 200, "text/html"),
        ("GET", f"/wallets/{wallet_address}/edit", 200, "text/html"),
        ("GET", f"/wallets/{wallet_address}/delete-confirm", 200, "text/html"),
        ("GET", "/all-trades", 200, "text/html"),
        ("GET", "/trades/trade-smoke-1", 200, "text/html"),
        ("GET", f"/wallets/{wallet_address}/trades/export", 200, "text/csv"),
        ("GET", "/all-trades/export", 200, "text/csv"),
        ("GET", "/admin/sync-status", 200, "text/html"),
        ("POST", "/admin/sync-status/cleanup", 303, ""),
        ("POST", f"/wallets/{wallet_address}/pin", 303, ""),
        ("POST", f"/wallets/{wallet_address}/archive", 303, ""),
        ("POST", f"/wallets/{wallet_address}/unarchive", 303, ""),
    ]

    for method, path, expected_status, expected_content_type in checks:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == expected_status
        if expected_content_type:
            assert expected_content_type in response.headers.get("content-type", "")

    trades_response = client.get(f"/wallets/{wallet_address}/trades")
    sync_response = client.get("/admin/sync-status")
    assert "YES | $0.6100 | 14.00" in trades_response.text
    assert "No semantic duplicates found." in sync_response.text
