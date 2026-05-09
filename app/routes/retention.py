"""Admin endpoint for retention metrics."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import retention as ret
from app.db import get_db
from app.settings import RETENTION_METRICS_ENABLED

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_RANGES = {"7d", "30d", "90d"}


@router.get("/admin/metrics/retention")
def retention_metrics(
    range: Optional[str] = Query("30d"),
    db: Session = Depends(get_db),
):
    """Return live retention summary plus pre-computed daily/weekly series.

    Query params:
        range: 7d | 30d (default) | 90d — lookback window for live summary
    """
    if not RETENTION_METRICS_ENABLED:
        return JSONResponse({"enabled": False}, status_code=200)

    period = range if range in _VALID_RANGES else "30d"
    days = {"7d": 7, "30d": 30, "90d": 90}[period]

    try:
        wau = ret.get_wau(db, days=7)
    except Exception:
        logger.exception("retention metrics: get_wau failed")
        wau = {}

    try:
        aor = ret.get_alert_open_rate(db, period="7d")
    except Exception:
        logger.exception("retention metrics: get_alert_open_rate failed")
        aor = {}

    try:
        repeat = ret.get_repeat_usage(db, lookback_days=days)
    except Exception:
        logger.exception("retention metrics: get_repeat_usage failed")
        repeat = {}

    try:
        daily = ret.get_daily_series(db, days=days)
    except Exception:
        logger.exception("retention metrics: get_daily_series failed")
        daily = []

    try:
        weekly = ret.get_weekly_series(db, weeks=max(1, days // 7))
    except Exception:
        logger.exception("retention metrics: get_weekly_series failed")
        weekly = []

    return JSONResponse(
        {
            "enabled": True,
            "range": period,
            "summary": {
                "wau": wau,
                "alert_open_rate": aor,
                "repeat_usage": repeat,
            },
            "daily_series": daily,
            "weekly_series": weekly,
        }
    )
