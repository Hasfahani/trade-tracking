# Summary: Handles alert settings and refresh status pages.
# Details: It connects browser requests to the right database work, page rendering, redirects, and JSON responses.
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
from app.models import MarketResolution, SyncEvent, Trade
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
        sweep_raw = metrics.get("threshold_sweep") or {}
        # Sort the precision/recall-vs-threshold rows by threshold for display.
        sweep = [
            {
                "threshold": float(key),
                "precision": row.get("precision"),
                "recall": row.get("recall"),
                "f1": row.get("f1"),
            }
            for key, row in sorted(sweep_raw.items(), key=lambda item: float(item[0]))
        ]
        baselines_raw = payload.get("baseline_metrics") or {}
        baselines = []
        for key, row in baselines_raw.items():
            test_row = (row or {}).get("test_metrics") or {}
            baselines.append({
                "name": str(key).replace("_", " "),
                "note": (row or {}).get("note"),
                "roc_auc": test_row.get("roc_auc"),
                "pr_auc": test_row.get("pr_auc"),
                "precision": test_row.get("precision_at_threshold"),
                "recall": test_row.get("recall_at_threshold"),
                "f1": test_row.get("f1_at_threshold"),
                "brier": test_row.get("brier"),
            })
        excluded = payload.get("excluded_features") or []
        return {
            "trained_at": payload.get("trained_at"),
            "model_version": payload.get("trained_at"),
            "mode": payload.get("mode"),
            "model_type": "single sigmoid neuron",
            "task": "unusual trade detection",
            "inference": "NumPy",
            "training_stack": "TensorFlow local/admin only",
            "feature_set": payload.get("feature_set"),
            "leakage_safe": payload.get("leakage_safe"),
            "n_features": len(payload.get("feature_names") or []),
            "excluded_features": excluded,
            "threshold": payload.get("threshold"),
            "n_train": payload.get("n_train"),
            "n_val": payload.get("n_val"),
            "n_test": payload.get("n_test"),
            "base_rate": metrics.get("base_rate"),
            "roc_auc": metrics.get("roc_auc"),
            "pr_auc": metrics.get("pr_auc"),
            # At the chosen operating threshold; older weight files only have
            # the fixed-0.5 numbers.
            "precision": metrics.get("precision_at_threshold", metrics.get("precision_at_0_5")),
            "recall": metrics.get("recall_at_threshold", metrics.get("recall_at_0_5")),
            "f1": metrics.get("f1_at_threshold", metrics.get("f1_at_0_5")),
            "brier": metrics.get("brier"),
            "flag_rate": metrics.get("flag_rate_at_threshold"),
            "threshold_sweep": sweep,
            "baselines": baselines,
        }
    except Exception:
        return None


# A profitability/outcome model is only trustworthy once its held-out test
# ROC-AUC is clearly above chance and it beats the market-implied-probability
# baseline. A model that only restates market price is useful context, but not a
# separate wallet edge.
OUTCOME_MODEL_MIN_TEST_ROC_AUC = 0.65
OUTCOME_MODEL_MIN_MARKET_ROC_EDGE = 0.01


