#!/usr/bin/env python
# Summary: Checks if Ollama is running locally.
# Details: It supports the PolySignal application by keeping setup, UI, tests, or runtime behavior organized.
"""Debug Ollama detection."""

import httpx

print("Testing Ollama connection...")
try:
    resp = httpx.get("http://localhost:11434/api/tags", timeout=2)
    print(f"âœ… Ollama responded: {resp.status_code}")
    print(f"Models: {resp.json()}")
except Exception as e:
    print(f"âŒ Error: {e}")
