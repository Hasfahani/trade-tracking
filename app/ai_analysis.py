"""AI trade analysis using free LLMs (Ollama or Hugging Face API)."""

import logging
import os
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import Trade

logger = logging.getLogger(__name__)

_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_analysis_cache: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _get_provider() -> Optional[str]:
    try:
        with httpx.Client(timeout=2) as client:
            client.get(f"{_OLLAMA_BASE}/api/tags")
        return "ollama"
    except Exception:
        pass
    if os.getenv("HUGGINGFACE_API_KEY"):
        return "huggingface"
    return None


def _get_ollama_model() -> Optional[str]:
    try:
        with httpx.Client(timeout=2) as client:
            resp = client.get(f"{_OLLAMA_BASE}/api/tags")
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                if models:
                    return models[0]["name"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Context gathering — pulls rich stats from the DB so the model has real data
# ---------------------------------------------------------------------------

def build_trade_context(trade: Trade, db: Session) -> Dict[str, Any]:
    """Gather statistical context about a trade for the AI prompt."""
    trade_value_expr = Trade.price * Trade.size

    # Wallet's trades on this exact market
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

    # Wallet's overall portfolio stats
    wallet_overall = db.query(
        func.count(Trade.id).label("total_trades"),
        func.avg(trade_value_expr).label("avg_value"),
        func.count(func.distinct(Trade.condition_id)).label("total_markets"),
        func.sum(case((Trade.side == "YES", 1), else_=0)).label("yes_count"),
        func.sum(case((Trade.side == "NO", 1), else_=0)).label("no_count"),
    ).filter(Trade.wallet_address == trade.wallet_address).first()

    # Market-wide stats across all wallets
    market_all = db.query(
        func.count(Trade.id).label("total_trades"),
        func.sum(case((Trade.side == "YES", 1), else_=0)).label("yes_count"),
        func.sum(case((Trade.side == "NO", 1), else_=0)).label("no_count"),
        func.avg(Trade.price).label("avg_price"),
        func.min(Trade.price).label("min_price"),
        func.max(Trade.price).label("max_price"),
        func.count(func.distinct(Trade.wallet_address)).label("unique_wallets"),
    ).filter(Trade.condition_id == trade.condition_id).first()

    # Average entry price for YES and NO sides on this market
    yes_avg_price = db.query(func.avg(Trade.price)).filter(
        Trade.condition_id == trade.condition_id, Trade.side == "YES"
    ).scalar() or 0.0
    no_avg_price = db.query(func.avg(Trade.price)).filter(
        Trade.condition_id == trade.condition_id, Trade.side == "NO"
    ).scalar() or 0.0

    # Derived values
    market_total = int(market_all.total_trades or 0)
    market_yes = int(market_all.yes_count or 0)
    market_no = int(market_all.no_count or 0)
    market_yes_pct = round(market_yes / market_total * 100) if market_total else 50

    wallet_market_count = int(wallet_market.count or 0)
    this_value = trade.price * trade.size
    wallet_avg_value = float(wallet_overall.avg_value or 0)
    size_vs_avg = round(this_value / wallet_avg_value, 1) if wallet_avg_value > 0 else 1.0

    wallet_total_trades = int(wallet_overall.total_trades or 0)
    wallet_yes_total = int(wallet_overall.yes_count or 0)
    wallet_no_total = int(wallet_overall.no_count or 0)
    wallet_yes_bias_pct = round(wallet_yes_total / wallet_total_trades * 100) if wallet_total_trades else 50

    # Is this the wallet's first ever trade on this market?
    first_trade_at = wallet_market.first_trade_at
    is_first_on_market = wallet_market_count <= 1 or (
        first_trade_at and first_trade_at >= trade.traded_at
    )

    # Price vs the side's market average
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

    # Market sentiment label
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

    # Wallet YES/NO bias label
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

    return {
        "trade_value": round(this_value, 2),
        "market_yes_pct": market_yes_pct,
        "market_sentiment": market_sentiment,
        "market_total_trades": market_total,
        "market_unique_wallets": int(market_all.unique_wallets or 0),
        "market_avg_price": round(float(market_all.avg_price or 0), 4),
        "market_yes_avg_price": round(float(yes_avg_price), 4),
        "market_no_avg_price": round(float(no_avg_price), 4),
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

def _build_prompt(trade: Trade, ctx: Dict[str, Any]) -> str:
    implied_prob = round(trade.price * 100)

    # Contrarian / momentum note
    stance_note = ""
    if trade.side == "YES" and ctx["market_yes_pct"] < 40:
        stance_note = f"CONTRARIAN alert: only {ctx['market_yes_pct']}% of all market trades are YES."
    elif trade.side == "NO" and ctx["market_yes_pct"] > 60:
        stance_note = f"CONTRARIAN alert: {ctx['market_yes_pct']}% of market trades are YES — this wallet is shorting the majority."
    elif trade.side == "YES" and ctx["market_yes_pct"] > 70:
        stance_note = f"MOMENTUM trade: {ctx['market_yes_pct']}% of market trades are YES, this wallet is with the crowd."
    elif trade.side == "NO" and ctx["market_yes_pct"] < 30:
        stance_note = f"MOMENTUM trade: only {ctx['market_yes_pct']}% YES, wallet confirms bearish market consensus."

    # Size note
    if ctx["size_vs_wallet_avg"] >= 2.5:
        size_note = f"HIGH CONVICTION: trade is {ctx['size_vs_wallet_avg']}x the wallet's average — significantly oversized."
    elif ctx["size_vs_wallet_avg"] <= 0.4:
        size_note = f"SMALL position: {ctx['size_vs_wallet_avg']}x wallet average — testing the water or scaling out."
    else:
        size_note = f"Normal position size ({ctx['size_vs_wallet_avg']}x wallet average of ${ctx['wallet_avg_trade_value']})."

    first_trade_line = (
        "This is the wallet's FIRST trade on this market — a new position entry."
        if ctx["is_first_trade_on_market"]
        else f"The wallet has {ctx['wallet_trades_on_this_market']} trades on this market already."
    )

    return f"""You are a quantitative prediction market analyst. Analyze this trade using the data below.
Respond in EXACTLY this format — 5 labeled lines, nothing else:

SIGNAL: [one of: CONTRARIAN | CONSENSUS | CONVICTION | SPECULATIVE]
RISK: [one of: LOW | MEDIUM | HIGH]
PRICE_INSIGHT: [1 sentence — what does paying ${trade.price:.4f} ({implied_prob}% implied probability) say about the trader's view?]
BEHAVIOR: [1 sentence — what does this trade reveal about the wallet's strategy or pattern?]
VERDICT: [1 sharp sentence — the single most important insight about this trade]

=== TRADE ===
Market: {trade.market_title}
Side: {trade.side}
Price: ${trade.price:.4f} → implies {implied_prob}% probability of YES outcome
Trade value: ${ctx['trade_value']:.2f}
Date: {trade.traded_at.strftime('%Y-%m-%d %H:%M UTC')}

=== MARKET CONTEXT (all wallets) ===
Total trades: {ctx['market_total_trades']} across {ctx['market_unique_wallets']} wallets
YES vs NO split: {ctx['market_yes_pct']}% YES — market is {ctx['market_sentiment']}
Average YES entry price: ${ctx['market_yes_avg_price']:.4f} | Average NO entry price: ${ctx['market_no_avg_price']:.4f}
This trade vs consensus: {ctx['price_vs_consensus']}
{stance_note}

=== WALLET CONTEXT ===
Portfolio: {ctx['wallet_total_trades']} trades across {ctx['wallet_total_markets']} markets
Overall bias: {ctx['wallet_bias']} ({ctx['wallet_yes_bias_pct']}% YES historically)
{first_trade_line}
{size_note}

Respond only with the 5 labeled lines. Be direct and analytical."""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "signal": None,
        "risk": None,
        "price_insight": None,
        "behavior": None,
        "verdict": None,
    }
    for line in text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("SIGNAL:"):
            val = line[7:].strip().upper()
            if val in {"CONTRARIAN", "CONSENSUS", "CONVICTION", "SPECULATIVE"}:
                result["signal"] = val
        elif line.upper().startswith("RISK:"):
            val = line[5:].strip().upper()
            if val in {"LOW", "MEDIUM", "HIGH"}:
                result["risk"] = val
        elif line.upper().startswith("PRICE_INSIGHT:"):
            result["price_insight"] = line[14:].strip()
        elif line.upper().startswith("BEHAVIOR:"):
            result["behavior"] = line[9:].strip()
        elif line.upper().startswith("VERDICT:"):
            result["verdict"] = line[8:].strip()
    return result


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------

def _analyze_with_ollama(trade: Trade, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    model = _get_ollama_model()
    if not model:
        return None
    prompt = _build_prompt(trade, ctx)
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{_OLLAMA_BASE}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
        if resp.status_code == 200:
            text = resp.json().get("response", "").strip()
            parsed = _parse_response(text)
            parsed["provider"] = f"Ollama ({model})"
            return parsed
    except Exception as e:
        logger.warning("Ollama analysis failed: %s", e)
    return None


def _analyze_with_huggingface(trade: Trade, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("HUGGINGFACE_API_KEY")
    if not api_key:
        return None
    prompt = _build_prompt(trade, ctx)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.2",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "inputs": prompt,
                    "parameters": {"max_new_tokens": 300, "temperature": 0.2, "return_full_text": False},
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
            return parsed
    except Exception as e:
        logger.warning("HuggingFace analysis failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_trade(trade: Trade, db: Session) -> Optional[Dict[str, Any]]:
    """Analyze a trade with AI. Returns structured dict or None if no provider available."""
    cached = _analysis_cache.get(trade.trade_id)
    if cached:
        return cached

    ctx = build_trade_context(trade, db)
    provider = _get_provider()

    result = None
    if provider == "ollama":
        result = _analyze_with_ollama(trade, ctx)
    elif provider == "huggingface":
        result = _analyze_with_huggingface(trade, ctx)

    if result and "_error" not in result:
        result["context"] = ctx
        _analysis_cache[trade.trade_id] = result

    return result


def get_trade_summary(trades: list, db: Optional[Session] = None) -> Optional[str]:
    """Generate a plain-text summary of recent trades (used for wallet-level summaries)."""
    if not trades:
        return None
    model = _get_ollama_model()
    if not model:
        return None

    lines = [
        f"- {t.side} ${t.price:.4f} × {t.size:.2f} on {(t.market_title or t.condition_id)[:40]}"
        for t in trades[:10]
    ]
    prompt = (
        "You are a prediction market analyst. Summarize this wallet's recent trading activity "
        "in 2-3 sentences. Highlight any patterns, concentration, or notable positioning.\n\n"
        "Trades:\n" + "\n".join(lines)
    )
    try:
        with httpx.Client(timeout=45) as client:
            resp = client.post(
                f"{_OLLAMA_BASE}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
            )
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception as e:
        logger.warning("Trade summary failed: %s", e)
    return None
