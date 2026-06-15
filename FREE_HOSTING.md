<!-- Explains free hosting options for PolySignal. -->
# Free Hosting After Railway Trial

Railway ending does not mean the app is gone. For this project, the safest no-payment option is to run PolySignal on this PC and expose it with a free Cloudflare Tunnel quick tunnel.

Why this is the recommended free path:
- Your app stores data in local SQLite by default under `data/app.db`.
- Many free web hosts have sleeping apps, ephemeral disks, or no free database.
- Running locally keeps your current database and avoids adding a paid Postgres service.

Tradeoff:
- The public link works only while this computer and the launcher window are running.
- Quick tunnel URLs can change when restarted.

## Start It Publicly For Free

From this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_public_free.ps1
```

Or double-click:

```text
start_public_free.bat
```

The script will:
- Ask you to create a dashboard password if `DASHBOARD_PASSWORD` is not already set.
- Generate a strong session secret for this run if needed.
- Start the app on `http://localhost:8000`.
- Download `cloudflared.exe` into `tools/` if it is missing.
- Print a public `https://*.trycloudflare.com` URL in the Cloudflare output.

Keep the terminal window open. Closing it stops both the app and the public link.

## Optional: Use A Fixed Free Domain

Quick tunnels are easiest, but the URL can change. If you already own a domain on Cloudflare, you can create a named tunnel and route a stable subdomain to this app:

```powershell
.\tools\cloudflared.exe tunnel login
.\tools\cloudflared.exe tunnel create polysignal
.\tools\cloudflared.exe tunnel route dns polysignal app.yourdomain.com
.\tools\cloudflared.exe tunnel run --url http://127.0.0.1:8000 polysignal
```

## Cloud Host Alternatives

Docker hosts such as Koyeb, Render, or similar platforms can run this app with the existing `Dockerfile`, but you must solve persistence first. If the host filesystem resets or sleeps, SQLite data can disappear. For a cloud deployment, use a free Postgres database if available and set:

```text
DATABASE_URL=postgresql://...
DASHBOARD_PASSWORD=...
SESSION_SECRET_KEY=...
APP_ENV=production
```

For a truly no-payment setup with your current data intact, use the local Cloudflare Tunnel launcher.
