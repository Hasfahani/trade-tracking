#!/usr/bin/env python3
"""
Initialize the database schema.
Run this script once before starting the application.
"""
import sys
from pathlib import Path

# Add parent directory to path to import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import init_db

if __name__ == "__main__":
    print("Initializing database...")
    init_db()
    print("Database initialized successfully!")
    print("Tables created: wallets, trades")
