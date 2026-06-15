#!/usr/bin/env python
# Tests AI analysis on a local trade.
"""Quick test of AI analysis endpoint."""

from app.ai_analysis import analyze_trade
from app.db import SessionLocal
from app.models import Trade

# Get a trade from database
session = SessionLocal()
trade = session.query(Trade).first()

if trade:
    print(f"âœ… Found trade: {trade.trade_id}")
    print(f"   Market: {trade.market_title}")
    print(f"   Side: {trade.side}")
    print(f"   Price: ${trade.price:.4f}")
    print(f"   Size: {trade.size:.2f}")
    print(f"\nðŸ” Testing AI analysis...\n")
    
    analysis = analyze_trade(trade, session)
    if analysis:
        print(f"âœ… AI Analysis:\n{analysis}")
    else:
        print("âš ï¸  No AI provider available (Ollama or HuggingFace not running)")
else:
    print("âŒ No trades in database")

session.close()
