"""
Rapid Search — GUI
=================================
Incidents + Changes for the Enabling Group, last 12 months.

Run:
    python sn_incident_kb_gui.py

Requirements:  requests, python-dotenv
Auth:          reuses sn_auth.py from ../SNOW
"""

import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent / "SNOW"))
from sn_auth import get_session

load_dotenv(Path(__file__).parent.parent / "SNOW" / ".env")

SN_INSTANCE    = "https://aztech.service-now.com"
ENABLING_GROUP = "5a666872474062588d608f85536d433f"
DB_PATH        = Path(__file__).parent / "incident_kb.db"
LOOKBACK_DAYS  = 365


C_NAVY   = "#003781"
C_WHITE  = "#FFFFFF"
C_LIGHT  = "#E8EEF6"
C_ACCENT = "#0563C1"
C_BG     = "#F4F6FB"
C_PANEL  = "#FFFFFF"
C_BORDER = "#C8D0DC"
C_TEXT   = "#1A1A2E"
C_GREEN  = "#1A7F37"
C_ORANGE = "#D97706"
C_RED    = "#B91C1C"
C_TEAL   = "#0D7377"

PRIORITY_COLOUR = {"1": C_RED, "2": C_ORANGE, "3": C_NAVY, "4": C_TEXT, "5": C_TEXT}
RISK_SHORT = {"1 - High": "High", "2 - Medium": "Medium", "3 - Low": "Low", "4 - None": "None"}


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _val(record: dict, field: str) -> str:
    v = record.get(field, "")
    if isinstance(v, dict):
        return v.get("display_value") or v.get("value") or ""
    return v or ""


def _paginate(table, query, fields, session, progress_cb=None):
    url, limit, offset, records, retried = (
        f"{SN_INSTANCE}/api/now/table/{table}", 1000, 0, [], False,
    )
    while True:
        resp = requests.get(
            url,
            cookies=session["cookies"],
            headers={"Accept": "application/json",
                     "X-UserToken": session.get("user_token", ""),
                     "X-WantSessionNotificationMessages": "false"},
            params={"sysparm_query": query, "sysparm_fields": ",".join(fields),
                    "sysparm_display_value": "true",
                    "sysparm_limit": limit, "sysparm_offset": offset},
            timeout=30,
        )
        if resp.status_code == 401 and not retried:
            session = get_session(force_relogin=True)
            retried = True
            continue
        resp.raise_for_status()
        batch = resp.json().get("result", [])
        records.extend(batch)
        if progress_cb:
            progress_cb(len(records))
        if len(batch) < limit:
            break
        offset += limit
    return records, session


# ── Database ─────────────────────────────────────────────────────────────────────

