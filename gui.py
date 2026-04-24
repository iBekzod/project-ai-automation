"""Modern desktop UI for the bot.

Visual:
- Sun Valley `sv_ttk` theme (Win11-style; dark by default).
- Five tabs: Home / Projects / Issues / Settings / Logs.
- Home dashboard: live stats (open issues, active chats, dev count, version),
  big mode toggle, quick action buttons. Refreshes every 3 s.
- Projects tab: tree of every project → repos → linked groups (DB-backed).
- Issues tab: live list of open issues with category badge.
- Settings tab: .env secrets section + DB-backed runtime config + developers
  list + auto-update controls — all editable.
- Logs tab: level-filter dropdown, search box, dark mono canvas.

Background:
- Closing the window minimises to a tray icon (pystray); bot keeps running.
- Right-click tray for Show / Hide / Start / Stop / Quit.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sv_ttk
from PIL import Image, ImageDraw
import pystray

import config
import db
import env_editor
import updater
from bot_controller import BotController

APP_TITLE = "Xonsaroy AI PM Bot"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3

# Settings tab grouping. .env-only keys live in the first section; the
# DB-backed runtime keys live in their own section so the user knows
# editing them takes effect immediately (DB write) rather than requiring
# Stop+Start.
SETTINGS_SECTIONS_ENV: list[tuple[str, list[str]]] = [
    ("Telegram (.env — secrets)", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_DEVELOPER_IDS",
                                   "MONITORED_GROUP_IDS"]),
    ("GitHub (.env — secrets)",   ["GITHUB_TOKEN"]),
]
SETTINGS_KEYS_DB = [
    "GITHUB_REPO", "REPO_PATH", "STAGE_BRANCH", "PROD_BRANCH",
    "TRIGGER_KEYWORDS", "DRY_RUN", "CLAUDE_CLI", "CLAUDE_TIMEOUT",
]

LOG_TAG_COLORS = {
    "DEBUG":    "#888888",
    "INFO":     "#dcdcdc",
    "WARNING":  "#f5b800",
    "ERROR":    "#ff5c5c",
    "CRITICAL": "#ff0000",
}
LOG_LEVELS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

CATEGORY_BADGE = {
    "backend_bug":  "🟢 Backend",
    "frontend_bug": "🔵 Frontend",
    "infra_issue":  "🟠 Infra",
    "user_error":   "⚪ User",
    "unclear":      "🟡 Unclear",
}


# ---------- file logging + queue bridge ----------

def _install_file_handler(env_file: Path) -> None:
    """Write rotating logs to bot.log next to the .env file."""
    log_path = env_file.parent / "bot.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler) and \
                Path(existing.baseFilename) == log_path:
            return
    root.addHandler(handler)


class _QueueLogHandler(logging.Handler):
    """Ships log records (with level) to a thread-safe queue."""

    def __init__(self, q: "queue.Queue[tuple[str, str]]"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait((record.levelname, self.format(record)))
        except queue.Full:
            pass


def _install_log_bridge() -> "queue.Queue[tuple[str, str]]":
    q: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=2000)
    handler = _QueueLogHandler(q)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return q


# ---------- tray icon ----------

def _make_tray_image(running: bool = False) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (60, 200, 100, 255) if running else (140, 140, 140, 255)
    d.ellipse((6, 6, size - 6, size - 6), fill=fill, outline=(40, 40, 40, 255), width=2)
    d.line((22, 22, 42, 42), fill=(255, 255, 255, 230), width=4)
    d.line((42, 22, 22, 42), fill=(255, 255, 255, 230), width=4)
    return img


# ---------- the App ----------

class App(tk.Tk):
    LOG_POLL_MS = 200
    DASH_POLL_MS = 3000           # live refresh on Home/Issues
    LOG_BUFFER_LINES = 2000

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x720")
        self.minsize(900, 600)

        sv_ttk.set_theme("dark")

        # Make sure DB is initialised so the GUI can read projects/issues
        # without depending on the bot being started.
        try:
            db.init()
        except Exception:  # noqa: BLE001
            pass

        self.log_queue = _install_log_bridge()
        self.log_buffer: list[tuple[str, str]] = []   # for filter/search
        self.log_filter_level = tk.StringVar(value="ALL")
        self.log_search = tk.StringVar(value="")

        self.controller = BotController(on_state_change=self._on_state_change)

        self._tray_icon: pystray.Icon | None = None
        self._tray_thread: threading.Thread | None = None
        self._tray_notified_once = False
        self._setup_tray()

        self._build_ui()
        self._pump_logs()
        self._refresh_buttons()
        self._refresh_dashboard()
        self._first_run_nudge()

        self.protocol("WM_DELETE_WINDOW", self._on_close_to_tray)

    # ===== UI build =====

    def _build_ui(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        self.tab_home     = ttk.Frame(self.nb)
        self.tab_projects = ttk.Frame(self.nb)
        self.tab_issues   = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.tab_logs     = ttk.Frame(self.nb)
        self.nb.add(self.tab_home,     text="  🏠  Home  ")
        self.nb.add(self.tab_projects, text="  📁  Projects  ")
        self.nb.add(self.tab_issues,   text="  🎯  Issues  ")
        self.nb.add(self.tab_settings, text="  ⚙  Settings  ")
        self.nb.add(self.tab_logs,     text="  📜  Logs  ")

        self._build_home()
        self._build_projects()
        self._build_issues()
        self._build_settings()
        self._build_logs()

        # Bottom status bar.
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=8)
        self.status_var = tk.StringVar(value=f"⚪ stopped  ·  {config.ENV_FILE.name}")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left")
        self.right_status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.right_status_var,
                  foreground="#888888", anchor="e").pack(side="right")

    # ----- Home tab -----

    def _build_home(self):
        f = self.tab_home

        # Hero: state + control buttons
        hero = ttk.LabelFrame(f, text="Bot status", padding=18)
        hero.pack(fill="x", padx=10, pady=(12, 8))

        top = ttk.Frame(hero)
        top.pack(fill="x")
        self.state_var = tk.StringVar(value="⚪  Stopped")
        ttk.Label(top, textvariable=self.state_var,
                  font=("Segoe UI Variable Display", 22, "bold")).pack(side="left")
        self.state_detail_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.state_detail_var,
                  font=("Segoe UI", 10), foreground="#aaaaaa").pack(side="left", padx=(12, 0))

        btns = ttk.Frame(hero)
        btns.pack(anchor="w", pady=(12, 0))
        self.btn_start  = ttk.Button(btns, text="▶  Start",  width=14,
                                     command=self._on_start, style="Accent.TButton")
        self.btn_pause  = ttk.Button(btns, text="⏸  Pause",  width=14, command=self._on_pause)
        self.btn_resume = ttk.Button(btns, text="⏵  Resume", width=14, command=self._on_resume)
        self.btn_stop   = ttk.Button(btns, text="⏹  Stop",   width=14, command=self._on_stop)
        for i, b in enumerate([self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop]):
            b.grid(row=0, column=i, padx=(0, 8), pady=4)

        # 4 stat cards
        cards = ttk.Frame(f)
        cards.pack(fill="x", padx=10, pady=(8, 8))
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="card")

        self.card_mode_var    = tk.StringVar(value="—")
        self.card_issues_var  = tk.StringVar(value="—")
        self.card_chats_var   = tk.StringVar(value="—")
        self.card_version_var = tk.StringVar(value="—")
        self._make_card(cards, 0, "🧪 Mode",          self.card_mode_var)
        self._make_card(cards, 1, "🎯 Open issues",   self.card_issues_var)
        self._make_card(cards, 2, "💬 Active chats",  self.card_chats_var)
        self._make_card(cards, 3, "📦 Version",       self.card_version_var)

        # Quick actions row
        actions = ttk.LabelFrame(f, text="Quick actions", padding=14)
        actions.pack(fill="x", padx=10, pady=(8, 8))
        for i in range(4):
            actions.columnconfigure(i, weight=1)
        ttk.Button(actions, text="🔁  Toggle DRY_RUN",
                   command=self._on_toggle_dry).grid(row=0, column=0, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="📦  Check updates",
                   command=self._on_check_updates).grid(row=0, column=1, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="♻️  Apply update",
                   command=self._on_apply_update).grid(row=0, column=2, padx=4, pady=4, sticky="we")
        ttk.Button(actions, text="🔄  Refresh",
                   command=self._refresh_dashboard).grid(row=0, column=3, padx=4, pady=4, sticky="we")

        # Recent activity (last 5 issues)
        recent = ttk.LabelFrame(f, text="Recent issues", padding=12)
        recent.pack(fill="both", expand=True, padx=10, pady=(8, 12))
        self.recent_txt = tk.Text(recent, height=8, wrap="word", state="disabled",
                                  font=("Cascadia Mono", 10), borderwidth=0, relief="flat")
        self.recent_txt.pack(fill="both", expand=True)

    def _make_card(self, parent: ttk.Frame, col: int, title: str, var: tk.StringVar):
        card = ttk.LabelFrame(parent, text=title, padding=14)
        card.grid(row=0, column=col, sticky="nsew", padx=4)
        ttk.Label(card, textvariable=var,
                  font=("Segoe UI", 14, "bold"),
                  wraplength=240, justify="left").pack(anchor="w", fill="x")

    # ----- Projects tab -----

    def _build_projects(self):
        f = self.tab_projects
        header = ttk.Frame(f)
        header.pack(fill="x", padx=10, pady=(12, 6))
        ttk.Label(header, text="Projects · repos · groups",
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(header, text="🔄 Refresh", command=self._refresh_projects).pack(side="right")

        cols = ("kind", "id_or_role", "details")
        self.proj_tree = ttk.Treeview(f, columns=cols, show="tree headings", height=20)
        self.proj_tree.heading("#0", text="")
        self.proj_tree.heading("kind", text="Type")
        self.proj_tree.heading("id_or_role", text="ID / Role")
        self.proj_tree.heading("details", text="Details")
        self.proj_tree.column("#0", width=30, stretch=False)
        self.proj_tree.column("kind", width=110, stretch=False)
        self.proj_tree.column("id_or_role", width=180, stretch=False)
        self.proj_tree.column("details", width=560, stretch=True)

        sb = ttk.Scrollbar(f, orient="vertical", command=self.proj_tree.yview)
        self.proj_tree.configure(yscrollcommand=sb.set)
        self.proj_tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 12))
        sb.pack(side="right", fill="y", pady=(0, 12), padx=(0, 10))

        self._refresh_projects()

    def _refresh_projects(self):
        # Clear tree
        for row in self.proj_tree.get_children():
            self.proj_tree.delete(row)
        try:
            projects = db.list_projects()
        except Exception:
            projects = []
        if not projects:
            self.proj_tree.insert("", "end", text="(empty)", values=(
                "—", "—", "Loyihalar yo'q. Bot start bo'lganda .env'dan auto-yaratiladi.",
            ))
            return
        for p in projects:
            pid = p.get("id") or "?"
            descr = p.get("description") or ""
            top = self.proj_tree.insert(
                "", "end", text="🌐",
                values=("project", pid, f"{p.get('name', '')} — {descr}"),
                open=True,
            )
            for r in db.list_repos(pid):
                self.proj_tree.insert(
                    top, "end", text="📂",
                    values=("repo", r["role"],
                            f"{r['github_repo']}  ({r['stage_branch']} → {r['prod_branch']})  ·  {r['repo_path']}"),
                )
            groups = db.groups_for_project(pid)
            if groups:
                self.proj_tree.insert(
                    top, "end", text="👥",
                    values=("groups", "", ", ".join(str(g) for g in groups)),
                )

    # ----- Issues tab -----

    def _build_issues(self):
        f = self.tab_issues
        header = ttk.Frame(f)
        header.pack(fill="x", padx=10, pady=(12, 6))
        ttk.Label(header, text="Open issues",
                  font=("Segoe UI", 12, "bold")).pack(side="left")
        ttk.Button(header, text="🔄 Refresh", command=self._refresh_issues).pack(side="right")

        cols = ("id", "category", "project", "group", "summary", "created")
        self.issues_tree = ttk.Treeview(f, columns=cols, show="headings", height=20)
        for c, w in (("id", 90), ("category", 130), ("project", 160),
                     ("group", 220), ("summary", 360), ("created", 130)):
            self.issues_tree.heading(c, text=c.title())
            self.issues_tree.column(c, width=w, stretch=(c == "summary"))
        sb = ttk.Scrollbar(f, orient="vertical", command=self.issues_tree.yview)
        self.issues_tree.configure(yscrollcommand=sb.set)
        self.issues_tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 12))
        sb.pack(side="right", fill="y", pady=(0, 12), padx=(0, 10))

    def _refresh_issues(self):
        for row in self.issues_tree.get_children():
            self.issues_tree.delete(row)
        try:
            issues = db.load_open_issues()
        except Exception:
            issues = []
        if not issues:
            self.issues_tree.insert("", "end", values=(
                "—", "—", "—", "—", "Ochiq muammo yo'q.", "—",
            ))
            return
        for it in issues:
            cat = (it.get("diagnosis") or {}).get("category") or "?"
            badge = CATEGORY_BADGE.get(cat, cat)
            proj = it.get("project_id") or "—"
            role = it.get("repo_role") or "—"
            project = f"{proj}/{role}"
            grp = it.get("group_title") or str(it.get("group_id") or "")
            summary = ((it.get("diagnosis") or {}).get("summary") or it.get("message") or "")[:120]
            created = (it.get("created_at") or "")[:16].replace("T", " ")
            self.issues_tree.insert("", "end", values=(
                it.get("id"), badge, project, grp[:40], summary, created,
            ))

    # ----- Settings tab -----

    def _build_settings(self):
        outer = self.tab_settings

        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scroll.pack(side="right", fill="y")

        form = ttk.Frame(canvas)
        form_id = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(form_id, width=e.width))

        env_values = env_editor.read_env(config.ENV_FILE)
        self._env_entries: dict[str, tk.Variable] = {}
        self._db_entries: dict[str, tk.Variable] = {}
        fields_by_key = {f.key: f for f in env_editor.FIELDS}

        # Section 1: .env secrets
        for section_title, keys in SETTINGS_SECTIONS_ENV:
            section = ttk.LabelFrame(form, text=section_title, padding=14)
            section.pack(fill="x", padx=4, pady=(0, 12))
            section.columnconfigure(1, weight=1)
            for row_idx, key in enumerate(keys):
                fld = fields_by_key.get(key)
                if fld is None:
                    continue
                self._render_field(section, row_idx, fld, env_values.get(key, ""), self._env_entries)

        # Section 2: DB-backed runtime settings
        section = ttk.LabelFrame(form, text="Runtime settings (DB — live)", padding=14)
        section.pack(fill="x", padx=4, pady=(0, 12))
        section.columnconfigure(1, weight=1)
        for row_idx, key in enumerate(SETTINGS_KEYS_DB):
            fld = fields_by_key.get(key)
            if fld is None:
                continue
            current = db.get_setting(key) or env_values.get(key, "")
            self._render_field(section, row_idx, fld, current, self._db_entries)

        # Section 3: Auto-update controls
        au_section = ttk.LabelFrame(form, text="Auto-update (optional)", padding=14)
        au_section.pack(fill="x", padx=4, pady=(0, 12))
        au_section.columnconfigure(1, weight=1)
        self._au_hours = tk.StringVar(value=str(db.get_setting("update_check_hours") or "0"))
        self._au_apply = tk.StringVar(
            value="true" if (db.get_setting("update_auto_apply") or "false").lower() == "true" else "false"
        )
        self._au_branch = tk.StringVar(value=db.get_setting("update_branch") or "main")
        ttk.Label(au_section, text="Tekshirish oraligi (soat, 0 = o'chirilgan)").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=8)
        ttk.Entry(au_section, textvariable=self._au_hours, width=8).grid(
            row=0, column=1, sticky="w", pady=8)
        ttk.Label(au_section, text="Avto-pull + restart (yangi versiya kelganda)").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=8)
        ttk.Combobox(au_section, values=["true", "false"], textvariable=self._au_apply,
                     state="readonly", width=10).grid(row=1, column=1, sticky="w", pady=8)
        ttk.Label(au_section, text="Branch").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=8)
        ttk.Entry(au_section, textvariable=self._au_branch, width=20).grid(
            row=2, column=1, sticky="w", pady=8)

        # Section 4: Developers list
        dev_section = ttk.LabelFrame(form, text="Developers (DB allow-list)", padding=14)
        dev_section.pack(fill="x", padx=4, pady=(0, 12))
        self.dev_list = tk.Text(dev_section, height=4, wrap="none", state="disabled",
                                font=("Cascadia Mono", 10))
        self.dev_list.pack(fill="x", padx=0, pady=(0, 8))
        dev_row = ttk.Frame(dev_section)
        dev_row.pack(fill="x")
        self._new_dev_id = tk.StringVar(value="")
        self._new_dev_label = tk.StringVar(value="")
        ttk.Entry(dev_row, textvariable=self._new_dev_id, width=15).pack(side="left", padx=(0, 8))
        ttk.Entry(dev_row, textvariable=self._new_dev_label, width=24).pack(side="left", padx=(0, 8))
        ttk.Button(dev_row, text="➕ Add", command=self._on_add_dev).pack(side="left")
        ttk.Button(dev_row, text="🗑 Remove (id above)", command=self._on_remove_dev).pack(side="left", padx=8)
        self._refresh_dev_list()

        # Action row
        btn_row = ttk.Frame(form)
        btn_row.pack(anchor="w", padx=4, pady=(4, 16))
        ttk.Button(btn_row, text="💾  Save settings", style="Accent.TButton",
                   command=self._on_save_settings).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="↻  Reload from disk/DB",
                   command=self._on_reload_settings).pack(side="left")
        ttk.Label(form, text="* required field — must be filled before Start",
                  foreground="#888").pack(anchor="w", padx=4, pady=(0, 16))

    def _render_field(self, parent: ttk.LabelFrame, row: int,
                      fld: "env_editor.Field", current: str,
                      target_dict: dict[str, tk.Variable]):
        label = f"{fld.label}{'  *' if fld.required else ''}"
        ttk.Label(parent, text=label).grid(
            row=row * 2, column=0, sticky="w", padx=(0, 12), pady=(8, 0),
        )
        if fld.kind == "bool":
            var = tk.StringVar(
                value="true" if current.strip().lower() in ("1", "true", "yes", "on") else "false",
            )
            cb = ttk.Combobox(parent, values=["true", "false"], textvariable=var,
                              state="readonly", width=12)
            cb.grid(row=row * 2, column=1, sticky="w", padx=0, pady=(8, 0))
            target_dict[fld.key] = var
        elif fld.kind == "path":
            var = tk.StringVar(value=current)
            wrap = ttk.Frame(parent)
            wrap.grid(row=row * 2, column=1, sticky="we", pady=(8, 0))
            wrap.columnconfigure(0, weight=1)
            ttk.Entry(wrap, textvariable=var).grid(row=0, column=0, sticky="we")
            ttk.Button(wrap, text="📁  Browse…",
                       command=lambda v=var: self._browse_dir(v)).grid(row=0, column=1, padx=(8, 0))
            target_dict[fld.key] = var
        else:
            var = tk.StringVar(value=current)
            entry = ttk.Entry(parent, textvariable=var, show="•" if fld.secret else "")
            entry.grid(row=row * 2, column=1, sticky="we", pady=(8, 0))
            target_dict[fld.key] = var
        if fld.hint:
            ttk.Label(parent, text=fld.hint, foreground="#888888",
                      font=("Segoe UI", 9)).grid(
                row=row * 2 + 1, column=1, sticky="w", padx=0, pady=(2, 0))

    # ----- Logs tab -----

    def _build_logs(self):
        f = self.tab_logs
        header = ttk.Frame(f)
        header.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(header, text="Live log stream",
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(header, text="🗑  Clear", command=self._clear_logs).pack(side="right")
        ttk.Combobox(header, values=LOG_LEVELS, textvariable=self.log_filter_level,
                     state="readonly", width=10).pack(side="right", padx=(0, 8))
        ttk.Label(header, text="Level:").pack(side="right", padx=(0, 4))
        search_entry = ttk.Entry(header, textvariable=self.log_search, width=24)
        search_entry.pack(side="right", padx=(0, 8))
        ttk.Label(header, text="Search:").pack(side="right", padx=(0, 4))
        self.log_filter_level.trace_add("write", lambda *_: self._rerender_logs())
        self.log_search.trace_add("write", lambda *_: self._rerender_logs())

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scroll = ttk.Scrollbar(wrap, orient="vertical")
        scroll.pack(side="right", fill="y")
        self.log_txt = tk.Text(wrap, wrap="word", state="disabled",
                               font=("Cascadia Mono", 9),
                               bg="#1c1c1c", fg="#dcdcdc",
                               insertbackground="#dcdcdc",
                               borderwidth=0, relief="flat",
                               yscrollcommand=scroll.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.log_txt.yview)
        for level, color in LOG_TAG_COLORS.items():
            self.log_txt.tag_configure(level, foreground=color)

    # ===== Tray =====

    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Hide", self._tray_hide),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start bot", self._tray_start),
            pystray.MenuItem("Stop bot",  self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "xonsaroy_bot", _make_tray_image(False), APP_TITLE, menu,
        )
        self._tray_thread = threading.Thread(
            target=self._tray_icon.run, name="tray", daemon=True,
        )
        self._tray_thread.start()

    def _refresh_tray_icon(self):
        if not self._tray_icon:
            return
        running = self.controller.state in ("running", "paused", "starting")
        try:
            self._tray_icon.icon = _make_tray_image(running)
            self._tray_icon.title = f"{APP_TITLE} · {self.controller.state}"
        except Exception:  # noqa: BLE001
            pass

    def _tray_show(self, _icon=None, _item=None):  self.after(0, self._show_window)
    def _tray_hide(self, _icon=None, _item=None):  self.after(0, self.withdraw)
    def _tray_start(self, _icon=None, _item=None): self.after(0, self._on_start)
    def _tray_stop(self, _icon=None, _item=None):  self.after(0, self._on_stop)
    def _tray_quit(self, _icon=None, _item=None):  self.after(0, self._real_quit)

    def _show_window(self):
        self.deiconify(); self.lift(); self.focus_force()

    # ===== Actions =====

    def _browse_dir(self, var: tk.Variable):
        current = var.get() or ""
        chosen = filedialog.askdirectory(
            initialdir=current if current else None,
            title="Select repo folder",
        )
        if chosen:
            var.set(chosen)

    def _on_start(self):
        values = env_editor.read_env(config.ENV_FILE)
        missing = env_editor.missing_required(values)
        if missing:
            messagebox.showerror(
                APP_TITLE,
                "Missing required settings:\n  • " + "\n  • ".join(missing),
            )
            self.nb.select(self.tab_settings)
            return
        ok, err = self.controller.start()
        if not ok:
            messagebox.showwarning(APP_TITLE, err or "could not start")
        self._refresh_dashboard()

    def _on_pause(self):
        ok, err = self.controller.pause()
        if not ok:
            messagebox.showinfo(APP_TITLE, err or "cannot pause")

    def _on_resume(self):
        ok, err = self.controller.resume()
        if not ok:
            messagebox.showinfo(APP_TITLE, err or "cannot resume")

    def _on_stop(self):
        self.config(cursor="watch"); self.update_idletasks()
        try:
            self.controller.stop()
        finally:
            self.config(cursor="")

    def _on_toggle_dry(self):
        try:
            new_val = "false" if config.DRY_RUN else "true"
            db.set_setting("DRY_RUN", new_val)
            config.reload()
            self._refresh_dashboard()
            messagebox.showinfo(APP_TITLE, f"DRY_RUN → {new_val}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Toggle failed: {exc}")

    def _on_check_updates(self):
        try:
            cur = updater.current_commit()
            remote = updater.latest_remote_commit()
            if not remote:
                messagebox.showwarning(APP_TITLE, "Could not reach GitHub.")
                return
            if cur == remote["sha"]:
                messagebox.showinfo(APP_TITLE, "Eng so'nggi versiyada.")
            else:
                messagebox.showinfo(
                    APP_TITLE,
                    f"Yangi versiya: {remote['sha'][:8]}\n{remote['message']}\n\n"
                    "Tap 'Apply update' to pull + restart.",
                )
                self._refresh_dashboard()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Check failed: {exc}")

    def _on_apply_update(self):
        if not updater.is_git_clone():
            messagebox.showerror(APP_TITLE, "Not a git clone — auto-update unavailable.")
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "git pull + bot restart bo'ladi. Davom etamizmi?"):
            return
        ok, output = updater.apply_update()
        if not ok:
            messagebox.showerror(APP_TITLE, f"Update failed:\n{output[:500]}")
            return
        messagebox.showinfo(APP_TITLE, f"Yangilandi:\n{output[:300]}\n\nBot qayta ishga tushadi.")
        # Stop the bot cleanly first, then restart the GUI process.
        try:
            self.controller.stop()
        except Exception:
            pass
        updater.restart_bot()

    def _on_save_settings(self):
        # .env section
        env_values = {k: v.get().strip() for k, v in self._env_entries.items()}
        try:
            env_editor.save_env(config.ENV_FILE, env_values)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f".env save failed: {exc}")
            return
        # DB section
        for key, var in self._db_entries.items():
            db.set_setting(key, var.get().strip())
        # Auto-update
        try:
            db.set_setting("update_check_hours", self._au_hours.get().strip() or "0")
            db.set_setting("update_auto_apply", self._au_apply.get())
            db.set_setting("update_branch", self._au_branch.get().strip() or "main")
        except Exception:
            pass

        config.reload()
        self._refresh_dashboard()
        msg = "Saqlandi."
        if self.controller.state in ("running", "paused"):
            msg += " .env o'zgarishlari uchun Stop+Start tavsiya qilinadi."
        messagebox.showinfo(APP_TITLE, msg)

    def _on_reload_settings(self):
        env_values = env_editor.read_env(config.ENV_FILE)
        for key, var in self._env_entries.items():
            v = env_values.get(key, "")
            if isinstance(var, tk.StringVar):
                var.set(v)
        for key, var in self._db_entries.items():
            v = db.get_setting(key) or env_values.get(key, "")
            if isinstance(var, tk.StringVar):
                var.set(v)
        self._au_hours.set(str(db.get_setting("update_check_hours") or "0"))
        self._au_apply.set("true" if (db.get_setting("update_auto_apply") or "false").lower() == "true" else "false")
        self._au_branch.set(db.get_setting("update_branch") or "main")
        config.reload()
        self._refresh_dashboard()
        self._refresh_dev_list()

    def _on_add_dev(self):
        try:
            uid = int(self._new_dev_id.get().strip())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "User ID raqam bo'lishi kerak.")
            return
        label = self._new_dev_label.get().strip() or None
        try:
            db.add_developer(uid, label)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Add failed: {exc}")
            return
        self._new_dev_id.set("")
        self._new_dev_label.set("")
        self._refresh_dev_list()

    def _on_remove_dev(self):
        try:
            uid = int(self._new_dev_id.get().strip())
        except ValueError:
            messagebox.showwarning(APP_TITLE, "Birinchi maydonga ID kiriting (raqam).")
            return
        if not messagebox.askyesno(APP_TITLE, f"Dev {uid} ni o'chirish?"):
            return
        db.remove_developer(uid)
        self._new_dev_id.set("")
        self._refresh_dev_list()

    def _refresh_dev_list(self):
        try:
            devs = db.list_developers()
        except Exception:
            devs = []
        self.dev_list.configure(state="normal")
        self.dev_list.delete("1.0", "end")
        if not devs:
            self.dev_list.insert("end", "(yo'q)")
        for d in devs:
            label = d.get("label") or "—"
            added = (d.get("added_at") or "")[:16].replace("T", " ")
            self.dev_list.insert("end", f"{d['user_id']:>14}  ·  {label:<24}  ·  {added}\n")
        self.dev_list.configure(state="disabled")

    def _clear_logs(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")
        self.log_buffer.clear()

    # ===== State + pumps =====

    def _on_state_change(self, new: str, detail: str | None):
        self.after(0, lambda: self._apply_state(new, detail))

    def _apply_state(self, new: str, detail: str | None):
        ICONS = {"stopped": "⚪", "starting": "🟡", "running": "🟢",
                 "paused": "🟠", "stopping": "🟡"}
        icon = ICONS.get(new, "⚪")
        self.state_var.set(f"{icon}  {new.capitalize()}")
        self.state_detail_var.set(detail or "")
        self.status_var.set(f"{icon} {new}  ·  {config.ENV_FILE.name}")
        self._refresh_buttons()
        self._refresh_dashboard()
        self._refresh_tray_icon()

    def _refresh_buttons(self):
        s = self.controller.state
        self.btn_start.configure(state="normal" if s == "stopped" else "disabled")
        self.btn_pause.configure(state="normal" if s == "running" else "disabled")
        self.btn_resume.configure(state="normal" if s == "paused" else "disabled")
        self.btn_stop.configure(state="normal" if s in ("running", "paused", "starting") else "disabled")

    def _refresh_dashboard(self):
        # Mode card
        mode = "🧪 DRY_RUN" if config.DRY_RUN else "🚀 LIVE"
        self.card_mode_var.set(mode)
        # Issues card
        try:
            issues = db.load_open_issues()
            n = len(issues)
            cats: dict[str, int] = {}
            for it in issues:
                c = (it.get("diagnosis") or {}).get("category") or "?"
                cats[c] = cats.get(c, 0) + 1
            sub = ", ".join(f"{k}:{v}" for k, v in sorted(cats.items())) if cats else ""
            self.card_issues_var.set(f"{n}\n{sub}" if sub else f"{n}")
        except Exception:
            self.card_issues_var.set("?")
            issues = []
        # Chats card
        try:
            chat_count = 0
            for dev_id in (config.TELEGRAM_DEVELOPER_IDS or []):
                chat_count += len(db.list_chats(dev_id))
            self.card_chats_var.set(f"{chat_count}")
        except Exception:
            self.card_chats_var.set("?")
        # Version card
        try:
            cur = updater.current_commit()
            self.card_version_var.set((cur[:8] + "…") if cur else "(no git)")
        except Exception:
            self.card_version_var.set("?")

        # Recent issues feed
        self.recent_txt.configure(state="normal")
        self.recent_txt.delete("1.0", "end")
        if not issues:
            self.recent_txt.insert("end", "(no open issues)")
        else:
            for it in issues[:5]:
                cat = (it.get("diagnosis") or {}).get("category") or "?"
                badge = CATEGORY_BADGE.get(cat, cat)
                grp = (it.get("group_title") or str(it.get("group_id") or ""))[:40]
                summary = ((it.get("diagnosis") or {}).get("summary")
                           or it.get("message") or "")[:120]
                self.recent_txt.insert(
                    "end",
                    f"[{it.get('id')}] {badge}  {grp}\n  {summary}\n\n",
                )
        self.recent_txt.configure(state="disabled")

        # Update right status bar with counters
        n_issues = len(issues)
        self.right_status_var.set(
            f"v{updater.current_commit()[:8] if updater.current_commit() else '—'}  ·  "
            f"{n_issues} open  ·  {self.card_chats_var.get()} chats"
        )

        # Schedule next refresh
        if not getattr(self, "_dash_job", None) or True:
            self._dash_job = self.after(self.DASH_POLL_MS, self._refresh_dashboard)

    def _pump_logs(self):
        drained = 0
        while drained < 200:
            try:
                level, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_buffer.append((level, line))
            if len(self.log_buffer) > self.LOG_BUFFER_LINES:
                self.log_buffer = self.log_buffer[-self.LOG_BUFFER_LINES:]
            self._append_log_line(level, line)
            drained += 1
        self.after(self.LOG_POLL_MS, self._pump_logs)

    def _append_log_line(self, level: str, line: str):
        if not self._log_passes_filter(level, line):
            return
        self.log_txt.configure(state="normal")
        tag = level if level in LOG_TAG_COLORS else "INFO"
        self.log_txt.insert("end", line + "\n", tag)
        line_count = int(self.log_txt.index("end-1c").split(".")[0])
        if line_count > self.LOG_BUFFER_LINES:
            self.log_txt.delete("1.0", "500.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _log_passes_filter(self, level: str, line: str) -> bool:
        wanted = self.log_filter_level.get()
        if wanted != "ALL" and level != wanted:
            return False
        q = (self.log_search.get() or "").strip().lower()
        if q and q not in line.lower():
            return False
        return True

    def _rerender_logs(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")
        for level, line in self.log_buffer[-self.LOG_BUFFER_LINES:]:
            self._append_log_line(level, line)

    def _first_run_nudge(self):
        values = env_editor.read_env(config.ENV_FILE)
        if env_editor.missing_required(values):
            self.nb.select(self.tab_settings)

    # ===== Closing =====

    def _on_close_to_tray(self):
        self.withdraw()
        if self._tray_icon and not self._tray_notified_once:
            try:
                self._tray_icon.notify(
                    "Oyna yashirildi, lekin bot ishlashda davom etmoqda. "
                    "Boshqarish uchun tray belgisini bosing.",
                    APP_TITLE,
                )
                self._tray_notified_once = True
            except Exception:
                pass

    def _real_quit(self):
        if self.controller.state != "stopped":
            try:
                self.controller.stop()
            except Exception:
                pass
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        try:
            self.destroy()
        finally:
            import os
            os._exit(0)


def main():
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    env_editor.ensure_env_file(config.ENV_FILE)
    _install_file_handler(config.ENV_FILE)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
