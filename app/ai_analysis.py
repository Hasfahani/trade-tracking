"""AI trade analysis — Claude (Anthropic), Ollama, or Hugging Face."""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import Trade, TradeAnalysis, Wallet
from app import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection with short-lived cache to avoid HTTP on every analysis
# ---------------------------------------------------------------------------

_provider_cache: Dict[str, Any] = {"provider": None, "model": None, "candidates": None, "expires_at": 0.0}
_PROVIDER_TTL_SECONDS = 30.0


def _detect_provider_candidates() -> List[tuple[str, str]]:
    """Return ordered provider candidates as (provider_name, model_name)."""
    now = time.monotonic()
    cached = _provider_cache.get("candidates")
    if now < _provider_cache["expires_at"] and isinstance(cached, list):
        return cached

    candidates: List[tuple[str, str]] = []

    if settings.ANTHROPIC_API_KEY:
        candidates.append(("anthropic", settings.ANTHROPIC_MODEL))

    try:
        with httpx.Client(timeout=2) as client:
            resp = client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                model_names = [m.get("name") for m in models if m.get("name")]
                if model_names:
                    preferred = settings.OLLAMA_MODEL
                    selected = preferred if preferred in model_names else model_names[0]
                    candidates.append(("ollama", selected))
    except Exception:
        pass

    if settings.HUGGINGFACE_API_KEY:
        candidates.append(("huggingface", "mistralai/Mistral-7B-Instruct-v0.2"))

    first_provider: Optional[str] = None
    first_model: Optional[str] = None
    if candidates:
        first_provider, first_model = candidates[0]

    _provider_cache.update(
        {
            "provider": first_provider,
            "model": first_model,
            "candidates": candidates,
            "expires_at": now + _PROVIDER_TTL_SECONDS,
        }
    )
    return candidates


def _detect_provider() -> tuple[Optional[str], Optional[str]]:
    """Return (provider_name, model_name). Cached for _PROVIDER_TTL_SECONDS."""
    candidates = _detect_provider_candidates()
    if candidates:
        return candidates[0]
    return None, None


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------

