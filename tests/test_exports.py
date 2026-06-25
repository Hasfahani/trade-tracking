# Summary: Tests exports and backups.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for CSV export routes in app/routes/exports.py."""
import csv
import io
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import AppSettings, Base, SyncEvent, Trade, Wallet


@pytest.fixture()
def client_and_session():
    return _build_client_and_session()


def _build_client_and_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = create_app(lifespan_context=None, csrf_enabled=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), session_factory


def _seed(session_factory):
    db = session_factory()
    wallet = Wallet(
        address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        label="Export Wallet",
        tags="alpha, beta",
        notes="export note",
        is_pinned=1,
    )
    db.add(wallet)
    db.flush()
    trades = [
        Trade(
            wallet_address=wallet.address,
            trade_id="exp-yes",
            condition_id="exp-cond",
            market_title="Export Market YES",
            side="YES",
            price=0.6,
            size=10.0,
            traded_at=datetime(2026, 3, 1, 12, 0),
        ),
        Trade(
            wallet_address=wallet.address,
            trade_id="exp-no",
            condition_id="exp-cond",
            market_title="Export Market NO",
            side="NO",
            price=0.4,
            size=20.0,
            traded_at=datetime(2026, 3, 2, 12, 0),
        ),
    ]
    db.add_all(trades)
    db.add(
        SyncEvent(
            wallet_address=wallet.address,
            status="success",
            fetched_count=2,
            inserted_count=2,
            duplicate_count=0,
            duration_ms=123,
        )
    )
    db.add(AppSettings(id=1, telegram_chat_id="123", alert_min_size=10.0, alerts_enabled=1))
    db.commit()
    address = wallet.address
    db.close()
    return address


class TestWalletCSVExport:
    def test_returns_200_with_csv_content_type(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/wallets/export")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_bom_present_for_excel_compatibility(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/wallets/export")
        assert resp.content.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM missing"

    def test_correct_headers(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/wallets/export")
        reader = csv.reader(io.StringIO(resp.text.lstrip("\ufeff")))
        headers = next(reader)
        assert headers == ["address", "label", "tags", "notes", "is_pinned", "is_archived", "created_at"]

    def test_wallet_data_present_in_rows(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/wallets/export")
        assert "Export Wallet" in resp.text
        assert "alpha;beta" in resp.text
        assert "export note" in resp.text

    def test_empty_db_returns_headers_only(self, client_and_session):
        client, _ = client_and_session
        resp = client.get("/wallets/export")
        assert resp.status_code == 200
        lines = [l for l in resp.text.lstrip("\ufeff").splitlines() if l]
        assert len(lines) == 1  # header only


class TestWalletTradesCSVExport:
    def test_returns_csv_with_correct_headers(self, client_and_session):
        client, sf = client_and_session
        address = _seed(sf)
        resp = client.get(f"/wallets/{address}/trades/export")
        assert resp.status_code == 200
        reader = csv.reader(io.StringIO(resp.text.lstrip("\ufeff")))
        headers = next(reader)
        assert "Trade ID" in headers
        assert "Side" in headers
        assert "Value" in headers

    def test_bom_present(self, client_and_session):
        client, sf = client_and_session
        address = _seed(sf)
        resp = client.get(f"/wallets/{address}/trades/export")
        assert resp.content.startswith(b"\xef\xbb\xbf")

    def test_all_trades_returned_by_default(self, client_and_session):
        client, sf = client_and_session
        address = _seed(sf)
        resp = client.get(f"/wallets/{address}/trades/export")
        assert "exp-yes" in resp.text
        assert "exp-no" in resp.text

    def test_side_filter_applied(self, client_and_session):
        client, sf = client_and_session
        address = _seed(sf)
        resp = client.get(f"/wallets/{address}/trades/export?side=YES")
        assert "exp-yes" in resp.text
        assert "exp-no" not in resp.text

    def test_empty_wallet_returns_headers_only(self, client_and_session):
        client, sf = client_and_session
        db = sf()
        empty = Wallet(address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        db.add(empty)
        db.commit()
        empty_address = empty.address
        db.close()
        resp = client.get(f"/wallets/{empty_address}/trades/export")
        assert resp.status_code == 200
        lines = [l for l in resp.text.lstrip("\ufeff").splitlines() if l]
        assert len(lines) == 1  # header only


class TestAllTradesCSVExport:
    def test_returns_csv_with_bom(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/all-trades/export")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\xef\xbb\xbf")

    def test_correct_headers(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/all-trades/export")
        reader = csv.reader(io.StringIO(resp.text.lstrip("\ufeff")))
        headers = next(reader)
        assert "Trade ID" in headers
        assert "Wallet" in headers
        assert "Market Title" in headers

    def test_wallet_label_used_in_wallet_column(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/all-trades/export")
        assert "Export Wallet" in resp.text

    def test_side_filter_reduces_output(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        resp = client.get("/all-trades/export?side=YES")
        assert "exp-yes" in resp.text
        assert "exp-no" not in resp.text

    def test_empty_db_returns_headers_only(self, client_and_session):
        client, _ = client_and_session
        resp = client.get("/all-trades/export")
        assert resp.status_code == 200
        lines = [l for l in resp.text.lstrip("\ufeff").splitlines() if l]
        assert len(lines) == 1


class TestFullJSONBackup:
    def test_backup_returns_downloadable_json_with_checksum(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)

        resp = client.get("/admin/backup.json")

        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        assert "attachment" in resp.headers["content-disposition"]
        assert len(resp.headers["X-Backup-Checksum-SHA256"]) == 64

    def test_backup_contains_all_core_tables_and_counts(self, client_and_session):
        client, sf = client_and_session
        address = _seed(sf)

        payload = json.loads(client.get("/admin/backup.json").text)

        assert payload["format"] == "polysignal.backup"
        assert payload["format_version"] == 1
        assert payload["counts"]["wallets"] == 1
        assert payload["counts"]["trades"] == 2
        assert payload["counts"]["sync_events"] == 1
        assert payload["counts"]["app_settings"] == 1
        assert payload["tables"]["wallets"][0]["address"] == address
        assert payload["tables"]["trades"][0]["trade_id"] == "exp-yes"


class TestFullJSONImport:
    def test_import_page_renders(self, client_and_session):
        client, _ = client_and_session

        resp = client.get("/admin/import-backup")

        assert resp.status_code == 200
        assert "Import Backup" in resp.text
        assert "Upload JSON Backup" in resp.text

    def test_import_backup_inserts_rows_and_skips_duplicates(self, client_and_session):
        client, sf = client_and_session
        _seed(sf)
        payload = client.get("/admin/backup.json").json()

        empty_client, empty_sf = _build_client_and_session()
        upload = {
            "backup_file": (
                "backup.json",
                json.dumps(payload).encode("utf-8"),
                "application/json",
            )
        }

        first = empty_client.post("/admin/import-backup", files=upload)
        second = empty_client.post("/admin/import-backup", files=upload)

        assert first.status_code == 200
        assert "Inserted 5 new rows" in first.text
        assert second.status_code == 200
        assert "Inserted 0 new rows" in second.text
        db = empty_sf()
        try:
            assert db.query(Wallet).count() == 1
            assert db.query(Trade).count() == 2
            assert db.query(SyncEvent).count() == 1
            assert db.query(AppSettings).count() == 1
        finally:
            db.close()
