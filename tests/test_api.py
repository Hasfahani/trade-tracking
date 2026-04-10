from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.models import Base, Notification, Trade, Wallet
from app.routes_v2 import router


def build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(router)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), TestingSessionLocal


def seed_dashboard_data(session_factory):
    db = session_factory()
    now = datetime.now(timezone.utc)
    wallet_a = Wallet(address="0xaaa", label="Alpha", is_pinned=1)
    wallet_b = Wallet(address="0xbbb", label="Beta")
    db.add_all([wallet_a, wallet_b])
    db.flush()
    db.add_all(
        [
            Trade(
                wallet_address="0xaaa",
                trade_id="t1",
                condition_id="c1",
                market_title="Election",
                side="YES",
                price=0.60,
                size=100,
                traded_at=now,
            ),
            Trade(
                wallet_address="0xaaa",
                trade_id="t2",
                condition_id="c1",
                market_title="Election",
                side="NO",
                price=0.40,
                size=25,
                traded_at=now - timedelta(hours=1),
            ),
            Trade(
                wallet_address="0xbbb",
                trade_id="t3",
                condition_id="c2",
                market_title="Sports",
                side="YES",
                price=0.70,
                size=50,
                traded_at=now - timedelta(days=2),
            ),
            Notification(
                wallet_address="0xaaa",
                trade_id="t1",
                message="Test alert",
                created_at=now,
                is_read=0,
            ),
        ]
    )
    db.commit()
    db.close()


def test_api_stats_returns_dashboard_summary():
    client, session_factory = build_client()
    seed_dashboard_data(session_factory)

    response = client.get("/api/stats")
    assert response.status_code == 200

    payload = response.json()
    assert payload["total_wallets"] == 2
    assert payload["total_trades"] == 3
    assert payload["trades_today"] == 2
    assert payload["most_active_wallet"] == "0xaaa"
    assert payload["most_active_wallet_count"] == 2
    assert payload["biggest_trade"]["trade_id"] == "t1"
    assert len(payload["recent_alerts"]) == 1


def test_wallet_refresh_endpoint_returns_structured_payload(monkeypatch):
    client, session_factory = build_client()
    db = session_factory()
    wallet = Wallet(address="0xrefresh", label="Refresh")
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    wallet_id = wallet.id
    db.close()

    def fake_refresh_wallet(db, wallet, **kwargs):
        wallet.last_checked_at = datetime(2026, 4, 9, tzinfo=timezone.utc)
        wallet.last_refresh_count = 3
        db.commit()
        return {
            "wallet": wallet.address,
            "fetched": 3,
            "inserted": 3,
            "stats": {
                "total_trades": 3,
                "total_volume": 42,
                "unique_markets": 1,
                "yes_trades": 2,
                "no_trades": 1,
                "avg_trade_size": 14,
                "last_trade_date": "2026-04-09T00:00:00+00:00",
            },
            "last_checked_at": "2026-04-09T00:00:00+00:00",
            "error": None,
        }

    monkeypatch.setattr("app.routes_v2.refresh_wallet", fake_refresh_wallet)

    response = client.post(f"/api/wallet/{wallet_id}/refresh")
    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "success"
    assert payload["wallet_id"] == wallet_id
    assert payload["inserted"] == 3
    assert payload["stats"]["total_trades"] == 3
