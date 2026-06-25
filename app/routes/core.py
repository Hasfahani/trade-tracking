# Summary: Handles dashboard and health check pages.
# Details: It connects browser requests to the right database work, page rendering, redirects, and JSON responses.
"""Root redirect and dashboard routes."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app import retention as ret
from app.backup import BACKUP_MODELS
from app.db import check_database_ready, get_db, get_applied_migration_versions
from app.models import SyncEvent, Trade, Wallet
from app.queries import get_dashboard_stats
from app.settings import APP_NAME, APP_ENV, APP_VERSION, GIT_COMMIT, IS_PRODUCTION, PUBLIC_BASE_URL, RETENTION_METRICS_ENABLED, RUNTIME_PLATFORM
from app import view_helpers as vh
from app.routes._shared import templates


def _get_status_counters() -> dict:
    try:
        from app.main import get_status_counters
        return get_status_counters()
    except Exception:
        return {}

router = APIRouter()


@router.get("/")
async def root():
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/tutorial")
async def tutorial(request: Request):
    """HTML tutorial for the normal wallet-tracking workflow."""
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="page_view",
            route="tutorial",
        ))

    return templates.TemplateResponse(
        request,
        "tutorial_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
        },
    )


@router.get("/healthz")
async def healthz(request: Request):
    return JSONResponse(
        {
            "status": "ok",
            "app": APP_NAME,
            "env": APP_ENV,
            "production": IS_PRODUCTION,
            "platform": RUNTIME_PLATFORM,
            "version": APP_VERSION,
            "commit": GIT_COMMIT,
            "ready": bool(getattr(request.app.state, "ready", False)),
        },
        status_code=200,
    )


@router.get("/readyz")
async def readyz(request: Request):
    ready = bool(getattr(request.app.state, "ready", False))
    db_ok = False
    error = getattr(request.app.state, "startup_error", None)
    if ready:
        try:
            check_database_ready()
            db_ok = True
        except Exception:
            ready = False
            error = "database check failed"
    status_code = 200 if ready and db_ok else 503
    return JSONResponse(
        {
            "status": "ready" if status_code == 200 else "not_ready",
            "database": "ok" if db_ok else "unavailable",
            "startup_error": error,
        },
        status_code=status_code,
    )


@router.get("/admin/schema-version")
async def schema_version():
    """Return applied migration versions and pending count for diagnostics."""
    from app.db import SCHEMA_MIGRATIONS
    applied = get_applied_migration_versions()
    all_versions = [v for v, _ in SCHEMA_MIGRATIONS]
    pending = [v for v in all_versions if v not in set(applied)]
    return JSONResponse({
        "applied": applied,
        "pending": pending,
        "total_defined": len(all_versions),
        "total_applied": len(applied),
    })


def _get_ai_provider_info() -> dict:
    """Return current AI provider detection result for diagnostics."""
    try:
        from app.ai_analysis import _detect_provider
        provider, model = _detect_provider()
        return {"provider": provider or "none", "model": model}
    except Exception:
        return {"provider": "unknown", "model": None}


def _get_ai_cache_stats(db) -> dict:
    """Return aggregate stats from the trade_analysis cache table."""
    try:
        from app.models import TradeAnalysis
        from sqlalchemy import func as _func
        count = db.query(_func.count(TradeAnalysis.id)).scalar() or 0
        return {"cached_analyses": int(count)}
    except Exception:
        return {"cached_analyses": 0}


def _get_table_counts(db) -> dict:
    counts = {}
    for model in BACKUP_MODELS:
        try:
            counts[model.__tablename__] = int(db.query(model).count())
        except Exception:
            counts[model.__tablename__] = None
    return counts


@router.get("/admin/ops")
async def ops_diagnostics(request: Request, db: Session = Depends(get_db)):
    """One-click operational diagnostics: db ping, migration status, last sync, build info, AI status."""
    db_ok = False
    db_error = None
    last_sync = None
    applied_migrations = []
    try:
        check_database_ready()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    try:
        from app.db import SCHEMA_MIGRATIONS
        applied_migrations = get_applied_migration_versions()
        sync = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).first()
        if sync:
            last_sync = {
                "status": sync.status,
                "wallet": sync.wallet_address,
                "inserted": sync.inserted_count,
                "fetched": sync.fetched_count,
                "at": sync.created_at.isoformat() if sync.created_at else None,
                "error": sync.error_message,
            }
        all_versions = [v for v, _ in SCHEMA_MIGRATIONS]
        pending = [v for v in all_versions if v not in set(applied_migrations)]
    except Exception:
        pending = []

    ai_info = _get_ai_provider_info()
    ai_cache = _get_ai_cache_stats(db)

    return JSONResponse({
        "database": "ok" if db_ok else "error",
        "database_error": db_error,
        "migration_status": "ok" if not pending else "pending",
        "migrations_applied": len(applied_migrations),
        "migrations_pending": pending,
        "last_sync": last_sync,
        "build": {"version": APP_VERSION, "commit": GIT_COMMIT, "env": APP_ENV, "platform": RUNTIME_PLATFORM},
        "ready": bool(getattr(request.app.state, "ready", False)),
        "counters": _get_status_counters(),
        "table_counts": _get_table_counts(db),
        "ai": {**ai_info, **ai_cache},
    })


@router.get("/api/last-sync")
async def last_sync_api():
    """Return the last successful sync timestamp for the staleness indicator."""
    try:
        from app.db import SessionLocal as _SL
        db = _SL()
        sync = db.query(SyncEvent).filter(SyncEvent.status == "success").order_by(desc(SyncEvent.created_at)).first()
        db.close()
        if sync and sync.created_at:
            return JSONResponse({"iso": sync.created_at.isoformat(), "has_sync": True})
    except Exception:
        pass
    return JSONResponse({"iso": None, "has_sync": False})


@router.get("/admin/ops-ui")
async def ops_ui(request: Request):
    """HTML operational diagnostics dashboard."""
    db_ok = False
    db_error = None
    last_sync = None
    applied_migrations = []
    pending = []
    try:
        check_database_ready()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)
    try:
        from app.db import SCHEMA_MIGRATIONS, SessionLocal as _SL
        applied_migrations = get_applied_migration_versions()
        db = _SL()
        sync = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).first()
        if sync:
            last_sync = {
                "status": sync.status,
                "wallet": sync.wallet_address,
                "inserted": sync.inserted_count,
                "fetched": sync.fetched_count,
                "at": sync.created_at.isoformat() if sync.created_at else None,
                "error": sync.error_message,
            }
        db.close()
        all_versions = [v for v, _ in SCHEMA_MIGRATIONS]
        pending = [v for v in all_versions if v not in set(applied_migrations)]
    except Exception:
        pending = []
    from app.db import SessionLocal as _SL2
    _db2 = _SL2()
    try:
        ai_cache = _get_ai_cache_stats(_db2)
    finally:
        _db2.close()
    ai_info = _get_ai_provider_info()

    return templates.TemplateResponse(
        request,
        "ops_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "db_ok": db_ok,
            "db_error": db_error,
            "applied_migrations": applied_migrations,
            "pending_migrations": pending,
            "last_sync": last_sync,
            "build_version": APP_VERSION,
            "build_commit": GIT_COMMIT,
            "build_env": APP_ENV,
            "ready": bool(getattr(request.app.state, "ready", False)),
            "counters": _get_status_counters(),
            "ai_provider": ai_info.get("provider", "none"),
            "ai_model": ai_info.get("model"),
            "ai_cached_analyses": ai_cache.get("cached_analyses", 0),
        },
    )


@router.get("/robots.txt")
async def robots_txt():
    sitemap_base_url = PUBLIC_BASE_URL.rstrip("/")
    return PlainTextResponse(
        "\n".join(
            [
                "User-agent: *",
                "Allow: /",
                "Disallow: /admin/",
                "Disallow: /settings",
                f"Sitemap: {sitemap_base_url}/sitemap.xml" if sitemap_base_url else "Sitemap: /sitemap.xml",
            ]
        )
        + "\n"
    )


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    base_url = PUBLIC_BASE_URL or f"{request.url.scheme}://{request.url.netloc}"
    urls = [
        "/",
        "/dashboard",
        "/tutorial",
        "/wallets",
        "/all-trades",
        "/wallets/import",
        "/healthz",
        "/readyz",
    ]
    body = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"]
    for path in urls:
        body.append("  <url>")
        body.append(f"    <loc>{base_url}{path}</loc>")
        body.append("  </url>")
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml")


@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="page_view",
            route="dashboard",
        ))

    stats = get_dashboard_stats(db)

    recent_trades = db.query(Trade).order_by(Trade.traded_at.desc()).limit(20).all()

    from sqlalchemy import func
    top_wallets_rows = (
        db.query(Trade.wallet_address, func.count(Trade.id).label("trade_count"))
        .group_by(Trade.wallet_address)
        .order_by(func.count(Trade.id).desc())
        .limit(5)
        .all()
    )
    needed_addresses = {row.wallet_address for row in top_wallets_rows} | {t.wallet_address for t in recent_trades}
    wallet_map = {w.address: w for w in db.query(Wallet).filter(Wallet.address.in_(needed_addresses)).all()}
    top_wallets = [
        {
            "wallet": wallet_map.get(row.wallet_address),
            "address": row.wallet_address,
            "trade_count": row.trade_count,
            "bar_pct": round((row.trade_count / top_wallets_rows[0].trade_count) * 100),
        }
        for row in top_wallets_rows
    ]

    top_markets = vh.build_top_markets(db)
    activity_days = vh.build_activity_heatmap(db)

    last_success_at = stats["last_success_at"]
    last_error_at = stats["last_error_at"]
    latest_sync_event = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).first()
    refresh_health = {
        "last_success_label": last_success_at.strftime("%Y-%m-%d %H:%M UTC") if last_success_at else "Never",
        "last_error_label": last_error_at.strftime("%Y-%m-%d %H:%M UTC") if last_error_at else "None recorded",
        "latest_status": latest_sync_event.status if latest_sync_event else "none",
        "latest_status_label": (latest_sync_event.status or "unknown").replace("_", " ").title() if latest_sync_event else "No syncs yet",
        "latest_summary": (
            f"{latest_sync_event.inserted_count or 0} inserted from {latest_sync_event.fetched_count or 0} fetched"
            if latest_sync_event
            else "Run a wallet refresh to establish the first sync event."
        ),
        "latest_error": latest_sync_event.error_message if latest_sync_event and latest_sync_event.error_message else None,
        "tone": "danger" if last_error_at and (not last_success_at or last_error_at > last_success_at) else "success",
    }
    interesting_activity = vh.detect_interesting_activity(db)
    insight_cards = [
        {
            "label": "24h value",
            "value": f"${stats['recent_value_24h']:,.2f}",
            "detail": "Stored trade value in the last day",
            "tone": "success" if stats["recent_value_24h"] else "info",
        },
        {
            "label": "Interesting events",
            "value": str(len(interesting_activity)),
            "detail": "Large trades, spikes, and new markets",
            "tone": "warning" if interesting_activity else "info",
        },
        {
            "label": "Top market",
            "value": top_markets[0]["market"] if top_markets else "None yet",
            "detail": f"${top_markets[0]['total_value']:,.2f} stored value" if top_markets else "Refresh wallets to populate markets",
            "tone": "success" if top_markets else "info",
        },
    ]

    retention_stats = ret.get_retention_summary(db) if RETENTION_METRICS_ENABLED else None

    from app.ml.model import effective_signal_threshold
    recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    model_flagged_24h = int(
        db.query(func.count(Trade.id))
        .filter(Trade.traded_at >= recent_cutoff)
        .filter(Trade.notable_score >= effective_signal_threshold())
        .scalar()
        or 0
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "app_name": APP_NAME,
            **stats,
            "refresh_health": refresh_health,
            "insight_cards": insight_cards,
            "recent_trades": recent_trades,
            "top_wallets": top_wallets,
            "top_markets": top_markets,
            "activity_days": activity_days,
            "wallet_map": wallet_map,
            "short_address": vh.short_address,
            "sync_status_class": vh.sync_status_class,
            "interesting_activity": interesting_activity,
            "retention": retention_stats,
            "model_flagged_24h": model_flagged_24h,
        },
    )
