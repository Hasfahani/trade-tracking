#!/usr/bin/env python3
"""Export a full PolySignal JSON backup."""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.backup import backup_json  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a full PolySignal JSON backup")
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output path. Defaults to backups/polysignal_backup_<UTC timestamp>.json",
    )
    args = parser.parse_args()

    output = Path(args.output) if args.output else Path("backups") / f"polysignal_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    try:
        output.write_text(backup_json(db), encoding="utf-8")
    finally:
        db.close()

    print(f"Wrote backup: {output}")


if __name__ == "__main__":
    main()
