#!/usr/bin/env python
"""Debug Ollama detection."""

import httpx

print("Testing Ollama connection...")
try:
    resp = httpx.get("http://localhost:11434/api/tags", timeout=2)
    print(f"✅ Ollama responded: {resp.status_code}")
    print(f"Models: {resp.json()}")
except Exception as e:
    print(f"❌ Error: {e}")
