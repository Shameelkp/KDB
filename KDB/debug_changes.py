"""
Check what's actually in the DB right now.
    python debug_changes.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "incident_kb.db"

if not DB_PATH.exists():
    print("DB does not exist")
else:
    conn = sqlite3.connect(DB_PATH)
    inc = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    chg = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(changes)").fetchall()]
    print(f"incidents : {inc}")
    print(f"changes   : {chg}")
    print(f"columns   : {cols}")
    if chg > 0:
        row = conn.execute("SELECT number, state, assigned_to, sys_created_on FROM changes LIMIT 3").fetchall()
        for r in row:
            print(f"  sample: {r}")
    conn.close()
