# Handles alert settings and refresh status pages.
"""Settings and refresh/status routes."""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_TELEGRAM_TOKEN_RE = re.compile(r'^\d+:[\w\-]+$')
_TELEGRAM_CHAT_ID_RE = re.compile(r'^-?\d+$')

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app import alerts
from app import retention as ret
from app import view_helpers as vh
from app.db import get_db
from app.ingest import cleanup_duplicate_trades, find_duplicate_groups, refresh_wallet
from app.models import SyncEvent, Trade
from app.routes._shared import _flash_redirect_to, resolve_wallet, sanitize_search, templates
from app.settings import APP_NAME, DEFAULT_REFRESH_LIMIT, RETENTION_METRICS_ENABLED

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="alert_impression",
            route="settings",
        ))

    settings = alerts.get_app_settings(db)
    return templates.TemplateResponse(
        request,
        "settings_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "settings": settings,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
        },
    )


@router.post("/settings")
async def save_settings(
    db: Session = Depends(get_db),
    telegram_bot_token: Optional[str] = Form(None),
    telegram_chat_id: Optional[str] = Form(None),
    alert_min_size: Optional[str] = Form(None),
    alerts_enabled: Optional[str] = Form(None),
):
    try:
        settings = alerts.get_app_settings(db)
        new_token = (telegram_bot_token or "").strip()
        new_chat_id = (telegram_chat_id or "").strip()
        if new_token and not _TELEGRAM_TOKEN_RE.match(new_token):
            return _flash_redirect_to("/settings", "Invalid bot token format (expected 123456789:AAExj7...).", "error")
        if new_chat_id and not _TELEGRAM_CHAT_ID_RE.match(new_chat_id):
            return _flash_redirect_to("/settings", "Chat ID must be a number (e.g. 8708428862 or -100123456).", "error")
        if new_token:
            settings.telegram_bot_token = new_token
        settings.telegram_chat_id = new_chat_id or None
        settings.alerts_enabled = 1 if alerts_enabled else 0
        try:
            settings.alert_min_size = float(alert_min_size) if alert_min_size else 0.0
        except ValueError:
            settings.alert_min_size = 0.0
        settings.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return _flash_redirect_to("/settings", "Settings saved.", "success")
    except Exception:
        logger.exception("Failed to save settings")
        return _flash_redirect_to("/settings", "Failed to save settings - please try again.", "error")


