"""SQLAlchemy models for the watchlist app.

SQLite compatibility columns are backfilled in app.db._ensure_wallet_columns.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(255), unique=True, nullable=False, index=True)
    label = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    is_pinned = Column(Integer, nullable=True, default=0)
    is_archived = Column(Integer, nullable=True, default=0)
    last_checked_at = Column(DateTime, nullable=True)
    last_refresh_status = Column(String(32), nullable=True)
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
        Index("ix_trades_wallet_traded_at", "wallet_address", "traded_at"),
        Index("ix_trades_wallet_side_traded_at", "wallet_address", "side", "traded_at"),
        Index("ix_trades_wallet_market_title", "wallet_address", "market_title"),
    )



class SyncEvent(Base):
    __tablename__ = "sync_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(255), nullable=True, index=True)
    status = Column(String(32), nullable=True, index=True)
    fetched_count = Column(Integer, nullable=True)
    inserted_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    duplicate_count = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    __table_args__ = (
        Index("ix_sync_events_wallet_created", "wallet_address", "created_at"),
    )
