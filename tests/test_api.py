from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.ingest import normalize_trade, refresh_wallet
from app.models import Base, Trade, Wallet
from app.routes_v2 import router


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
