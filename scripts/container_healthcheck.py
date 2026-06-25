# Summary: Checks if the app is healthy in Docker.
# Details: It is a command-line helper for setup, maintenance, migration, backup, scoring, or operational checks.
"""Container liveness probe.

Uses only the Python standard library so the runtime image does not need curl.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.request import urlopen


def main() -> int:
    port = os.getenv("PORT", "8000")
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urlopen(url, timeout=4) as response:
            if response.status != 200:
                return 1
            payload = json.loads(response.read().decode("utf-8"))
            return 0 if payload.get("status") == "ok" else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