def build_trade_context(trade: Trade, db: Session) -> Dict[str, Any]:
    """Gather statistical context about a trade for the AI prompt."""
    trade_value_expr = Trade.price * Trade.size

    wallet_market = db.query(
        func.count(Trade.id).label("count"),
        func.sum(case((Trade.side == "YES", 1), else_=0)).label("yes_count"),
        func.sum(case((Trade.side == "NO", 1), else_=0)).label("no_count"),
        func.avg(trade_value_expr).label("avg_value"),
        func.min(Trade.traded_at).label("first_trade_at"),
    ).filter(
        Trade.wallet_address == trade.wallet_address,
        Trade.condition_id == trade.condition_id,
    ).first()

    wallet_overall = db.query(
        func.count(Trade.id).label("total_trades"),
        func.avg(trade_value_expr).label("avg_value"),
        func.count(func.distinct(Trade.condition_id)).label("total_markets"),
        func.sum(case((Trade.side == "YES", 1), else_=0)).label("yes_count"),
        func.sum(case((Trade.side == "NO", 1), else_=0)).label("no_count"),
    ).filter(Trade.wallet_address == trade.wallet_address).first()

    market_all = db.query(
        func.count(Trade.id).label("total_trades"),
        func.sum(case((Trade.side == "YES", 1), else_=0)).label("yes_count"),
        func.sum(case((Trade.side == "NO", 1), else_=0)).label("no_count"),
        func.avg(Trade.price).label("avg_price"),
        func.avg(case((Trade.side == "YES", Trade.price), else_=None)).label("yes_avg_price"),
        func.avg(case((Trade.side == "NO", Trade.price), else_=None)).label("no_avg_price"),
        func.count(func.distinct(Trade.wallet_address)).label("unique_wallets"),
        func.max(Trade.price).label("max_price"),
        func.min(Trade.price).label("min_price"),
    ).filter(Trade.condition_id == trade.condition_id).first()

    # Derived values
    market_total = int(market_all.total_trades or 0)
    market_yes = int(market_all.yes_count or 0)
    market_yes_pct = round(market_yes / market_total * 100) if market_total else 50

    yes_avg_price = float(market_all.yes_avg_price or 0)
    no_avg_price = float(market_all.no_avg_price or 0)

    wallet_market_count = int(wallet_market.count or 0)
    this_value = trade.price * trade.size
    wallet_avg_value = float(wallet_overall.avg_value or 0)
    size_vs_avg = round(this_value / wallet_avg_value, 1) if wallet_avg_value > 0 else 1.0

    wallet_total_trades = int(wallet_overall.total_trades or 0)
    wallet_yes_total = int(wallet_overall.yes_count or 0)
    wallet_yes_bias_pct = round(wallet_yes_total / wallet_total_trades * 100) if wallet_total_trades else 50

    first_trade_at = wallet_market.first_trade_at
    is_first_on_market = wallet_market_count <= 1 or (first_trade_at and first_trade_at >= trade.traded_at)

    ref_avg = yes_avg_price if trade.side == "YES" else no_avg_price
    price_vs_consensus = "at market average"
    if ref_avg > 0:
        diff_pct = round((trade.price - ref_avg) / ref_avg * 100)
        if diff_pct > 8:
            price_vs_consensus = f"{diff_pct}% above {trade.side} avg (paid premium)"
        elif diff_pct < -8:
            price_vs_consensus = f"{abs(diff_pct)}% below {trade.side} avg (got discount)"
        else:
            price_vs_consensus = f"near {trade.side} market average (±{abs(diff_pct)}%)"

    if market_yes_pct >= 65:
        market_sentiment = "strongly bullish"
    elif market_yes_pct >= 55:
        market_sentiment = "bullish"
    elif market_yes_pct <= 35:
        market_sentiment = "strongly bearish"
    elif market_yes_pct <= 45:
        market_sentiment = "bearish"
    else:
        market_sentiment = "neutral"

    if wallet_yes_bias_pct >= 70:
        wallet_bias = "strong YES bias"
    elif wallet_yes_bias_pct >= 55:
        wallet_bias = "mild YES bias"
    elif wallet_yes_bias_pct <= 30:
        wallet_bias = "strong NO bias"
    elif wallet_yes_bias_pct <= 45:
        wallet_bias = "mild NO bias"
    else:
        wallet_bias = "balanced YES/NO history"

    price_range = float(market_all.max_price or 0) - float(market_all.min_price or 0)
    market_conviction = "high conviction" if price_range < 0.15 else ("split" if price_range < 0.35 else "wide disagreement")

    return {
        "trade_value": round(float(this_value), 2),
        "market_yes_pct": market_yes_pct,
        "market_sentiment": market_sentiment,
        "market_total_trades": market_total,
        "market_unique_wallets": int(market_all.unique_wallets or 0),
        "market_avg_price": round(float(market_all.avg_price or 0), 4),
        "market_yes_avg_price": round(yes_avg_price, 4),
        "market_no_avg_price": round(no_avg_price, 4),
        "market_conviction": market_conviction,
        "price_vs_consensus": price_vs_consensus,
        "wallet_total_trades": wallet_total_trades,
        "wallet_total_markets": int(wallet_overall.total_markets or 0),
        "wallet_avg_trade_value": round(wallet_avg_value, 2),
        "wallet_yes_bias_pct": wallet_yes_bias_pct,
        "wallet_bias": wallet_bias,
        "wallet_trades_on_this_market": wallet_market_count,
        "is_first_trade_on_market": is_first_on_market,
        "size_vs_wallet_avg": size_vs_avg,
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an expert quantitative prediction market analyst for Polymarket. "
    "Analyze trades with precision and directness. No hedging. Use concrete observations from data."
)