@router.post("/settings/test-alert")
async def test_alert(request: Request, db: Session = Depends(get_db)):
    settings = alerts.get_app_settings(db)
    token = (settings.telegram_bot_token or "").strip()
    chat_id = (settings.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return _flash_redirect_to("/settings", "Enter a bot token and chat ID before testing.", "error")
    ok = alerts.send_telegram_message(token, chat_id, "\u2705 <b>PolySignal test alert</b>\nYour Telegram alerts are working.")
    if ok:
        if RETENTION_METRICS_ENABLED:
            ret.emit(ret.RawEvent(
                tracker_id=ret.get_or_create_tracker_id(request),
                event_name="alert_open",
                route="settings_test_alert",
            ))
        return _flash_redirect_to("/settings", "Test alert sent successfully.", "success")
    return _flash_redirect_to("/settings", "Test failed \u2014 check your bot token and chat ID.", "error")


@router.get("/admin/sync-status")
async def sync_status_page(
    request: Request,
    wallet_search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    error_only: int = Query(0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    wallet_search = sanitize_search(wallet_search)
    events_query = vh.filter_sync_events(
        db.query(SyncEvent).order_by(desc(SyncEvent.created_at)),
        wallet_search=wallet_search,
        status=status,
        error_only=bool(error_only),
    )
    total_events = events_query.order_by(False).count()
    total_pages = max(1, (total_events + page_size - 1) // page_size)
    page = min(page, total_pages)
    events = events_query.limit(page_size).offset((page - 1) * page_size).all()
    pagination = vh.pagination_meta(page, page_size, total_events)
    duplicates = find_duplicate_groups(
        db,
        wallet_search.lower() if wallet_search and vh.WALLET_ADDRESS_RE.match(wallet_search.lower()) else None,
    )
    return templates.TemplateResponse(
        request,
        "sync_status_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "events": events,
            "total_events": total_events,
            "total_pages": total_pages,
            "page": page,
            "page_size": page_size,
            "pagination": pagination,
            "duplicates": duplicates,
            "wallet_search": wallet_search,
            "status_filter": status,
            "error_only": bool(error_only),
            "sync_status_class": vh.sync_status_class,
            "duration_label": vh.duration_label,
            "short_address": vh.short_address,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
        },
    )


@router.post(
    "/admin/sync-status/cleanup",
    summary="Remove duplicate trades",
    description="Find and delete semantically-duplicate trade rows, keeping the earliest insertion.",
    tags=["admin"],
)
def cleanup_sync_duplicates(db: Session = Depends(get_db)):
    removed = cleanup_duplicate_trades(db)
    msg = f"Removed {removed} duplicate trade{'s' if removed != 1 else ''}." if removed else "No duplicate trades found."
    level = "success" if removed else "info"
    return _flash_redirect_to("/admin/sync-status", msg, level)


def _model_weights_status() -> Optional[Dict[str, Any]]:
    """Summarize data/model_weights.json for the admin page, or None if absent/invalid."""
    from app.ml.model import DEFAULT_WEIGHTS_PATH

    try:
        payload = json.loads(Path(DEFAULT_WEIGHTS_PATH).read_text(encoding="utf-8"))
        metrics = payload.get("test_metrics") or {}
        return {
            "trained_at": payload.get("trained_at"),
            "mode": payload.get("mode"),
            "threshold": payload.get("threshold"),
            "n_train": payload.get("n_train"),
            "n_test": payload.get("n_test"),
            "base_rate": metrics.get("base_rate"),
            "roc_auc": metrics.get("roc_auc"),
            "pr_auc": metrics.get("pr_auc"),
            # At the chosen operating threshold; older weight files only have
            # the fixed-0.5 numbers.
            "precision": metrics.get("precision_at_threshold", metrics.get("precision_at_0_5")),
            "recall": metrics.get("recall_at_threshold", metrics.get("recall_at_0_5")),
            "f1": metrics.get("f1_at_threshold", metrics.get("f1_at_0_5")),
        }
    except Exception:
        return None


@router.get("/admin/train-model")
async def train_model_page(request: Request, db: Session = Depends(get_db)):
    from app.ml import train as ml_train
    from app.ml.features import MIN_PRIOR_TRADES

    wallet_counts = (
        db.query(func.count(Trade.id)).group_by(Trade.wallet_address).all()
    )
    scorable_trades = sum(max(count - MIN_PRIOR_TRADES, 0) for (count,) in wallet_counts)

    return templates.TemplateResponse(
        request,
        "train_model_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "model_status": _model_weights_status(),
            "scorable_trades": scorable_trades,
            "tf_available": ml_train.tensorflow_available(),
            "training": ml_train.get_training_status(),
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
        },
    )


@router.post(
    "/admin/train-model",
    summary="Train the notable-trade model",
    description="Run training in a background thread and export new model weights. Only one run at a time.",
    tags=["admin"],
)
async def start_train_model(request: Request):
    from app.ml import train as ml_train

    if not ml_train.tensorflow_available():
        return _flash_redirect_to("/admin/train-model", "Training unavailable on this server - run locally.", "error")

    app = request.app

    def _reload_model(_result: Dict[str, Any]) -> None:
        from app.ml.model import get_signal_model, reset_signal_model_cache

        reset_signal_model_cache()
        app.state.signal_model = get_signal_model()
        logger.info("Signal model reloaded after training")

    if not ml_train.start_training_in_background(on_success=_reload_model):
        return _flash_redirect_to("/admin/train-model", "A training run is already in progress.", "error")
    return _flash_redirect_to("/admin/train-model", "Training started.", "success")


@router.get("/admin/train-model/status")
async def train_model_status():
    from app.ml import train as ml_train

    return JSONResponse(ml_train.get_training_status())


@router.post(
    "/admin/refresh",
    summary="Refresh wallet trades",
    description="Fetch new trades for one wallet (address=) or all active wallets. Returns inserted/fetched counts per wallet.",
    tags=["admin"],
)
def refresh_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if address:
        wallet = resolve_wallet(db, address)
        return JSONResponse({"status": "success", **refresh_wallet(db, wallet, limit=limit_per_wallet)})

    results: Dict[str, Any] = {}
    for wallet in vh.active_wallets(vh.wallet_order_query(db).all()):
        results[wallet.address] = refresh_wallet(db, wallet, limit=limit_per_wallet)
    return JSONResponse({"status": "success", "wallets_refreshed": len(results), "results": results})


@router.post(
    "/admin/refresh-all",
    summary="Full-history refresh for all wallets",
    description="Paginate through the complete Polymarket history for one or all active wallets. Can be slow.",
    tags=["admin"],
)
def refresh_all_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if address:
        wallet = resolve_wallet(db, address)
        return JSONResponse(
            {
                "status": "success",
                "message": "Full history fetch complete",
                **refresh_wallet(db, wallet, fetch_all=True, limit=limit_per_wallet),
            }
        )

    results: Dict[str, Any] = {}
    for wallet in vh.active_wallets(vh.wallet_order_query(db).all()):
        results[wallet.address] = refresh_wallet(db, wallet, fetch_all=True, limit=limit_per_wallet)
    return JSONResponse(
        {
            "status": "success",
            "wallets_refreshed": len(results),
            "results": results,
            "message": "Full history fetch complete for all wallets",
        }
    )
