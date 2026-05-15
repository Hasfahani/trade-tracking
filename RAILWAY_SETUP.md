# Railway Deployment Setup

## Services to Deploy

You need **2 Railway services**:
1. **FastAPI App** - Main web app
2. **Ollama** - AI analysis engine

## Deployment Steps

### 1. Create FastAPI App Service
```bash
railway up
```
- Select "Deploy from GitHub"
- Select this repo
- Railway will auto-detect `railway.json` and `Dockerfile`

### 2. Create Ollama Service (separate)
- New Railway project
- Create from Dockerfile: `Dockerfile.ollama`
- Set port: `11434`
- Name: `ollama`

### 3. Environment Variables (FastAPI Service)

Set these in Railway dashboard → Variables:

```
DATABASE_URL=postgresql://user:pass@postgres-host:5432/trades
OLLAMA_BASE_URL=http://ollama:11434
DASHBOARD_PASSWORD=<secure-password>
SESSION_SECRET_KEY=<secure-secret-key>
LOG_LEVEL=info
```

**For internal Railway networking:**
- If both services in same project: `http://ollama:11434`
- If different projects: Use Railway's domain or public URL

### 4. Link Services (same project)

If deploying to same Railway project:
1. Go to FastAPI service → Variables
2. Add: `OLLAMA_BASE_URL=${{ Ollama.RAILWAY_PUBLIC_URL }}`
3. Or use internal hostname: `http://ollama:11434` (if internal networking enabled)

### 5. Health Check

Once deployed:
```bash
curl https://your-app.railway.app/docs
curl https://your-ollama.railway.app/api/tags
```

## Local Development

Use docker-compose:
```bash
docker-compose up
```

This runs:
- FastAPI on `http://localhost:8000`
- Ollama on `http://localhost:11434`

## Resource Considerations

- **Ollama**: Needs ~4GB RAM, significant disk for models
- **FastAPI**: Can run on Railway free tier
- **Cost**: Ollama service will incur charges on Railway

## Alternative: Local Ollama + Railway App

If Railway Ollama is too expensive:
1. Deploy only FastAPI to Railway
2. Keep Ollama running locally
3. Set `OLLAMA_BASE_URL=http://localhost:11434` for local dev
4. For production, expose local Ollama via tunnel or static IP