def _build_prompt(trade: Trade, ctx: Dict[str, Any]) -> str:
    """Build the user-facing analysis prompt (system prompt is kept separate)."""
    implied_prob = round(trade.price * 100)

    stance_note = ""
    if trade.side == "YES" and ctx["market_yes_pct"] < 40:
        stance_note = f"CONTRARIAN: only {ctx['market_yes_pct']}% of tracked market trades are YES — this wallet goes against the grain."
    elif trade.side == "NO" and ctx["market_yes_pct"] > 60:
        stance_note = f"CONTRARIAN: {ctx['market_yes_pct']}% of market trades are YES — this wallet is shorting the majority view."
    elif trade.side == "YES" and ctx["market_yes_pct"] > 70:
        stance_note = f"MOMENTUM: {ctx['market_yes_pct']}% of market trades are YES — wallet aligns with strong crowd consensus."
    elif trade.side == "NO" and ctx["market_yes_pct"] < 30:
        stance_note = f"MOMENTUM: only {ctx['market_yes_pct']}% YES — wallet piles onto an already bearish market."

    if ctx["size_vs_wallet_avg"] >= 2.5:
        size_note = f"HIGH CONVICTION: this trade is {ctx['size_vs_wallet_avg']}x the wallet's average size — a significantly oversized bet."
    elif ctx["size_vs_wallet_avg"] <= 0.4:
        size_note = f"PROBE TRADE: {ctx['size_vs_wallet_avg']}x wallet average — testing a position or scaling out."
    else:
        size_note = f"Typical position size: {ctx['size_vs_wallet_avg']}x wallet average of ${ctx['wallet_avg_trade_value']}."

    first_trade_line = (
        "This is the wallet's FIRST entry on this market — new position initiation."
        if ctx["is_first_trade_on_market"]
        else f"The wallet has {ctx['wallet_trades_on_this_market']} prior trades on this market (accumulating / scaling)."
    )

    return f"""Analyze this Polymarket trade.

RESPOND IN EXACTLY THIS FORMAT — 5 labeled lines, no preamble, no explanation, nothing else:

SIGNAL: [CONTRARIAN | CONSENSUS | CONVICTION | SPECULATIVE]
RISK: [LOW | MEDIUM | HIGH]
PRICE_INSIGHT: [1 crisp sentence about what the entry price implies about the trader's probability view]
BEHAVIOR: [1 crisp sentence about what this trade reveals about the wallet's strategy or pattern]
VERDICT: [1 sharp, insightful sentence — the single most important takeaway about this trade]

=== TRADE ===
Market: {trade.market_title}
Side: {trade.side}
Price: ${trade.price:.4f} → trader implies {implied_prob}% probability of YES outcome
Trade value: ${ctx['trade_value']:.2f} (price × size)
Timestamp: {trade.traded_at.strftime('%Y-%m-%d %H:%M UTC')}

=== MARKET CONTEXT (all tracked wallets in this market) ===
Tracked trade volume: {ctx['market_total_trades']} trades across {ctx['market_unique_wallets']} wallets
YES vs NO distribution: {ctx['market_yes_pct']}% YES — overall market is {ctx['market_sentiment']}
Market price spread: {ctx['market_conviction']}
Avg YES entry: ${ctx['market_yes_avg_price']:.4f} | Avg NO entry: ${ctx['market_no_avg_price']:.4f}
This entry vs peer consensus: {ctx['price_vs_consensus']}
{stance_note}

=== WALLET CONTEXT ===
Portfolio: {ctx['wallet_total_trades']} total trades across {ctx['wallet_total_markets']} markets
Historical bias: {ctx['wallet_bias']} ({ctx['wallet_yes_bias_pct']}% YES across all trades)
{first_trade_line}
{size_note}"""


# ---------------------------------------------------------------------------
# Response parsing (handles minor LLM formatting variance)
# ---------------------------------------------------------------------------

