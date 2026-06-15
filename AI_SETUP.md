<!-- Explains how to set up AI analysis providers. -->
# AI Analysis Setup Guide

Your trade tracking app now has AI-powered trade analysis built in!

## Option 1: Ollama (Recommended - Free, runs locally, no internet needed)

### Setup:
1. **Download Ollama** from https://ollama.ai
2. **Install & run it**
3. **Pull a model** (Mistral is fast and free):
   ```powershell
   ollama pull mistral
   ```
4. **Done!** The app will auto-detect Ollama and start using it

### How to use:
- Open any trade detail page
- Click "Get AI Analysis" button (will appear once Ollama is running)
- AI will analyze the trade instantly

---

## Option 2: Hugging Face (Free API tier available)

### Setup:
1. **Get a free account** at https://huggingface.co
2. **Create API token** at https://huggingface.co/settings/tokens
3. **Set environment variable** (PowerShell):
   ```powershell
   $env:HUGGINGFACE_API_KEY="hf_your_token_here"
   ```
4. **Restart your app** and you're ready!

---

## Option 3: OpenAI (Costs money, but very good)

If you want to use ChatGPT for analysis:

1. **Get API key** from https://openai.com/api/
2. **Edit** `app/ai_analysis.py` and add function `analyze_trade_with_openai()`
3. **Add to requirements.txt**: `openai>=1.0.0`

---

## Test it works:

1. **Start your app** normally
2. **Navigate to a trade** (any wallet > Trades > click a trade)
3. **Look for "AI Analysis" button/section**
4. Click it - if Ollama/HuggingFace is running, it will analyze!

---

## API Endpoint:

You can also call it directly:
```
GET /api/trades/{trade_id}/ai-analysis
```

Response:
```json
{
  "trade_id": "xyz123",
  "analysis": "This trader is betting YES on a politics market...",
  "available": true
}
```

---

## What it does:

The AI analyzes:
- What market you're trading on
- Which side (YES or NO)
- Price and size
- Whether the trade is contrarian or mainstream

---

## For Railway deployment:

1. **Ollama** - Not ideal for Railway (needs local GPU/resources)
2. **HuggingFace** - Perfect for Railway! Just add env var:
   ```
   HUGGINGFACE_API_KEY=hf_...
   ```

That's it! Railway will call the free API automatically.

---

## Troubleshooting:

**"No analysis available"**
- Ollama not running? Start it: `ollama serve`
- HuggingFace key wrong? Check the token
- Both not set? The app will just skip AI features (no error)

**App is slow**
- Ollama first request is slow (loading model)
- After first use, it's cached and faster
- Switch to HuggingFace for faster responses

---

## Next: Store AI Analysis in Database

Want to save AI analysis with trades? Let me know and I can add that!
