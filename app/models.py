# Defines the database tables.
"""SQLAlchemy models for the watchlist app.

SQLite compatibility changes are applied by app.db.run_schema_migrations.
"""

from sqlalchemy import CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import declarative_base, relationship
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
    ai_summary = Column(Text, nullable=True)
    ai_summary_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    trades = relationship("Trade", back_populates="wallet", cascade="all, delete-orphan", passive_deletes=True)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(255), ForeignKey("wallets.address", ondelete="CASCADE"), nullable=False, index=True)
    wallet = relationship("Wallet", back_populates="trades")
    trade_id = Column(String(255), unique=True, nullable=False, index=True)
    condition_id = Column(String(255), nullable=False, index=True)
    market_title = Column(Text, nullable=True)
    side = Column(String(3), nullable=False)
    price = Column(Numeric(18, 6, asdecimal=False), nullable=False)
    size = Column(Numeric(18, 6, asdecimal=False), nullable=False)
    traded_at = Column(DateTime, nullable=False)
    inserted_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    alert_sent = Column(Integer, nullable=False, default=0)
    notable_score = Column(Float, nullable=True)
    # The Yes/No token this trade was on, captured from the Polymarket `outcome`
    # field. Orthogonal to `side` (which encodes BUY->YES / SELL->NO): together
    # they reconstruct buy/sell of the Yes/No token for realized-PnL accounting.
    # Nullable: rows ingested before this column existed have no token.
    outcome_token = Column(String(3), nullable=True)

    __table_args__ = (
        CheckConstraint("side IN ('YES', 'NO')", name="check_side"),
        Index("ix_trades_wallet_traded_at", "wallet_address", "traded_at"),
        Index("ix_trades_wallet_side_traded_at", "wallet_address", "side", "traded_at"),
        Index("ix_trades_wallet_market_title", "wallet_address", "market_title"),
        Index("ix_trades_traded_at", "traded_at"),
        Index("ix_trades_alert_sent", "alert_sent"),
    )



class MarketResolution(Base):
    """Resolved outcome for a Polymarket condition (binary YES/NO market).

    Populated from Polymarket's CLOB API (the token with ``winner=True``).
    ``outcome`` is ``YES``/``NO`` for a resolved market, or ``UNRESOLVED`` when
    the market is not yet closed. Used to compute realized PnL/ROI/win rate.
    """

    __tablename__ = "market_resolutions"

    condition_id = Column(String(255), primary_key=True)
    market_title = Column(Text, nullable=True)
    outcome = Column(String(16), nullable=False, default="UNRESOLVED")
    resolved_at = Column(DateTime, nullable=True)
    checked_at = Column(DateTime, server_default=func.now())
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        CheckConstraint("outcome IN ('YES', 'NO', 'UNRESOLVED')", name="check_resolution_outcome"),
        Index("ix_market_resolutions_outcome", "outcome"),
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


class AppSettings(Base):
    """Singleton settings row (always id=1). Stores Telegram alert config."""

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, default=1)
    telegram_bot_token = Column(Text, nullable=True)
    telegram_chat_id = Column(Text, nullable=True)
    alert_min_size = Column(Float, nullable=True, default=0.0)
    alerts_enabled = Column(Integer, nullable=True, default=0)
    updated_at = Column(DateTime, nullable=True)


class EventLog(Base):
    """Raw event stream for retention signal tracking."""

    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tracker_id = Column(String(64), nullable=False)
    event_name = Column(String(64), nullable=False)
    event_ts = Column(DateTime, nullable=False, server_default=func.now())
    route = Column(String(255), nullable=True)
    metadata_json = Column(Text, nullable=True)
    alert_id = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_event_log_tracker_ts", "tracker_id", "event_ts"),
        Index("ix_event_log_name_ts", "event_name", "event_ts"),
    )


class RetentionDaily(Base):
    """Pre-computed daily retention snapshot (populated by backfill script)."""

    __tablename__ = "retention_daily"

    date = Column(Date, primary_key=True)
    dau = Column(Integer, nullable=True)
    wau_rolling_7 = Column(Integer, nullable=True)
    alert_impressions_users = Column(Integer, nullable=True)
    alert_open_users = Column(Integer, nullable=True)
    alert_open_rate = Column(Float, nullable=True)
    d1_return_rate = Column(Float, nullable=True)
    d7_return_rate = Column(Float, nullable=True)
    sessions_per_user = Column(Float, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())


class RetentionWeekly(Base):
    """Pre-computed weekly retention snapshot (populated by backfill script)."""

    __tablename__ = "retention_weekly"

    week_start = Column(Date, primary_key=True)
    wau = Column(Integer, nullable=True)
    repeat_users = Column(Integer, nullable=True)
    sessions_per_user = Column(Float, nullable=True)
    alert_open_rate = Column(Float, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())


class TradeAnalysis(Base):
    """Persistent AI analysis cache for individual trades."""

    __tablename__ = "trade_analysis"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String(255), unique=True, nullable=False, index=True)
    provider = Column(String(64), nullable=True)
    signal = Column(String(32), nullable=True)
    risk = Column(String(16), nullable=True)
    price_insight = Column(Text, nullable=True)
    behavior = Column(Text, nullable=True)
    verdict = Column(Text, nullable=True)
    context_json = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    model_version = Column(String(128), nullable=True)
