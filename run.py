# Starts the PolySignal app.
"""Application entrypoint.

Reads PORT from the environment so the process works whether a host runs this
via exec form (no shell, so ${PORT} would not expand) or via a shell.
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    workers = int(os.environ.get("WEB_CONCURRENCY") or os.environ.get("UVICORN_WORKERS") or "1")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
    )
