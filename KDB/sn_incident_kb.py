"""
Rapid Search  (Enabling group)
=====================================================
Fetches all resolved/closed incidents from the last year and stores them in a
local SQLite database with full-text search (FTS5).

Usage:
    python sn_incident_kb.py build              # fetch & build / refresh the DB
    python sn_incident_kb.py search <keywords>  # find similar past incidents
    python sn_incident_kb.py stats              # show DB statistics

Search examples:
    python sn_incident_kb.py search db2 lockwait
    python sn_incident_kb.py search "db2_lockwait_exec_rate"
    python sn_incident_kb.py search linux db2 lata1
"""

import argparse
import sqlite3
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# Reuse auth from the SNOW project one level up
sys.path.insert(0, str(Path(__file__).parent.parent / "SNOW"))
from sn_auth import get_session

load_dotenv(Path(__file__).parent.parent / "SNOW" / ".env")

SN_INSTANCE    = "https://aztech.service-now.com"
ENABLING_GROUP = "5a666872474062588d608f85536d433f"   # A.TEC.DE.CPI.ADR.DBM.DB-ENABLING-TEAM
DB_PATH        = Path(__file__).parent / "incident_kb.db"
LOOKBACK_DAYS  = 365


# ── Helpers ────────────────────────────────────────────────────────────────────

def _val(record: dict, field: str) -> str:
    v = record.get(field, "")
    if isinstance(v, dict):
        return v.get("display_value") or v.get("value") or ""
    return v or ""


def _paginate(table: str, query: str, fields: list[str], session: dict) -> tuple[list[dict], dict]:
    url     = f"{SN_INSTANCE}/api/now/table/{table}"
    limit   = 1000
    offset  = 0
    records: list[dict] = []
    retried = False

    while True:
        resp = requests.get(
            url,
            cookies=session["cookies"],
            headers={
                "Accept":      "application/json",
                "X-UserToken": session.get("user_token", ""),
                "X-WantSessionNotificationMessages": "false",
            },
            params={
                "sysparm_query":         query,
                "sysparm_fields":        ",".join(fields),
                "sysparm_display_value": "true",
                "sysparm_limit":         limit,
                "sysparm_offset":        offset,
            },
            timeout=30,
        )

        if resp.status_code == 401 and not retried:
            print("  Session expired - re-authenticating...")
            session = get_session(force_relogin=True)
            retried = True
            continue

        resp.raise_for_status()
        batch = resp.json().get("result", [])
        records.extend(batch)
        print(f"  ... {len(records)} records fetched", end="\r")

        if len(batch) < limit:
            break
        offset += limit

    print()
    return records, session


# ── Database ───────────────────────────────────────────────────────────────────

