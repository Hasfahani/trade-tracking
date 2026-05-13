#!/usr/bin/env python
"""Quick test of AI analysis endpoint."""

from app.db import SessionLocal
from app.models import Trade
from app.ai_analysis import analyze_trade

# Get a trade from database
session = SessionLocal()
trade = session.query(Trade).first()
session.close()

if trade:
    print(f"✅ Found trade: {trade.trade_id}")
    print(f"   Market: {trade.market_title}")
    print(f"   Side: {trade.side}")
    print(f"   Price: ${trade.price:.4f}")
    print(f"   Size: {trade.size:.2f}")
    print(f"\n🔍 Testing AI analysis...\n")
    
    analysis = analyze_trade(trade)
    if analysis:
        print(f"✅ AI Analysis:\n{analysis}")
    else:
        print("⚠️  No AI provider available (Ollama or HuggingFace not running)")
else:
    print("❌ No trades in database")