_VALID_SIGNALS = frozenset({"CONTRARIAN", "CONSENSUS", "CONVICTION", "SPECULATIVE"})
_VALID_RISKS = frozenset({"LOW", "MEDIUM", "HIGH"})


def _parse_response(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "signal": None, "risk": None,
        "price_insight": None, "behavior": None, "verdict": None,
    }
    for line in text.strip().splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("SIGNAL:"):
            val = stripped[7:].strip().upper().split()[0] if stripped[7:].strip() else ""
            if val in _VALID_SIGNALS:
                result["signal"] = val
        elif upper.startswith("RISK:"):
            val = stripped[5:].strip().upper().split()[0] if stripped[5:].strip() else ""
            if val in _VALID_RISKS:
                result["risk"] = val
        elif upper.startswith("PRICE_INSIGHT:"):
            result["price_insight"] = stripped[14:].strip()
        elif upper.startswith("BEHAVIOR:"):
            result["behavior"] = stripped[9:].strip()
        elif upper.startswith("VERDICT:"):
            result["verdict"] = stripped[8:].strip()

    if result["signal"] is None and text:
        lc = text.lower()
        if "contrarian" in lc:
            result["signal"] = "CONTRARIAN"
        elif "conviction" in lc or "oversized" in lc or "high conviction" in lc:
            result["signal"] = "CONVICTION"
        elif "consensus" in lc or "momentum" in lc:
            result["signal"] = "CONSENSUS"
        else:
            result["signal"] = "SPECULATIVE"

    return result


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _analyze_with_anthropic(trade: Trade, ctx: Dict[str, Any], model: str) -> Optional[Dict[str, Any]]:
    """Call Claude via the official Anthropic SDK with prompt caching on the system prompt."""
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=model,
            max_tokens=400,
            temperature=0.1,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": _build_prompt(trade, ctx)}],
        )
        text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip()
        parsed = _parse_response(text)
        parsed["provider"] = f"Claude ({model})"
        parsed["model_version"] = model
        return parsed
    except Exception as e:
        logger.warning("Anthropic analysis failed: %s", e)
    return None


