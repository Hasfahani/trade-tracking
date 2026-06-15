# Shows examples of using AI trade analysis.
"""Example usage of AI analysis - customize for your needs."""

from app.ai_analysis import analyze_trade, get_trade_summary
from app.models import Trade


# Example 1: Analyze trades in batch
def analyze_recent_trades(trades: list[Trade]):
    """Get AI insights for multiple trades."""
    insights = []
    for trade in trades:
        analysis = analyze_trade(trade)
        if analysis:
            insights.append({
                "trade_id": trade.trade_id,
                "market": trade.market_title,
                "side": trade.side,
                "analysis": analysis,
            })
    return insights


# Example 2: Generate trader summary
def summarize_trader_activity(trades: list[Trade]):
    """Get AI summary of a trader's recent activity."""
    summary = get_trade_summary(trades)
    return summary


# Example 3: Custom analysis with your own LLM
def custom_risk_analysis(trade: Trade) -> str:
    """Analyze trade risk using custom prompt."""
    try:
        import requests
        
        prompt = f"""As a risk analyst, assess this market prediction trade:
Market: {trade.market_title}
Side: {trade.side}
Price: ${trade.price:.4f}
Size: {trade.size:.2f}

Rate the risk level (High/Medium/Low) and explain in 1 sentence."""
        
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "mistral",
                "prompt": prompt,
                "stream": False,
            },
            timeout=10,
        )
        
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"Risk analysis failed: {e}")
    
    return None


# Example 4: Pattern detection
def detect_trading_pattern(trades: list[Trade]) -> str:
    """Detect patterns in trading behavior."""
    try:
        import requests
        
        pattern_text = "\n".join([
            f"{t.side} ${t.price:.4f} x{t.size:.2f}"
            for t in sorted(trades, key=lambda x: x.traded_at)[:20]
        ])
        
        prompt = f"""Identify patterns in these trades:
{pattern_text}

What's the trader's strategy? (1-2 sentences)"""
        
        resp = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "mistral",
                "prompt": prompt,
                "stream": False,
            },
            timeout=10,
        )
        
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"Pattern detection failed: {e}")
    
    return None


# Example 5: To use these in a route:
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Trade, Wallet

router = APIRouter()

@router.get("/api/wallets/{address}/ai-summary")
async def wallet_ai_summary(address: str, db: Session = Depends(get_db)):
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404)
    
    recent_trades = (
        db.query(Trade)
        .filter(Trade.wallet_address == address)
        .order_by(Trade.traded_at.desc())
        .limit(50)
        .all()
    )
    
    summary = summarize_trader_activity(recent_trades)
    pattern = detect_trading_pattern(recent_trades)
    
    return {
        "wallet_address": address,
        "activity_summary": summary,
        "trading_pattern": pattern,
    }
"""
