# Summary: Tests Telegram alerts.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for Telegram alert logic in app/alerts.py."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import alerts
from app.models import AppSettings, Base, Trade, Wallet


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _wallet(db, address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", label=None):
    w = Wallet(address=address, label=label)
    db.add(w)
    db.commit()
    db.refresh(w)
    return w


def _settings(db, *, enabled=1, token="bot-token", chat_id="chat-123", min_size=100.0):
    s = AppSettings(
        id=1,
        alerts_enabled=enabled,
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        alert_min_size=min_size,
    )
    db.add(s)
    db.commit()
    return s


def _trade(db, wallet_address, trade_id, *, price=0.5, size=300.0, hours_ago=0, alert_sent=0):
    t = Trade(
        wallet_address=wallet_address,
        trade_id=trade_id,
        condition_id="cond-1",
        market_title="Test Market",
        side="YES",
        price=price,
        size=size,
        traded_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago),
        alert_sent=alert_sent,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


class TestGetAppSettings:
    def test_creates_defaults_when_missing(self, db):
        settings = alerts.get_app_settings(db)
        assert settings.id == 1
        assert settings.alerts_enabled == 0

    def test_returns_existing_row(self, db):
        _settings(db, enabled=1, token="tok", chat_id="chat")
        settings = alerts.get_app_settings(db)
        assert settings.telegram_bot_token == "tok"


class TestSendTelegramMessage:
    def test_returns_true_on_success(self):
        mock_resp = MagicMock()
        mock_resp.is_success = True
        with patch("app.alerts.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = mock_resp
            result = alerts.send_telegram_message("token", "chat", "hello")
        assert result is True

    def test_returns_false_on_http_failure(self):
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with patch("app.alerts.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = mock_resp
            result = alerts.send_telegram_message("bad-token", "chat", "hello")
        assert result is False

    def test_returns_false_on_network_exception(self):
        with patch("app.alerts.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = Exception("timeout")
            result = alerts.send_telegram_message("token", "chat", "hello")
        assert result is False


class TestFireAlertsForNewTrades:
    def test_returns_zero_when_alerts_disabled(self, db):
        wallet = _wallet(db)
        _settings(db, enabled=0)
        _trade(db, wallet.address, "t1")
        assert alerts.fire_alerts_for_new_trades(db, wallet) == 0

    def test_returns_zero_when_no_token(self, db):
        wallet = _wallet(db)
        s = AppSettings(id=1, alerts_enabled=1, telegram_bot_token=None, telegram_chat_id="chat")
        db.add(s)
        db.commit()
        _trade(db, wallet.address, "t1")
        assert alerts.fire_alerts_for_new_trades(db, wallet) == 0

    def test_sends_alert_for_qualifying_trade_and_marks_sent(self, db):
        wallet = _wallet(db)
        _settings(db, min_size=50.0)
        trade = _trade(db, wallet.address, "t-qualify", price=0.5, size=200.0)

        with patch("app.alerts.send_telegram_message", return_value=True) as mock_send:
            sent = alerts.fire_alerts_for_new_trades(db, wallet)

        assert sent == 1
        mock_send.assert_called_once()
        db.refresh(trade)
        assert trade.alert_sent == 1

    def test_skips_trade_below_threshold(self, db):
        wallet = _wallet(db)
        _settings(db, min_size=500.0)
        _trade(db, wallet.address, "t-small", price=0.5, size=10.0)  # value = 5.0

        with patch("app.alerts.send_telegram_message", return_value=True) as mock_send:
            sent = alerts.fire_alerts_for_new_trades(db, wallet)

        assert sent == 0
        mock_send.assert_not_called()

    def test_caps_at_max_alerts_per_wallet(self, db):
        wallet = _wallet(db)
        _settings(db, min_size=10.0)
        for i in range(10):
            _trade(db, wallet.address, f"t-cap-{i}", price=0.5, size=200.0)

        with patch("app.alerts.send_telegram_message", return_value=True):
            sent = alerts.fire_alerts_for_new_trades(db, wallet)

        assert sent == alerts._MAX_ALERTS_PER_WALLET

    def test_marks_stale_trades_as_sent_without_alerting(self, db):
        wallet = _wallet(db)
        _settings(db, min_size=10.0)
        stale_trade = _trade(db, wallet.address, "t-stale", price=0.5, size=200.0, hours_ago=30)

        with patch("app.alerts.send_telegram_message", return_value=True) as mock_send:
            sent = alerts.fire_alerts_for_new_trades(db, wallet)

        assert sent == 0
        mock_send.assert_not_called()
        db.refresh(stale_trade)
        assert stale_trade.alert_sent == 1

    def test_already_sent_trade_is_not_re_alerted(self, db):
        wallet = _wallet(db)
        _settings(db, min_size=10.0)
        _trade(db, wallet.address, "t-already-sent", price=0.5, size=200.0, alert_sent=1)

        with patch("app.alerts.send_telegram_message", return_value=True) as mock_send:
            sent = alerts.fire_alerts_for_new_trades(db, wallet)

        assert sent == 0
        mock_send.assert_not_called()

    def test_alert_sent_stays_zero_when_telegram_call_fails(self, db):
        wallet = _wallet(db)
        _settings(db, min_size=10.0)
        trade = _trade(db, wallet.address, "t-fail", price=0.5, size=200.0)

        with patch("app.alerts.send_telegram_message", return_value=False):
            sent = alerts.fire_alerts_for_new_trades(db, wallet)

        assert sent == 0
        db.refresh(trade)
        assert trade.alert_sent == 0


class TestBuildMessage:
    def test_includes_wallet_label_market_direction_and_value(self):
        wallet = Wallet(address="0x" + "a" * 40, label="Signal Desk")
        trade = Trade(
            wallet_address=wallet.address,
            trade_id="msg-1",
            condition_id="cond-msg",
            market_title="Election Market",
            side="YES",
            price=0.75,
            size=200.0,
            traded_at=datetime(2026, 1, 15, 10, 30),
        )
        msg = alerts._build_message(trade, wallet)
        assert "Signal Desk" in msg
        assert "Election Market" in msg
        assert "BUY" in msg
        assert "$150.00" in msg

    def test_falls_back_to_short_address_when_no_label(self):
        wallet = Wallet(address="0x" + "b" * 40, label=None)
        trade = Trade(
            wallet_address=wallet.address,
            trade_id="msg-2",
            condition_id="cond-fall",
            market_title=None,
            side="NO",
            price=0.5,
            size=10.0,
            traded_at=datetime(2026, 1, 15, 10, 30),
        )
        msg = alerts._build_message(trade, wallet)
        assert "0xbbbb" in msg
        assert "cond-fall" in msg
        assert "SELL" in msg