def _summarize_with_anthropic(prompt: str, model: str) -> Optional[str]:
    """Call Claude for a wallet summary with prompt caching on the system instruction."""
    _summary_system = "You are a prediction market analyst. Be concrete and analytical. No caveats or hedging."
    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=model,
            max_tokens=200,
            temperature=0.2,
            system=[
                {
                    "type": "text",
                    "text": _summary_system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if hasattr(block, "text")
        ).strip() or None
    except Exception as e:
        logger.warning("Anthropic summary failed: %s", e)
    return None


def _analyze_with_ollama(trade: Trade, ctx: Dict[str, Any], model: str) -> Optional[Dict[str, Any]]:
    prompt = _build_prompt(trade, ctx)
    try:
        with httpx.Client(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            resp = client.post(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
        if resp.status_code == 200:
            text = resp.json().get("response", "").strip()
            parsed = _parse_response(text)
            parsed["provider"] = f"Ollama ({model})"
            parsed["model_version"] = model
            return parsed
    except Exception as e:
        logger.warning("Ollama analysis failed: %s", e)
    return None


def _analyze_with_huggingface(trade: Trade, ctx: Dict[str, Any], model: str) -> Optional[Dict[str, Any]]:
    if not settings.HUGGINGFACE_API_KEY:
        return None
    prompt = _build_prompt(trade, ctx)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"https://api-inference.huggingface.co/models/{model}",
                headers={"Authorization": f"Bearer {settings.HUGGINGFACE_API_KEY}"},
                json={
                    "inputs": prompt,
                    "parameters": {"max_new_tokens": 400, "temperature": 0.1, "return_full_text": False},
                },
            )
        if resp.status_code == 503:
            logger.warning("HuggingFace model is loading")
            return {"_error": "model_loading"}
        if resp.status_code == 200:
            result = resp.json()
            text = ""
            if isinstance(result, list) and result:
                text = result[0].get("generated_text", "").strip()
            parsed = _parse_response(text)
            parsed["provider"] = "Hugging Face"
            parsed["model_version"] = model
            return parsed
    except Exception as e:
        logger.warning("HuggingFace analysis failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Persistent DB cache — individual trade analysis
# ---------------------------------------------------------------------------

def _load_cached_analysis(trade_id: str, db: Session) -> Optional[Dict[str, Any]]:
    """Load a non-expired analysis from the DB cache."""
    row = db.query(TradeAnalysis).filter(TradeAnalysis.trade_id == trade_id).first()
    if row is None:
        return None
    if row.created_at:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=settings.AI_CACHE_TTL_HOURS)
        if row.created_at < cutoff:
            db.delete(row)
            db.commit()
            return None
    ctx = {}
    if row.context_json:
        try:
            ctx = json.loads(row.context_json)
        except Exception:
            pass
    return {
        "signal": row.signal,
        "risk": row.risk,
        "price_insight": row.price_insight,
        "behavior": row.behavior,
        "verdict": row.verdict,
        "provider": row.provider,
        "model_version": row.model_version,
        "context": ctx,
        "_from_cache": True,
    }


def _save_analysis_to_db(trade_id: str, result: Dict[str, Any], ctx: Dict[str, Any], db: Session) -> None:
    """Upsert analysis result into persistent cache table."""
    try:
        existing = db.query(TradeAnalysis).filter(TradeAnalysis.trade_id == trade_id).first()
        if existing:
            existing.provider = result.get("provider")
            existing.signal = result.get("signal")
            existing.risk = result.get("risk")
            existing.price_insight = result.get("price_insight")
            existing.behavior = result.get("behavior")
            existing.verdict = result.get("verdict")
            existing.context_json = json.dumps(ctx)
            existing.model_version = result.get("model_version")
            existing.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            row = TradeAnalysis(
                trade_id=trade_id,
                provider=result.get("provider"),
                signal=result.get("signal"),
                risk=result.get("risk"),
                price_insight=result.get("price_insight"),
                behavior=result.get("behavior"),
                verdict=result.get("verdict"),
                context_json=json.dumps(ctx),
                model_version=result.get("model_version"),
            )
            db.add(row)
        db.commit()
    except Exception:
        logger.exception("Failed to save AI analysis to DB cache for trade_id=%s", trade_id)
        db.rollback()


# ---------------------------------------------------------------------------
# Wallet summary cache — stored on Wallet row
# ---------------------------------------------------------------------------

_WALLET_SUMMARY_TTL_HOURS = 24


def _load_cached_wallet_summary(wallet: Wallet) -> Optional[str]:
    if not wallet.ai_summary or not wallet.ai_summary_at:
        return None
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=_WALLET_SUMMARY_TTL_HOURS)
    if wallet.ai_summary_at < cutoff:
        return None
    return wallet.ai_summary


def _save_wallet_summary(wallet_address: str, summary: str, db: Session) -> None:
    try:
        wallet = db.query(Wallet).filter(Wallet.address == wallet_address).first()
        if wallet:
            wallet.ai_summary = summary
            wallet.ai_summary_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
    except Exception:
        logger.exception("Failed to save wallet AI summary for %s", wallet_address)
        db.rollback()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_trade(trade: Trade, db: Session) -> Optional[Dict[str, Any]]:
    """Analyze a trade with AI. Returns structured dict or None if no provider available.

    Results are persisted to the trade_analysis table with a configurable TTL.
    Provider selection order: Anthropic Claude → Ollama → Hugging Face.
    """
    cached = _load_cached_analysis(trade.trade_id, db)
    if cached:
        return cached

    ctx = build_trade_context(trade, db)
    candidates = _detect_provider_candidates()

    if not candidates:
        return None

    first_error_result: Optional[Dict[str, Any]] = None
    for provider, model in candidates:
        result: Optional[Dict[str, Any]] = None
        if provider == "anthropic":
            result = _analyze_with_anthropic(trade, ctx, model)
        elif provider == "ollama":
            result = _analyze_with_ollama(trade, ctx, model)
        elif provider == "huggingface":
            result = _analyze_with_huggingface(trade, ctx, model)

        if not result:
            continue

        if result.get("_error") and first_error_result is None:
            first_error_result = result
            continue

        result["context"] = ctx
        _save_analysis_to_db(trade.trade_id, result, ctx, db)
        return result

    return first_error_result


def get_trade_summary(trades: List[Trade], db: Optional[Session] = None) -> Optional[str]:
    """Generate a plain-text summary of recent trades for wallet-level analysis.

    Result is cached on the Wallet row for _WALLET_SUMMARY_TTL_HOURS hours.
    """
    if not trades:
        return None

    # Check wallet-level cache first
    wallet_address = trades[0].wallet_address
    if db:
        wallet = db.query(Wallet).filter(Wallet.address == wallet_address).first()
        if wallet:
            cached = _load_cached_wallet_summary(wallet)
            if cached:
                return cached

    candidates = _detect_provider_candidates()
    if not candidates:
        return None

    lines = [
        f"- {t.side} ${t.price:.4f} × {t.size:.2f} on {(t.market_title or t.condition_id)[:50]} ({t.traded_at.strftime('%Y-%m-%d')})"
        for t in trades[:15]
    ]
    prompt = (
        "Summarize this wallet's recent trading pattern in 2-3 sentences. "
        "Focus on market concentration, directional bias, and any notable positioning.\n\n"
        "Trades (newest first):\n" + "\n".join(lines)
    )

    for provider, model in candidates:
        summary: Optional[str] = None
        try:
            if provider == "anthropic":
                summary = _summarize_with_anthropic(prompt, model)
            elif provider == "ollama":
                with httpx.Client(timeout=45) as client:
                    resp = client.post(
                        f"{settings.OLLAMA_BASE_URL}/api/generate",
                        json={"model": model, "prompt": prompt, "stream": False},
                    )
                if resp.status_code == 200:
                    summary = resp.json().get("response", "").strip() or None
            elif provider == "huggingface" and settings.HUGGINGFACE_API_KEY:
                with httpx.Client(timeout=30) as client:
                    resp = client.post(
                        f"https://api-inference.huggingface.co/models/{model}",
                        headers={"Authorization": f"Bearer {settings.HUGGINGFACE_API_KEY}"},
                        json={
                            "inputs": prompt,
                            "parameters": {"max_new_tokens": 200, "temperature": 0.2, "return_full_text": False},
                        },
                    )
                if resp.status_code == 200:
                    result = resp.json()
                    if isinstance(result, list) and result:
                        summary = result[0].get("generated_text", "").strip() or None
        except Exception as e:
            logger.warning("Trade summary failed via %s: %s", provider, e)
            continue

        if summary:
            if db:
                _save_wallet_summary(wallet_address, summary, db)
            return summary

    return None


def invalidate_cache(trade_id: str, db: Session) -> bool:
    """Remove a cached analysis so it will be re-analyzed next time."""
    row = db.query(TradeAnalysis).filter(TradeAnalysis.trade_id == trade_id).first()
    if row:
        db.delete(row)
        db.commit()
        return True
    return False


def invalidate_wallet_summary(wallet_address: str, db: Session) -> bool:
    """Clear the cached wallet summary so it will be regenerated next time."""
    wallet = db.query(Wallet).filter(Wallet.address == wallet_address).first()
    if wallet and wallet.ai_summary:
        wallet.ai_summary = None
        wallet.ai_summary_at = None
        db.commit()
        return True
    return False