def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            number            TEXT PRIMARY KEY,
            short_description TEXT,
            description       TEXT,
            close_notes       TEXT,
            work_notes        TEXT,
            resolved_by       TEXT,
            resolved_at       TEXT,
            category          TEXT,
            subcategory       TEXT,
            close_code        TEXT,
            priority          TEXT,
            fetched_at        TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS incidents_fts USING fts5(
            number,
            short_description,
            description,
            close_notes,
            work_notes,
            content=incidents,
            content_rowid=rowid,
            tokenize="porter unicode61"
        );
    """)
    conn.commit()
    return conn


def _rebuild_fts(conn: sqlite3.Connection):
    conn.execute("INSERT INTO incidents_fts(incidents_fts) VALUES('rebuild')")
    conn.commit()


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_build(args):
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    # Resolved (6) or Closed (7), Enabling group, last year, no child incidents
    query = (
        f"assignment_group={ENABLING_GROUP}"
        f"^state=6^ORstate=7"
        f"^resolved_at>={since}"
        f"^parent_incidentISEMPTY"
        f"^ORDERBYDESCresolved_at"
    )
    fields = [
        "number", "short_description", "description",
        "close_notes", "work_notes",
        "resolved_by", "resolved_at",
        "category", "subcategory",
        "close_code", "priority",
    ]

    print(f"Connecting to {SN_INSTANCE} ...")
    session = get_session()

    print(f"Fetching resolved/closed incidents since {since[:10]} ...")
    records, _ = _paginate("incident", query, fields, session)
    print(f"Fetched {len(records)} incidents.\n")

    conn  = _init_db()
    now   = datetime.now().isoformat()
    rows  = [
        (
            _val(r, "number"),
            _val(r, "short_description"),
            _val(r, "description"),
            _val(r, "close_notes"),
            _val(r, "work_notes"),
            _val(r, "resolved_by"),
            _val(r, "resolved_at"),
            _val(r, "category"),
            _val(r, "subcategory"),
            _val(r, "close_code"),
            _val(r, "priority"),
            now,
        )
        for r in records
    ]

    conn.executemany("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    _rebuild_fts(conn)
    conn.close()

    print(f"Rapid Search DB saved : {DB_PATH}")
    print(f"Total incidents stored: {len(rows)}")
    print("\nReady to search:")
    print("  python sn_incident_kb.py search db2 lockwait")


def cmd_search(args):
    if not DB_PATH.exists():
        print("Database not found. Run:  python sn_incident_kb.py build")
        return

    raw_query = " ".join(args.query)
    limit     = args.limit

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # BM25 weights: short_description ranked highest (10), then description/notes (3/2/2)
    fts_sql = """
        SELECT i.number, i.short_description, i.close_notes, i.work_notes,
               i.resolved_by, i.resolved_at, i.category, i.subcategory,
               i.close_code, i.priority
        FROM   incidents_fts f
        JOIN   incidents i ON i.rowid = f.rowid
        WHERE  incidents_fts MATCH ?
        ORDER  BY bm25(incidents_fts, 1, 10, 3, 2, 2)
        LIMIT  ?
    """

    results = []
    try:
        results = conn.execute(fts_sql, (raw_query, limit)).fetchall()
    except sqlite3.OperationalError:
        # Fallback: LIKE search if FTS query has syntax issues
        like_sql = """
            SELECT number, short_description, close_notes, work_notes,
                   resolved_by, resolved_at, category, subcategory,
                   close_code, priority
            FROM   incidents
            WHERE  short_description LIKE ?
               OR  close_notes LIKE ?
               OR  work_notes  LIKE ?
            ORDER  BY resolved_at DESC
            LIMIT  ?
        """
        pat = f"%{raw_query}%"
        results = conn.execute(like_sql, (pat, pat, pat, limit)).fetchall()

    conn.close()

    if not results:
        print(f'\nNo matches found for: "{raw_query}"')
        print("Try broader keywords, e.g.:")
        print("  python sn_incident_kb.py search db2 lockwait")
        return

    SEP = "─" * 80
    print(f'\nTop {len(results)} match(es) for: "{raw_query}"\n')

    for i, r in enumerate(results, 1):
        url      = f"{SN_INSTANCE}/incident.do?sysparm_query=number={r['number']}"
        date_str = (r["resolved_at"] or "")[:10]
        pri      = r["priority"] or "?"
        resolver = r["resolved_by"] or "—"
        code     = r["close_code"] or "—"
        cat      = " / ".join(filter(None, [r["category"], r["subcategory"]])) or "—"

        print(f"{i}. {r['number']}  [Priority: {pri}]  Resolved: {date_str}  Category: {cat}")
        print(f"   {r['short_description']}")
        print(f"   Resolved by : {resolver}")
        print(f"   Close code  : {code}")
        print(f"   URL         : {url}")

        close = (r["close_notes"] or "").strip()
        work  = (r["work_notes"]  or "").strip()
        notes = close or work or ""

        if notes:
            print("\n   Resolution Notes:")
            wrapped = textwrap.fill(
                notes[:800],
                width=78,
                initial_indent="   ",
                subsequent_indent="   ",
            )
            print(wrapped)
            if len(notes) > 800:
                print("   ... [truncated — open URL for full notes]")
        else:
            print("   (no resolution notes recorded)")

        print(SEP)


def cmd_stats(args):
    if not DB_PATH.exists():
        print("Database not found. Run:  python sn_incident_kb.py build")
        return

    conn = sqlite3.connect(DB_PATH)

    total      = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    oldest     = conn.execute("SELECT MIN(resolved_at) FROM incidents").fetchone()[0]
    newest     = conn.execute("SELECT MAX(resolved_at) FROM incidents").fetchone()[0]
    fetched_at = conn.execute("SELECT MAX(fetched_at)  FROM incidents").fetchone()[0]
    with_notes = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE close_notes != '' OR work_notes != ''"
    ).fetchone()[0]

    by_category = conn.execute("""
        SELECT category, COUNT(*) AS n
        FROM   incidents
        GROUP  BY category
        ORDER  BY n DESC
        LIMIT  10
    """).fetchall()

    conn.close()

    pct = (100 * with_notes // total) if total else 0
    print(f"\nRapid Search DB: {DB_PATH}")
    print(f"  Group           : Enabling (A.TEC.DE.CPI.ADR.DBM.DB-ENABLING-TEAM)")
    print(f"  Total incidents : {total}")
    print(f"  Date range      : {(oldest or '')[:10]} → {(newest or '')[:10]}")
    print(f"  With notes      : {with_notes} / {total} ({pct}%)")
    print(f"  Last built      : {(fetched_at or '')[:19]}")

    if by_category:
        print("\n  Top categories:")
        for cat, n in by_category:
            print(f"    {(cat or 'Unknown'):<30}  {n}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Rapid Search (Enabling group)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python sn_incident_kb.py build
  python sn_incident_kb.py search db2 lockwait
  python sn_incident_kb.py search "db2_lockwait_exec_rate_check"
  python sn_incident_kb.py search linux lata1 db2
  python sn_incident_kb.py stats
        """,
    )
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    sub.add_parser("build", help="Fetch resolved incidents and build / refresh the database")

    sp = sub.add_parser("search", help="Search for similar past incidents")
    sp.add_argument("query", nargs="+", help="Keywords or short description fragment")
    sp.add_argument("-n", "--limit", type=int, default=10, metavar="N",
                    help="Number of results to show (default: 10)")

    sub.add_parser("stats", help="Show Rapid Search statistics")

    args = parser.parse_args()

    if   args.cmd == "build":  cmd_build(args)
    elif args.cmd == "search": cmd_search(args)
    elif args.cmd == "stats":  cmd_stats(args)
    else:                      parser.print_help()


if __name__ == "__main__":
    main()
