"""AI trade analysis using free LLMs (Ollama or Hugging Face API)."""

import logging
from typing import Optional

from app.models import Trade

logger = logging.getLogger(__name__)


def get_ai_provider():
    """Detect which AI provider to use based on environment."""
    import os
    import httpx
    
    # Check if Ollama is running locally
    try:
        with httpx.Client() as client:
            client.get("http://localhost:11434/api/tags", timeout=2)
        return "ollama"
    except Exception:
        pass
    
    # Check for Hugging Face API key
    if os.getenv("HUGGINGFACE_API_KEY"):
        return "huggingface"
    
    return None


def get_ollama_model() -> Optional[str]:
    """Get first available model from Ollama."""
    try:
        import httpx
        with httpx.Client() as client:
            resp = client.get("http://localhost:11434/api/tags", timeout=2)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                if models:
                    model_name = models[0].get("name")
                    logger.info(f"Using Ollama model: {model_name}")
                    return model_name
    except Exception as e:
        logger.warning(f"Error getting Ollama models: {e}")
    return None


def analyze_trade_with_ollama(trade: Trade) -> Optional[str]:
    """Analyze a single trade using Ollama (free, runs locally).
    
    Install Ollama from https://ollama.ai then run:
    ollama pull mistral
    (or any other model)
    """
    try:
        import httpx
        
        model = get_ollama_model()
        if not model:
            logger.warning("No Ollama models available")
            return None
        
        prompt = f"""Analyze this market prediction trade:
- Market: {trade.market_title}
- Side: {trade.side} (betting YES or NO)
- Price: ${trade.price:.4f}
- Size: {trade.size:.2f}
- Date: {trade.traded_at.strftime('%Y-%m-%d %H:%M')}

Provide a brief 1-2 sentence analysis of this trade decision. Is it contrarian, mainstream, etc?"""

        with httpx.Client() as client:
            resp = client.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=30,
            )
        
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception as e:
        logger.warning("Ollama analysis failed: %s", e)
    
    return None


def analyze_trade_with_huggingface(trade: Trade) -> Optional[str]:
    """Analyze a trade using Hugging Face free inference API.
    
    Get free API key at https://huggingface.co/settings/tokens
    Set env var: HUGGINGFACE_API_KEY=hf_...
    """
    try:
        import os
        import httpx
        
        api_key = os.getenv("HUGGINGFACE_API_KEY")
        if not api_key:
            return None
        
        prompt = f"""Trade Analysis: {trade.side} on {trade.market_title} at ${trade.price:.4f}. Brief insight:"""
        
        with httpx.Client() as client:
            resp = client.post(
                "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.1",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"inputs": prompt},
                timeout=10,
            )
        
        if resp.status_code == 200:
            result = resp.json()
            if isinstance(result, list) and result:
                return result[0].get("generated_text", "").replace(prompt, "").strip()
    except Exception as e:
        logger.warning("Hugging Face analysis failed: %s", e)
    
    return None


def analyze_trade(trade: Trade) -> Optional[str]:
    """Analyze a trade using available AI provider."""
    provider = get_ai_provider()
    
    if provider == "ollama":
        return analyze_trade_with_ollama(trade)
    elif provider == "huggingface":
        return analyze_trade_with_huggingface(trade)
    
    return None


def get_trade_summary(trades: list[Trade]) -> Optional[str]:
    """Generate AI summary of recent trades."""
    if not trades:
        return None
    
    try:
        import httpx
        
        model = get_ollama_model()
        if not model:
            return None
        
        trade_list = "\n".join([
            f"- {t.side} ${t.price:.4f} on {t.market_title[:30]}"
            for t in trades[:10]
        ])
        
        prompt = f"""Summarize this trader's recent activity in 2 sentences:
{trade_list}"""
        
        with httpx.Client() as client:
            resp = client.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=30,
            )
        
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception as e:
        logger.warning("Trade summary analysis failed: %s", e)
    
    return None
