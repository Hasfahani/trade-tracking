# Summary: Tests the startup trade seed and the leaderboard template render.
# Details: Guards against (a) the trades seed not populating an empty DB and
# (b) the _leaderboard.html partial regressing into a self-import recursion.
"""Seed loader + leaderboard render regression tests."""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base, Trade
from app.seed_trades import seed_trades
from app.watchlist_seed import seed_watchlist_wallets


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_seed_populates_empty_db_from_real_file():
    factory = _factory()
    db = factory()
    try:
        seed_watchlist_wallets(db)
        db.flush()
        result = seed_trades(db)
        db.commit()
        assert result["skipped"] is False
        assert result["inserted_trades"] > 0
        assert result["inserted_resolutions"] > 0
        assert db.query(func.count(Trade.id)).scalar() == result["inserted_trades"]
    finally:
        db.close()


def test_seed_is_idempotent_and_skips_when_trades_exist():
    factory = _factory()
    db = factory()
    try:
        seed_watchlist_wallets(db)
        db.flush()
        first = seed_trades(db)
        db.commit()
        assert first["inserted_trades"] > 0

        second = seed_trades(db)
        db.commit()
        assert second["skipped"] is True
        assert second["inserted_trades"] == 0
        # No duplication: count unchanged after the second call.
        assert db.query(func.count(Trade.id)).scalar() == first["inserted_trades"]
    finally:
        db.close()


def test_leaderboard_renders_with_seeded_data_no_recursion():
    """The _leaderboard.html partial must render real rows, not 500 (self-import)."""
    factory = _factory()
    seed_db = factory()
    try:
        seed_watchlist_wallets(seed_db)
        seed_db.flush()
        seed_trades(seed_db)
        seed_db.commit()
    finally:
        seed_db.close()

    app = create_app(lifespan_context=None, csrf_enabled=False)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    client = TestClient(app)

    response = client.get("/leaderboard")
    assert response.status_code == 200
    assert "rank-badge" in response.text
    assert "Leaderboard not available yet" not in response.text
