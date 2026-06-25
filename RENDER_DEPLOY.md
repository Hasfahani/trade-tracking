<!-- Summary: Explains how to deploy PolySignal on Render. -->
<!-- Details: It gives plain written instructions or reference notes for running, deploying, or understanding the project. -->
# Render Deployment

Render is a good fit for this app if you deploy it with Postgres. Do not use SQLite on Render free web services for real data: Render's service filesystem is ephemeral unless you attach a paid persistent disk.

This repo includes `render.yaml`, so you can deploy it as a Render Blueprint.

## Recommended Setup

1. Push this repo to GitHub.
2. In Render, create a new Blueprint from the repo.
3. Render will create:
   - `polysignal` web service
   - `polysignal-db` Postgres database
4. Set `DASHBOARD_PASSWORD` when Render asks for synced secret values.
5. Deploy.

Render injects `DATABASE_URL` from Postgres and generates `SESSION_SECRET_KEY`.
The app also reads Render's `RENDER_EXTERNAL_URL` and `RENDER_GIT_COMMIT` defaults for canonical URLs and build metadata.

## Import Existing Local Data

After Render creates the Postgres database, copy its internal or external database URL and run this locally:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_to_postgres.py --sqlite data\app.db --postgres "postgresql://USER:PASSWORD@HOST:5432/DB"
```

The migration is safe to re-run. Existing wallets, trades, sync events, app settings, retention tables, and AI analysis cache rows are skipped instead of duplicated.

You can also export and import portable JSON backups:

```powershell
.\.venv\Scripts\python.exe scripts\export_backup.py
.\.venv\Scripts\python.exe scripts\import_backup.py backups\YOUR_BACKUP.json --database-url "postgresql://USER:PASSWORD@HOST:5432/DB" --dry-run
.\.venv\Scripts\python.exe scripts\import_backup.py backups\YOUR_BACKUP.json --database-url "postgresql://USER:PASSWORD@HOST:5432/DB" --yes
```

## Production Checklist

- `APP_ENV=production`
- `DATABASE_URL` points to Render Postgres
- `DASHBOARD_PASSWORD` is set
- `SESSION_SECRET_KEY` is a generated 32+ character secret
- `PUBLIC_BASE_URL` is optional on Render because the app falls back to `RENDER_EXTERNAL_URL`
- `/healthz` returns 200
- `/readyz` returns 200 after startup finishes
- `/admin/ops` shows database `ok`

## Free Tier Notes

Render free web services can spin down after inactivity. The first request after sleep can be slower while the service wakes up. That is normal for the free tier.