def _outcome_model_status() -> Optional[Dict[str, Any]]:
    """Summarize data/outcome_model_weights.json (the experimental win-prob model).

    Returns None when the file is absent/invalid. The ``experimental`` flag is
    True whenever the held-out test ROC-AUC is not clearly above chance, so the
    UI never presents a data-starved model as finished.
    """
    from app.ml.outcome_model import DEFAULT_OUTCOME_WEIGHTS_PATH

    try:
        payload = json.loads(Path(DEFAULT_OUTCOME_WEIGHTS_PATH).read_text(encoding="utf-8"))
    except Exception:
        return None

    test = payload.get("test_metrics") or {}
    val = payload.get("val_metrics") or {}
    baselines = payload.get("baseline_metrics") or {}
    market_baseline = baselines.get("market_implied_win_prob") or {}
    market_test = market_baseline.get("test_metrics") or {}
    model_vs_market = payload.get("model_vs_market") or {}
    test_roc = test.get("roc_auc")
    market_roc = market_test.get("roc_auc")
    roc_edge = model_vs_market.get("roc_auc_delta")
    if roc_edge is None and test_roc is not None and market_roc is not None:
        roc_edge = test_roc - market_roc
    experimental = (
        test_roc is None
        or test_roc < OUTCOME_MODEL_MIN_TEST_ROC_AUC
        or market_roc is None
        or roc_edge is None
        or roc_edge < OUTCOME_MODEL_MIN_MARKET_ROC_EDGE
    )
    if market_roc is None:
        limitation = "missing_baseline"
    elif roc_edge is not None and roc_edge < OUTCOME_MODEL_MIN_MARKET_ROC_EDGE:
        limitation = "market_baseline_not_beaten"
    elif test_roc is None or test_roc < OUTCOME_MODEL_MIN_TEST_ROC_AUC:
        limitation = "weak_test_roc"
    else:
        limitation = "validated"
    return {
        "trained_at": payload.get("trained_at"),
        "n_features": len(payload.get("feature_names") or []),
        "n_train": payload.get("n_train"),
        "n_val": payload.get("n_val"),
        "n_test": payload.get("n_test"),
        "threshold": payload.get("threshold"),
        "experimental": experimental,
        "limitation": limitation,
        "min_test_roc_auc": OUTCOME_MODEL_MIN_TEST_ROC_AUC,
        "min_market_roc_edge": OUTCOME_MODEL_MIN_MARKET_ROC_EDGE,
        "val_roc_auc": val.get("roc_auc"),
        "val_base_rate": val.get("base_rate"),
        "test_roc_auc": test_roc,
        "test_pr_auc": test.get("pr_auc"),
        "test_base_rate": test.get("base_rate"),
        "test_precision": test.get("precision_at_threshold"),
        "test_recall": test.get("recall_at_threshold"),
        "test_f1": test.get("f1_at_threshold"),
        "test_accuracy": test.get("accuracy"),
        "test_brier": test.get("brier"),
        "market_baseline_threshold": market_baseline.get("threshold"),
        "market_baseline_roc_auc": market_roc,
        "market_baseline_pr_auc": market_test.get("pr_auc"),
        "market_baseline_accuracy": market_test.get("accuracy"),
        "market_baseline_brier": market_test.get("brier"),
        "market_baseline_f1": market_test.get("f1_at_threshold"),
        "roc_auc_edge": roc_edge,
        "pr_auc_edge": model_vs_market.get("pr_auc_delta"),
        "accuracy_edge": model_vs_market.get("accuracy_delta"),
        "brier_edge": model_vs_market.get("brier_delta"),
        "f1_edge": model_vs_market.get("f1_delta"),
    }


@router.get("/admin/train-model")
async def train_model_page(request: Request, db: Session = Depends(get_db)):
    from app.ml import train as ml_train
    from app.ml.features import MIN_PRIOR_TRADES

    wallet_counts = (
        db.query(func.count(Trade.id)).group_by(Trade.wallet_address).all()
    )
    scorable_trades = sum(max(count - MIN_PRIOR_TRADES, 0) for (count,) in wallet_counts)
    outcome_status = _outcome_model_status()
    if outcome_status:
        resolved_market_count = (
            db.query(func.count(MarketResolution.condition_id))
            .filter(MarketResolution.outcome.in_(("YES", "NO")))
            .scalar()
            or 0
        )
        outcome_status = {**outcome_status, "resolved_market_count": int(resolved_market_count)}

    return templates.TemplateResponse(
        request,
        "train_model_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "model_status": _model_weights_status(),
            "outcome_status": outcome_status,
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
        from app.ml.model import effective_signal_threshold, get_signal_model, reset_signal_model_cache

        reset_signal_model_cache()
        app.state.signal_model = get_signal_model()
        templates.env.globals["signal_threshold"] = effective_signal_threshold()
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
