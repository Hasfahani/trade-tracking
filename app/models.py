"""
Manual SQLite migration notes for existing databases.
Run these statements yourself; the app will not apply them automatically.

ALTER TABLE wallets ADD COLUMN tags TEXT;
ALTER TABLE wallets ADD COLUMN is_pinned INTEGER;
ALTER TABLE wallets ADD COLUMN last_checked_at DATETIME;
ALTER TABLE wallets ADD COLUMN last_refresh_count INTEGER;
ALTER TABLE wallets ADD COLUMN last_error_at DATETIME;
ALTER TABLE wallets ADD COLUMN last_error_message TEXT;

CREATE TABLE notification_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sound_enabled INTEGER,
    min_trade_value FLOAT,
    dedupe_window_seconds INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address VARCHAR(255),
    status VARCHAR(32),
    fetched_count INTEGER,
    inserted_count INTEGER,
    error_message TEXT,
    duplicate_count INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, DateTime, Float, CheckConstraint
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(255), unique=True, nullable=False, index=True)
    label = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    is_pinned = Column(Integer, nullable=True, default=0)
    last_checked_at = Column(DateTime, nullable=True)
    last_refresh_count = Column(Integer, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(255), nullable=False, index=True)
    trade_id = Column(String(255), unique=True, nullable=False, index=True)
    condition_id = Column(String(255), nullable=False, index=True)
    market_title = Column(Text, nullable=True)
    side = Column(String(3), nullable=False)
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    traded_at = Column(DateTime, nullable=False)
    inserted_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("side IN ('YES', 'NO')", name="check_side"),
    )


class Notification(Base):
    __tablename__ = 'notifications'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(255), nullable=False, index=True)
    trade_id = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    market_title = Column(Text)
    side = Column(String(3))
    price = Column(Float)
    size = Column(Float)
    is_read = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class NotificationSetting(Base):
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sound_enabled = Column(Integer, nullable=True, default=1)
    min_trade_value = Column(Float, nullable=True, default=0.0)
    dedupe_window_seconds = Column(Integer, nullable=True, default=120)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(255), nullable=True, index=True)
    status = Column(String(32), nullable=True, index=True)
    fetched_count = Column(Integer, nullable=True)
    inserted_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    duplicate_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)