def _init_db():
    conn = sqlite3.connect(DB_PATH)

    # If changes table exists but is missing closed_at, drop and recreate it
    cols = {row[1] for row in conn.execute("PRAGMA table_info(changes)").fetchall()}
    if cols and "sys_created_on" not in cols:
        conn.executescript("""
            DROP TABLE IF EXISTS changes_fts;
            DROP TABLE IF EXISTS changes;
        """)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            number TEXT PRIMARY KEY, short_description TEXT,
            description TEXT, close_notes TEXT, work_notes TEXT,
            resolved_by TEXT, resolved_at TEXT,
            category TEXT, subcategory TEXT,
            close_code TEXT, priority TEXT, fetched_at TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS incidents_fts USING fts5(
            number, short_description, description, close_notes, work_notes,
            content=incidents, content_rowid=rowid,
            tokenize="porter unicode61"
        );
        CREATE TABLE IF NOT EXISTS changes (
            number TEXT PRIMARY KEY, short_description TEXT,
            description TEXT, work_notes TEXT, close_notes TEXT,
            assigned_to TEXT, change_request TEXT,
            planned_start_date TEXT, planned_end_date TEXT,
            sys_created_on TEXT, state TEXT, fetched_at TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS changes_fts USING fts5(
            number, short_description, description, work_notes,
            content=changes, content_rowid=rowid,
            tokenize="porter unicode61"
        );
    """)
    conn.commit()
    return conn


def db_build(progress_cb, done_cb, error_cb):
    try:
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

        # ── Incidents ──
        inc_query  = (f"assignment_group={ENABLING_GROUP}^state=6^ORstate=7"
                      f"^resolved_at>={since}^parent_incidentISEMPTY^ORDERBYDESCresolved_at")
        inc_fields = ["number", "short_description", "description",
                      "close_notes", "work_notes", "resolved_by", "resolved_at",
                      "category", "subcategory", "close_code", "priority"]

        progress_cb("Connecting to ServiceNow...")
        session = get_session()

        progress_cb("Fetching incidents...")
        inc_records, session = _paginate(
            "incident", inc_query, inc_fields, session,
            lambda n: progress_cb(f"Incidents: {n} fetched"),
        )

        # ── Change Tasks (CTASKs) ──
        # planned_end_date is empty on this instance — no date filter, fetch all for the group
        chg_query  = (f"assignment_group={ENABLING_GROUP}"
                      f"^ORDERBYDESCsys_created_on")
        chg_fields = ["number", "short_description", "description",
                      "work_notes", "close_notes", "assigned_to",
                      "change_request", "planned_start_date", "planned_end_date",
                      "sys_created_on", "state"]

        progress_cb("Fetching change tasks...")
        chg_records, _ = _paginate(
            "change_task", chg_query, chg_fields, session,
            lambda n: progress_cb(f"Change tasks: {n} fetched"),
        )

        # ── Store ──
        conn = _init_db()
        now  = datetime.now().isoformat()

        inc_rows = [(
            _val(r, "number"), _val(r, "short_description"), _val(r, "description"),
            _val(r, "close_notes"), _val(r, "work_notes"), _val(r, "resolved_by"),
            _val(r, "resolved_at"), _val(r, "category"), _val(r, "subcategory"),
            _val(r, "close_code"), _val(r, "priority"), now,
        ) for r in inc_records]

        chg_rows = [(
            _val(r, "number"), _val(r, "short_description"), _val(r, "description"),
            _val(r, "work_notes"), _val(r, "close_notes"),
            _val(r, "assigned_to"), _val(r, "change_request"),
            _val(r, "planned_start_date"), _val(r, "planned_end_date"),
            _val(r, "sys_created_on"), _val(r, "state"), now,
        ) for r in chg_records]

        conn.executemany("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", inc_rows)
        conn.executemany("INSERT OR REPLACE INTO changes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", chg_rows)
        conn.commit()
        conn.execute("INSERT INTO incidents_fts(incidents_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO changes_fts(changes_fts) VALUES('rebuild')")
        conn.commit()
        conn.close()

        done_cb(len(inc_rows), len(chg_rows))
    except Exception as exc:
        error_cb(str(exc))


def _build_fts_query(raw: str) -> str:
    tokens = [t.strip('"') for t in raw.split() if t.strip('"')]
    return " ".join(f'"{t}"' for t in tokens)


def db_search(raw_query, record_type="all", limit=50):
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    fts_query = _build_fts_query(raw_query)
    results = []

    if record_type in ("all", "incident"):
        fts_sql = """
            SELECT 'INC' AS _type,
                   i.number, i.short_description, i.close_notes, i.work_notes,
                   i.resolved_by AS person, i.resolved_at AS date,
                   i.category, i.subcategory, i.close_code, i.priority,
                   '' AS implementation_plan, '' AS backout_plan, '' AS type, '' AS risk
            FROM   incidents_fts f JOIN incidents i ON i.rowid = f.rowid
            WHERE  incidents_fts MATCH ?
            ORDER  BY bm25(incidents_fts, 1, 10, 3, 2, 2)
            LIMIT  ?
        """
        try:
            results.extend(dict(r) for r in conn.execute(fts_sql, (fts_query, limit)))
        except sqlite3.OperationalError:
            tokens = raw_query.split()
            conds  = " AND ".join(
                "(short_description LIKE ? OR close_notes LIKE ? OR work_notes LIKE ?)"
                for _ in tokens
            )
            params = [p for t in tokens for p in (f"%{t}%", f"%{t}%", f"%{t}%")] + [limit]
            rows = conn.execute(
                f"SELECT 'INC' AS _type, number, short_description, close_notes, work_notes,"
                f" resolved_by AS person, resolved_at AS date, category, subcategory, close_code, priority,"
                f" '' AS implementation_plan, '' AS backout_plan, '' AS type, '' AS risk"
                f" FROM incidents WHERE {conds} ORDER BY resolved_at DESC LIMIT ?",
                params,
            ).fetchall()
            results.extend(dict(r) for r in rows)

    if record_type in ("all", "change"):
        tbl_ok = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='changes'"
        ).fetchone()
        if tbl_ok:
            chg_fts_sql = """
                SELECT 'CHG' AS _type,
                       c.number, c.short_description, c.close_notes, c.work_notes,
                       c.assigned_to AS person,
                       COALESCE(NULLIF(c.planned_end_date,''), c.sys_created_on) AS date,
                       c.change_request, c.planned_start_date, c.state,
                       '' AS category, '' AS subcategory, '' AS close_code, '' AS priority,
                       '' AS implementation_plan, '' AS backout_plan, '' AS type, '' AS risk
                FROM   changes_fts f JOIN changes c ON c.rowid = f.rowid
                WHERE  changes_fts MATCH ?
                ORDER  BY bm25(changes_fts, 1, 10, 3, 2)
                LIMIT  ?
            """
            try:
                results.extend(dict(r) for r in conn.execute(chg_fts_sql, (fts_query, limit)))
            except sqlite3.OperationalError:
                tokens = raw_query.split()
                conds  = " AND ".join(
                    "(short_description LIKE ? OR work_notes LIKE ?)"
                    for _ in tokens
                )
                params = [p for t in tokens for p in (f"%{t}%", f"%{t}%")] + [limit]
                rows = conn.execute(
                    f"SELECT 'CHG' AS _type, number, short_description, close_notes, work_notes,"
                    f" assigned_to AS person,"
                    f" COALESCE(NULLIF(planned_end_date,''), sys_created_on) AS date,"
                    f" change_request, planned_start_date, state,"
                    f" '' AS category, '' AS subcategory, '' AS close_code, '' AS priority,"
                    f" '' AS implementation_plan, '' AS backout_plan, '' AS type, '' AS risk"
                    f" FROM changes WHERE {conds} ORDER BY planned_end_date DESC LIMIT ?",
                    params,
                ).fetchall()
                results.extend(dict(r) for r in rows)

    conn.close()
    results.sort(key=lambda r: r.get("date") or "", reverse=True)
    return results[:limit]


def db_stats():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    inc_total  = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    inc_oldest = conn.execute("SELECT MIN(resolved_at) FROM incidents").fetchone()[0]
    inc_newest = conn.execute("SELECT MAX(resolved_at) FROM incidents").fetchone()[0]
    fetched_at = conn.execute("SELECT MAX(fetched_at)  FROM incidents").fetchone()[0]

    chg_total = 0
    if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='changes'").fetchone():
        chg_total  = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        chg_built  = conn.execute("SELECT MAX(fetched_at) FROM changes").fetchone()[0]
        if chg_built and (not fetched_at or chg_built > fetched_at):
            fetched_at = chg_built

    conn.close()
    return {
        "inc_total": inc_total, "chg_total": chg_total,
        "oldest": (inc_oldest or "")[:10], "newest": (inc_newest or "")[:10],
        "fetched_at": (fetched_at or "")[:19],
    }


# ── GUI ──────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Rapid Search — Enabling Group")
        self.geometry("1200x750")
        self.minsize(900, 600)
        self.configure(bg=C_BG)

        self._results     = {}
        self._type_filter = tk.StringVar(value="all")

        self._build_styles()
        self._build_toolbar()
        self._build_search_bar()
        self._build_main_area()
        self._build_statusbar()
        self._refresh_stats_label()

    # ── Styles ───────────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")

        s.configure("Toolbar.TFrame",  background=C_NAVY)
        s.configure("Card.TFrame",     background=C_PANEL, relief="flat")
        s.configure("BG.TFrame",       background=C_BG)

        s.configure("TLabel",           background=C_BG,    foreground=C_TEXT, font=("Segoe UI", 10))
        s.configure("Title.TLabel",     background=C_NAVY,  foreground=C_WHITE, font=("Segoe UI", 13, "bold"))
        s.configure("Sub.TLabel",       background=C_NAVY,  foreground="#A8C4E8", font=("Segoe UI", 9))
        s.configure("Stats.TLabel",     background=C_BG,    foreground="#555", font=("Segoe UI", 9))
        s.configure("CardTitle.TLabel", background=C_PANEL, foreground=C_NAVY,  font=("Segoe UI", 10, "bold"))
        s.configure("CardText.TLabel",  background=C_PANEL, foreground=C_TEXT,  font=("Segoe UI", 9))
        s.configure("Link.TLabel",      background=C_PANEL, foreground=C_ACCENT,
                    font=("Segoe UI", 9, "underline"), cursor="hand2")

        s.configure("Navy.TButton",   background=C_NAVY,   foreground=C_WHITE,
                    font=("Segoe UI", 9, "bold"), borderwidth=0, focusthickness=0, padding=(14, 6))
        s.map("Navy.TButton",
              background=[("active", "#00509E"), ("pressed", "#002F6C")],
              foreground=[("active", C_WHITE)])

        s.configure("Accent.TButton", background=C_ACCENT, foreground=C_WHITE,
                    font=("Segoe UI", 9, "bold"), borderwidth=0, focusthickness=0, padding=(14, 6))
        s.map("Accent.TButton",
              background=[("active", "#0452A8"), ("pressed", "#033A82")],
              foreground=[("active", C_WHITE)])

        s.configure("Filter.TRadiobutton",
                    background=C_BG, foreground=C_TEXT,
                    font=("Segoe UI", 9), focusthickness=0)
        s.map("Filter.TRadiobutton", background=[("active", C_BG)])

        s.configure("Treeview",
                    background=C_PANEL, fieldbackground=C_PANEL,
                    foreground=C_TEXT, rowheight=28, font=("Segoe UI", 9))
        s.configure("Treeview.Heading",
                    background=C_NAVY, foreground=C_WHITE,
                    font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview.Heading", background=[("active", "#00509E")])
        s.map("Treeview",
              background=[("selected", C_LIGHT)],
              foreground=[("selected", C_TEXT)])

        s.configure("Horizontal.TProgressbar",
                    troughcolor=C_LIGHT, background=C_ACCENT,
                    thickness=6, borderwidth=0)

    # ── Toolbar ──────────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Toolbar.TFrame")
        bar.pack(fill="x")

        ttk.Label(bar, text="Rapid Search", style="Title.TLabel").pack(side="left", padx=16, pady=10)
        ttk.Label(bar, text="Incidents & Changes  ·  Enabling Group  ·  Last 12 months",
                  style="Sub.TLabel").pack(side="left", pady=10)

        self._btn_build = ttk.Button(bar, text="⟳  Build / Refresh DB",
                                     style="Navy.TButton", command=self._on_build)
        self._btn_build.pack(side="right", padx=12, pady=8)

    # ── Search bar ───────────────────────────────────────────────────────────────

    def _build_search_bar(self):
        frame = ttk.Frame(self, style="BG.TFrame")
        frame.pack(fill="x", padx=16, pady=(12, 4))

        PLACEHOLDER = "Search incidents & changes  (e.g.  db2 lockwait,  maintenance window,  CPU alert)"

        entry = tk.Entry(frame, textvariable=tk.StringVar(),
                         font=("Segoe UI", 11), bg=C_PANEL, fg=C_TEXT,
                         relief="flat", bd=0, highlightthickness=1,
                         highlightbackground=C_BORDER, highlightcolor=C_ACCENT,
                         insertbackground=C_TEXT)
        entry.pack(side="left", fill="x", expand=True, ipady=8, ipadx=8)
        entry.bind("<Return>", lambda _: self._on_search())
        entry.insert(0, PLACEHOLDER)
        entry.configure(fg="#999")

        def _focus_in(e):
            if entry.get() == PLACEHOLDER:
                entry.delete(0, "end")
                entry.configure(fg=C_TEXT)

        def _focus_out(e):
            if not entry.get():
                entry.insert(0, PLACEHOLDER)
                entry.configure(fg="#999")

        entry.bind("<FocusIn>",  _focus_in)
        entry.bind("<FocusOut>", _focus_out)
        self._entry       = entry
        self._placeholder = PLACEHOLDER

        # Type filter
        filter_frame = tk.Frame(frame, bg=C_BG)
        filter_frame.pack(side="left", padx=(10, 4))
        for text, val in (("All", "all"), ("INC", "incident"), ("CHG", "change")):
            ttk.Radiobutton(
                filter_frame, text=text, variable=self._type_filter,
                value=val, style="Filter.TRadiobutton",
                command=self._on_search,
            ).pack(side="left", padx=2)

        ttk.Button(frame, text="Search", style="Accent.TButton",
                   command=self._on_search).pack(side="left", padx=(4, 0))

        self._stats_label = ttk.Label(frame, text="", style="Stats.TLabel")
        self._stats_label.pack(side="right", padx=8)

    # ── Main area ─────────────────────────────────────────────────────────────────

    def _build_main_area(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        # Left: results list
        left = ttk.Frame(paned, style="BG.TFrame")
        paned.add(left, weight=3)

        cols = ("rec_type", "number", "prio_risk", "date", "description", "person")
        self._tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")

        self._tree.heading("rec_type",    text="Type")
        self._tree.heading("number",      text="Record #")
        self._tree.heading("prio_risk",   text="P / State")
        self._tree.heading("date",        text="Date")
        self._tree.heading("description", text="Short Description")
        self._tree.heading("person",      text="Resolver / Assigned To")

        self._tree.column("rec_type",    width=45,  anchor="center", stretch=False)
        self._tree.column("number",      width=115, anchor="w",      stretch=False)
        self._tree.column("prio_risk",   width=80,  anchor="center", stretch=False)
        self._tree.column("date",        width=90,  anchor="center", stretch=False)
        self._tree.column("description", width=410, anchor="w")
        self._tree.column("person",      width=150, anchor="w",      stretch=False)

        self._tree.tag_configure("inc",      foreground=C_NAVY)
        self._tree.tag_configure("inc_alt",  foreground=C_NAVY,   background=C_LIGHT)
        self._tree.tag_configure("chg",      foreground=C_TEAL)
        self._tree.tag_configure("chg_alt",  foreground=C_TEAL,   background=C_LIGHT)

        vsb = ttk.Scrollbar(left, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self._tree.pack(side="left", fill="both", expand=True)

        def _hscroll(event):
            delta = -1 if (event.delta > 0 or event.num == 4) else 1
            self._tree.xview_scroll(3 * delta, "units")

        self._tree.bind("<Shift-MouseWheel>", _hscroll)
        self._tree.bind("<Shift-Button-4>",   _hscroll)
        self._tree.bind("<Shift-Button-5>",   _hscroll)
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Double-1>",         self._on_tree_double_click)
        self._tree.bind("<Button-3>",         self._on_tree_right_click)

        self._ctx_menu = tk.Menu(self, tearoff=0)
        self._ctx_menu.add_command(label="Copy Record Number", command=self._copy_selected_number)
        self._ctx_menu.add_command(label="Open in ServiceNow", command=lambda: self._on_open_link(None))

        # Right: detail panel
        right = ttk.Frame(paned, style="Card.TFrame")
        paned.add(right, weight=2)
        self._build_detail_panel(right)

    def _build_detail_panel(self, parent):
        parent.configure(relief="flat")
        tk.Frame(parent, bg=C_NAVY, height=4).pack(fill="x")

        body = tk.Frame(parent, bg=C_PANEL)
        body.pack(fill="both", expand=True, padx=14, pady=10)

        meta = tk.Frame(body, bg=C_PANEL)
        meta.pack(fill="x", pady=(0, 8))

        def _lbl(text, bold=False):
            return tk.Label(meta, text=text, bg=C_PANEL, fg=C_TEXT,
                            font=("Segoe UI", 9, "bold" if bold else "normal"), anchor="w")

        self._lbl_record   = _lbl("Record",      bold=True)
        self._lbl_prio     = _lbl("Priority",    bold=True)
        self._lbl_category = _lbl("Category",    bold=True)
        self._lbl_code     = _lbl("Close Code",  bold=True)
        self._lbl_person   = _lbl("Resolved By", bold=True)
        self._lbl_date     = _lbl("Resolved At", bold=True)

        for row, w in enumerate([self._lbl_record, self._lbl_prio, self._lbl_category,
                                  self._lbl_code, self._lbl_person, self._lbl_date]):
            w.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=1)

        self._dv_number   = tk.StringVar(value="—")
        self._dv_priority = tk.StringVar(value="—")
        self._dv_category = tk.StringVar(value="—")
        self._dv_code     = tk.StringVar(value="—")
        self._dv_resolver = tk.StringVar(value="—")
        self._dv_date     = tk.StringVar(value="—")

        # Row 0: clickable number + copy
        num_frame = tk.Frame(meta, bg=C_PANEL)
        num_frame.grid(row=0, column=1, sticky="w")
        self._num_link = tk.Label(num_frame, textvariable=self._dv_number,
                                   bg=C_PANEL, fg=C_ACCENT,
                                   font=("Segoe UI", 9, "underline"),
                                   anchor="w", cursor="hand2")
        self._num_link.pack(side="left")
        self._num_link.bind("<Button-1>", lambda _: self._on_open_link(None))

        self._copy_btn = tk.Label(num_frame, text=" Copy", bg=C_PANEL, fg="#888",
                                   font=("Segoe UI", 8), cursor="hand2")
        self._copy_btn.pack(side="left", padx=(4, 0))
        self._copy_btn.bind("<Button-1>", self._on_copy_number)

        for row, var in enumerate([self._dv_priority, self._dv_category,
                                    self._dv_code, self._dv_resolver, self._dv_date], start=1):
            tk.Label(meta, textvariable=var, bg=C_PANEL, fg=C_TEXT,
                     font=("Segoe UI", 9), anchor="w").grid(row=row, column=1, sticky="w")

        self._dv_short = tk.StringVar(value="")
        tk.Label(body, textvariable=self._dv_short, bg=C_PANEL, fg=C_NAVY,
                 font=("Segoe UI", 10, "bold"), wraplength=380, justify="left",
                 anchor="w").pack(fill="x", pady=(0, 8))

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=4)

        self._notes_header = tk.Label(body, text="Resolution Notes", bg=C_PANEL, fg=C_NAVY,
                                       font=("Segoe UI", 9, "bold"))
        self._notes_header.pack(anchor="w")

        self._notes_box = scrolledtext.ScrolledText(
            body, wrap="word", font=("Segoe UI", 9),
            bg="#FAFBFD", fg=C_TEXT, relief="flat",
            bd=0, highlightthickness=1, highlightbackground=C_BORDER,
            state="disabled", height=12,
        )
        self._notes_box.pack(fill="both", expand=True, pady=(4, 8))

        self._link_lbl = tk.Label(body, text="", bg=C_PANEL, fg=C_ACCENT,
                                   font=("Segoe UI", 9, "underline"),
                                   cursor="hand2", anchor="w")
        self._link_lbl.pack(fill="x")
        self._link_lbl.bind("<Button-1>", self._on_open_link)
        self._current_url = ""

    # ── Status bar ───────────────────────────────────────────────────────────────

    def _build_statusbar(self):
        tk.Frame(self, bg=C_BORDER, height=1).pack(fill="x")
        bottom = tk.Frame(self, bg=C_BG)
        bottom.pack(fill="x", padx=16)

        self._status_var = tk.StringVar(value="Ready — build the database or search existing records.")
        tk.Label(bottom, textvariable=self._status_var, bg=C_BG, fg="#555",
                 font=("Segoe UI", 8), anchor="w").pack(side="left", pady=4)

        self._progress = ttk.Progressbar(bottom, mode="indeterminate",
                                          style="Horizontal.TProgressbar", length=140)
        self._progress.pack(side="right", pady=6)
        self._progress.pack_forget()

    # ── Event handlers ────────────────────────────────────────────────────────────

    def _on_build(self):
        self._btn_build.configure(state="disabled")
        self._set_status("Connecting to ServiceNow...")
        self._progress.pack(side="right", pady=6)
        self._progress.start(12)

        def _done(inc_count, chg_count):
            self._progress.stop()
            self._progress.pack_forget()
            self._btn_build.configure(state="normal")
            self._set_status(f"Done — {inc_count} incidents + {chg_count} changes stored.")
            self._refresh_stats_label()
            messagebox.showinfo("Done",
                f"Rapid Search built successfully.\n\n"
                f"  {inc_count} incidents\n"
                f"  {chg_count} changes")

        def _err(msg):
            self._progress.stop()
            self._progress.pack_forget()
            self._btn_build.configure(state="normal")
            self._set_status(f"Error: {msg}")
            messagebox.showerror("Error", f"Failed to build database:\n\n{msg}")

        def progress_cb(msg):
            self.after(0, lambda m=msg: self._set_status(m))

        def done_cb(inc_count, chg_count):
            self.after(0, lambda: _done(inc_count, chg_count))

        def error_cb(msg):
            self.after(0, lambda: _err(msg))

        threading.Thread(target=db_build, args=(progress_cb, done_cb, error_cb), daemon=True).start()

    def _on_search(self):
        raw = self._entry.get().strip()
        if not raw or raw == self._placeholder:
            return

        record_type = self._type_filter.get()

        if not DB_PATH.exists():
            messagebox.showwarning("No Database",
                "Database not found.\nPlease click 'Build / Refresh DB' first.")
            return

        self._set_status(f'Searching for: "{raw}"...')
        self._clear_detail()
        self._tree.delete(*self._tree.get_children())

        results = db_search(raw, record_type=record_type)

        if not results:
            self._set_status(f'No results for "{raw}". Try different keywords or switch filter.')
            return

        self._results = {}
        for i, r in enumerate(results):
            rec_type = r.get("_type", "INC")
            if rec_type == "CHG":
                prio_risk = r.get("state") or "?"
                tag = "chg_alt" if i % 2 else "chg"
            else:
                prio_risk = r.get("priority") or "?"
                tag = "inc_alt" if i % 2 else "inc"

            date_str = (r.get("date") or "")[:10]
            self._tree.insert("", "end", iid=r["number"],
                               values=(rec_type, r["number"], prio_risk, date_str,
                                       r.get("short_description", ""), r.get("person") or "—"),
                               tags=(tag,))
            self._results[r["number"]] = r

        type_label = {"all": "incidents & changes", "incident": "incidents",
                      "change": "changes"}[record_type]
        self._set_status(f'{len(results)} result(s) for "{raw}" in {type_label}.')

        first = self._tree.get_children()
        if first:
            self._tree.selection_set(first[0])
            self._tree.focus(first[0])

    def _on_select(self, _event):
        sel = self._tree.selection()
        if not sel:
            return
        number = sel[0]
        r = self._results.get(number, {})
        if not r:
            return

        is_chg = r.get("_type") == "CHG"

        self._lbl_record.configure(  text="Change Task"  if is_chg else "Incident")
        self._lbl_prio.configure(    text="State"         if is_chg else "Priority")
        self._lbl_category.configure(text="Parent CHG"    if is_chg else "Category")
        self._lbl_code.configure(    text="Planned Start" if is_chg else "Close Code")
        self._lbl_person.configure(  text="Assigned To"   if is_chg else "Resolved By")
        self._lbl_date.configure(    text="Planned End"   if is_chg else "Resolved At")
        self._notes_header.configure(text="Work Notes"    if is_chg else "Resolution Notes")

        self._dv_number.set(number)
        self._dv_priority.set(r.get("state") or "—" if is_chg else (r.get("priority") or "?"))
        self._dv_category.set(r.get("change_request") or "—" if is_chg else
                              (" / ".join(filter(None, [r.get("category"), r.get("subcategory")])) or "—"))
        self._dv_code.set((r.get("planned_start_date") or "—")[:16] if is_chg else (r.get("close_code") or "—"))
        self._dv_resolver.set(r.get("person") or "—")
        self._dv_date.set((r.get("date") or "")[:16] or "—")
        self._dv_short.set(r.get("short_description", ""))

        if is_chg:
            close = (r.get("close_notes") or "").strip()
            work  = (r.get("work_notes")  or "").strip()
            notes = close or work or "(No notes recorded for this change task.)"
            url   = f"{SN_INSTANCE}/change_task.do?sysparm_query=number={number}"
        else:
            close = (r.get("close_notes") or "").strip()
            work  = (r.get("work_notes")  or "").strip()
            notes = close or work or "(No resolution notes recorded for this incident.)"
            url   = f"{SN_INSTANCE}/incident.do?sysparm_query=number={number}"

        self._notes_box.configure(state="normal")
        self._notes_box.delete("1.0", "end")
        self._notes_box.insert("1.0", notes)
        self._notes_box.configure(state="disabled")

        self._current_url = url
        self._link_lbl.configure(text=f"Open in ServiceNow  →  {number}")

    def _on_tree_double_click(self, event):
        if self._tree.identify_region(event.x, event.y) == "cell":
            self._on_open_link(None)

    def _on_tree_right_click(self, event):
        row = self._tree.identify_row(event.y)
        if row:
            self._tree.selection_set(row)
            self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _on_copy_number(self, _event=None):
        number = self._dv_number.get()
        if number and number != "—":
            self.clipboard_clear()
            self.clipboard_append(number)
            self._copy_btn.configure(text=" Copied!", fg=C_GREEN)
            self.after(1500, lambda: self._copy_btn.configure(text=" Copy", fg="#888"))

    def _copy_selected_number(self):
        sel = self._tree.selection()
        if sel:
            self.clipboard_clear()
            self.clipboard_append(sel[0])
            self._set_status(f"Copied {sel[0]} to clipboard.")

    def _on_open_link(self, _event):
        if self._current_url:
            webbrowser.open(self._current_url)

    def _clear_detail(self):
        for var in [self._dv_number, self._dv_priority, self._dv_category,
                    self._dv_code, self._dv_resolver, self._dv_date]:
            var.set("—")
        self._dv_short.set("")
        self._notes_box.configure(state="normal")
        self._notes_box.delete("1.0", "end")
        self._notes_box.configure(state="disabled")
        self._link_lbl.configure(text="")
        self._current_url = ""

    def _set_status(self, msg: str):
        self._status_var.set(msg)
        self.update_idletasks()

    def _refresh_stats_label(self):
        stats = db_stats()
        if stats:
            self._stats_label.configure(
                text=(f"{stats['inc_total']} incidents  +  {stats['chg_total']} changes"
                      f"  |  Last built: {stats['fetched_at']}")
            )
        else:
            self._stats_label.configure(text="Database not built yet")
        self._results = {}


if __name__ == "__main__":
    app = App()
    app.mainloop()
