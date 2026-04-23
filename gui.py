"""Modern desktop UI for the bot.

Visual:
- Sun Valley `sv_ttk` theme (Win11-style ttk look-and-feel; dark by default).
- Dashboard tab with status cards instead of a flat label.
- Settings tab grouped into sections (Telegram / GitHub / Repo / Behaviour).
- Logs tab with INFO/WARNING/ERROR colour coding.
- Always-visible status bar at the bottom.

Background:
- Closing the window minimises to a system-tray icon (pystray) instead of
  exiting; the bot keeps running.
- Right-click the tray for Show / Hide / Stop / Quit.
- "Quit" cleanly stops the bot first, then exits.

Old API preserved:
- `_install_file_handler` is still importable from main.py.
- `main()` still launches the GUI; `python gui.py` works unchanged.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sv_ttk
from PIL import Image, ImageDraw
import pystray

import config
import env_editor
from bot_controller import BotController

APP_TITLE = "Xonsaroy AI PM Bot"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3

# Section -> list of env field keys (matches env_editor.FIELDS).
SETTINGS_SECTIONS: list[tuple[str, list[str]]] = [
    ("Telegram",   ["TELEGRAM_BOT_TOKEN", "TELEGRAM_DEVELOPER_IDS",
                    "MONITORED_GROUP_IDS"]),
    ("GitHub",     ["GITHUB_TOKEN", "GITHUB_REPO"]),
    ("Repository", ["REPO_PATH", "STAGE_BRANCH", "PROD_BRANCH"]),
    ("Behaviour",  ["TRIGGER_KEYWORDS", "DRY_RUN", "CLAUDE_CLI",
                    "CLAUDE_TIMEOUT"]),
]

LOG_TAG_COLORS = {
    "DEBUG":    "#888888",
    "INFO":     "#dcdcdc",
    "WARNING":  "#f5b800",
    "ERROR":    "#ff5c5c",
    "CRITICAL": "#ff0000",
}


def _install_file_handler(env_file: Path) -> None:
    """Write rotating logs to bot.log next to the .env file."""
    log_path = env_file.parent / "bot.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler) and \
                Path(existing.baseFilename) == log_path:
            return
    root.addHandler(handler)


# ---------- logging bridge ----------

class _QueueLogHandler(logging.Handler):
    """Ships log records (with level) into a thread-safe queue for the UI."""

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
    """Render a simple coloured circle icon. Green when running, grey otherwise."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (60, 200, 100, 255) if running else (140, 140, 140, 255)
    d.ellipse((6, 6, size - 6, size - 6), fill=fill, outline=(40, 40, 40, 255), width=2)
    # tiny "X" mark to suggest Xonsaroy
    d.line((22, 22, 42, 42), fill=(255, 255, 255, 230), width=4)
    d.line((42, 22, 22, 42), fill=(255, 255, 255, 230), width=4)
    return img


# ---------- app ----------

class App(tk.Tk):
    POLL_MS = 200
    LOG_BUFFER_LINES = 2000

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("960x640")
        self.minsize(820, 540)

        # Apply modern theme. Dark looks great on a Logs tab; if user wants
        # light, sv_ttk.set_theme("light") swaps in a Win11-light palette.
        sv_ttk.set_theme("dark")

        self.log_queue = _install_log_bridge()
        self.controller = BotController(on_state_change=self._on_state_change)

        # Tray-related state — set in _setup_tray, used throughout.
        self._tray_icon: pystray.Icon | None = None
        self._tray_thread: threading.Thread | None = None
        self._setup_tray()

        self._build_ui()
        self._pump_logs()
        self._refresh_buttons()
        self._refresh_dashboard()
        self._first_run_nudge()

        # Window-close → minimise to tray instead of exit.
        self.protocol("WM_DELETE_WINDOW", self._on_close_to_tray)

    # ===== UI construction =====

    def _build_ui(self):
        # Top notebook with three tabs.
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        self.tab_dashboard = ttk.Frame(self.nb)
        self.tab_settings  = ttk.Frame(self.nb)
        self.tab_logs      = ttk.Frame(self.nb)
        self.nb.add(self.tab_dashboard, text="  📊  Dashboard  ")
        self.nb.add(self.tab_settings,  text="  ⚙  Settings  ")
        self.nb.add(self.tab_logs,      text="  📜  Logs  ")

        self._build_dashboard()
        self._build_settings()
        self._build_logs()

        # Bottom status bar.
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=12, pady=8)
        self.status_var = tk.StringVar(
            value=f"⚪ stopped  ·  {config.ENV_FILE.name}",
        )
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left")
        ttk.Label(
            bar, text=f"v1.0  ·  py-telegram-bot 21.6",
            foreground="#888888", anchor="e",
        ).pack(side="right")

    # ----- Dashboard tab -----

    def _build_dashboard(self):
        f = self.tab_dashboard

        # Hero status card.
        hero = ttk.LabelFrame(f, text="Bot status", padding=18)
        hero.pack(fill="x", padx=10, pady=(12, 8))

        self.state_var = tk.StringVar(value="⚪  Stopped")
        ttk.Label(
            hero, textvariable=self.state_var,
            font=("Segoe UI Variable Display", 22, "bold"),
        ).pack(anchor="w")

        self.state_detail_var = tk.StringVar(value="")
        ttk.Label(
            hero, textvariable=self.state_detail_var,
            font=("Segoe UI", 10), foreground="#aaaaaa",
        ).pack(anchor="w", pady=(2, 12))

        btn_row = ttk.Frame(hero)
        btn_row.pack(anchor="w")
        self.btn_start = ttk.Button(
            btn_row, text="▶  Start",  width=14,
            command=self._on_start, style="Accent.TButton",
        )
        self.btn_pause  = ttk.Button(btn_row, text="⏸  Pause",  width=14, command=self._on_pause)
        self.btn_resume = ttk.Button(btn_row, text="⏵  Resume", width=14, command=self._on_resume)
        self.btn_stop   = ttk.Button(btn_row, text="⏹  Stop",   width=14, command=self._on_stop)
        for i, b in enumerate(
            [self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop]
        ):
            b.grid(row=0, column=i, padx=(0, 8), pady=4)

        # Three-card row: Mode / Repo / Groups.
        cards = ttk.Frame(f)
        cards.pack(fill="x", padx=10, pady=(8, 8))
        cards.columnconfigure(0, weight=1, uniform="card")
        cards.columnconfigure(1, weight=1, uniform="card")
        cards.columnconfigure(2, weight=1, uniform="card")

        self.card_mode_var   = tk.StringVar()
        self.card_repo_var   = tk.StringVar()
        self.card_groups_var = tk.StringVar()
        self._make_card(cards, 0, "Mode",            self.card_mode_var)
        self._make_card(cards, 1, "Target repo",     self.card_repo_var)
        self._make_card(cards, 2, "Monitored chats", self.card_groups_var)

        # Full configuration card (collapsible feel via LabelFrame).
        cfg = ttk.LabelFrame(f, text="Active configuration", padding=12)
        cfg.pack(fill="both", expand=True, padx=10, pady=(8, 12))
        self.summary_txt = tk.Text(
            cfg, height=8, wrap="word", state="disabled",
            font=("Cascadia Mono", 10), borderwidth=0, relief="flat",
        )
        self.summary_txt.pack(fill="both", expand=True)

    def _make_card(self, parent: ttk.Frame, col: int, title: str, var: tk.StringVar):
        card = ttk.LabelFrame(parent, text=title, padding=14)
        card.grid(row=0, column=col, sticky="nsew", padx=4)
        ttk.Label(
            card, textvariable=var,
            font=("Segoe UI", 13, "bold"), wraplength=260, justify="left",
        ).pack(anchor="w", fill="x")

    # ----- Settings tab -----

    def _build_settings(self):
        outer = self.tab_settings

        # Scrollable area in case the form grows.
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scroll.pack(side="right", fill="y")

        form = ttk.Frame(canvas)
        form_id = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(form_id, width=e.width))

        values = env_editor.read_env(config.ENV_FILE)
        self._entries: dict[str, tk.Variable] = {}
        fields_by_key = {f.key: f for f in env_editor.FIELDS}
        seen: set[str] = set()

        # Render in section order.
        for section_title, keys in SETTINGS_SECTIONS:
            section = ttk.LabelFrame(form, text=section_title, padding=14)
            section.pack(fill="x", padx=4, pady=(0, 12))
            section.columnconfigure(1, weight=1)

            for row_idx, key in enumerate(keys):
                fld = fields_by_key.get(key)
                if fld is None:
                    continue
                seen.add(key)
                self._render_field(section, row_idx, fld, values.get(key, ""))

        # Render any fields not in any section (forward-compat).
        leftovers = [f for f in env_editor.FIELDS if f.key not in seen]
        if leftovers:
            section = ttk.LabelFrame(form, text="Other", padding=14)
            section.pack(fill="x", padx=4, pady=(0, 12))
            section.columnconfigure(1, weight=1)
            for row_idx, fld in enumerate(leftovers):
                self._render_field(section, row_idx, fld, values.get(fld.key, ""))

        # Action row.
        btn_row = ttk.Frame(form)
        btn_row.pack(anchor="w", padx=4, pady=(4, 16))
        ttk.Button(
            btn_row, text="💾  Save", style="Accent.TButton",
            command=self._on_save_settings,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="↻  Reload", command=self._on_reload_settings).pack(side="left")
        ttk.Label(
            form, text="* required field — must be filled before Start",
            foreground="#888",
        ).pack(anchor="w", padx=4, pady=(0, 16))

    def _render_field(self, parent: ttk.LabelFrame, row: int,
                      fld: "env_editor.Field", current: str):
        label = f"{fld.label}{'  *' if fld.required else ''}"
        ttk.Label(parent, text=label).grid(
            row=row * 2, column=0, sticky="w", padx=(0, 12), pady=(8, 0),
        )

        if fld.kind == "bool":
            var = tk.StringVar(
                value="true" if current.strip().lower() in ("1", "true", "yes", "on") else "false",
            )
            cb = ttk.Combobox(
                parent, values=["true", "false"], textvariable=var,
                state="readonly", width=12,
            )
            cb.grid(row=row * 2, column=1, sticky="w", padx=0, pady=(8, 0))
            self._entries[fld.key] = var
        elif fld.kind == "path":
            var = tk.StringVar(value=current)
            wrap = ttk.Frame(parent)
            wrap.grid(row=row * 2, column=1, sticky="we", pady=(8, 0))
            wrap.columnconfigure(0, weight=1)
            ttk.Entry(wrap, textvariable=var).grid(row=0, column=0, sticky="we")
            ttk.Button(wrap, text="📁  Browse…", command=lambda v=var: self._browse_dir(v)).grid(
                row=0, column=1, padx=(8, 0),
            )
            self._entries[fld.key] = var
        else:
            var = tk.StringVar(value=current)
            entry = ttk.Entry(parent, textvariable=var, show="•" if fld.secret else "")
            entry.grid(row=row * 2, column=1, sticky="we", pady=(8, 0))
            self._entries[fld.key] = var

        if fld.hint:
            ttk.Label(
                parent, text=fld.hint, foreground="#888888",
                font=("Segoe UI", 9),
            ).grid(row=row * 2 + 1, column=1, sticky="w", padx=0, pady=(2, 0))

    # ----- Logs tab -----

    def _build_logs(self):
        f = self.tab_logs
        header = ttk.Frame(f)
        header.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(
            header, text="Live log stream",
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")
        ttk.Button(
            header, text="🗑  Clear", command=self._clear_logs,
        ).pack(side="right")

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        scroll = ttk.Scrollbar(wrap, orient="vertical")
        scroll.pack(side="right", fill="y")
        self.log_txt = tk.Text(
            wrap, wrap="word", state="disabled",
            font=("Cascadia Mono", 9),
            bg="#1c1c1c", fg="#dcdcdc",
            insertbackground="#dcdcdc",
            borderwidth=0, relief="flat",
            yscrollcommand=scroll.set,
        )
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
            self._tray_icon.title = (
                f"{APP_TITLE} · {self.controller.state}"
            )
        except Exception:  # noqa: BLE001
            pass  # tray icon may not be fully initialised yet

    def _tray_show(self, _icon=None, _item=None):
        self.after(0, self._show_window)

    def _tray_hide(self, _icon=None, _item=None):
        self.after(0, self.withdraw)

    def _tray_start(self, _icon=None, _item=None):
        self.after(0, self._on_start)

    def _tray_stop(self, _icon=None, _item=None):
        self.after(0, self._on_stop)

    def _tray_quit(self, _icon=None, _item=None):
        # Cleanly stop the bot, kill tray, then destroy the window.
        self.after(0, self._real_quit)

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

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
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.controller.stop()
        finally:
            self.config(cursor="")

    def _on_save_settings(self):
        values = {k: v.get().strip() for k, v in self._entries.items()}
        try:
            env_editor.save_env(config.ENV_FILE, values)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Save failed: {exc}")
            return
        config.reload()
        self._refresh_dashboard()
        changed_while_running = self.controller.state in ("running", "paused")
        msg = "Settings saved."
        if changed_while_running:
            msg += "\n\nStop and Start the bot for all changes to take effect."
        messagebox.showinfo(APP_TITLE, msg)

    def _on_reload_settings(self):
        values = env_editor.read_env(config.ENV_FILE)
        for key, var in self._entries.items():
            v = values.get(key, "")
            if isinstance(var, tk.StringVar):
                var.set(v)
        config.reload()
        self._refresh_dashboard()

    def _clear_logs(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    # ===== State + pumps =====

    def _on_state_change(self, new: str, detail: str | None):
        self.after(0, lambda: self._apply_state(new, detail))

    def _apply_state(self, new: str, detail: str | None):
        ICONS = {
            "stopped":  "⚪",
            "starting": "🟡",
            "running":  "🟢",
            "paused":   "🟠",
            "stopping": "🟡",
        }
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
        self.btn_stop.configure(
            state="normal" if s in ("running", "paused", "starting") else "disabled",
        )

    def _refresh_dashboard(self):
        # Cards.
        mode = "🧪  DRY_RUN  (safe)" if config.DRY_RUN else "🚀  LIVE  (writes prod)"
        self.card_mode_var.set(mode)

        repo_text = config.GITHUB_REPO or "(not set)"
        if config.STAGE_BRANCH or config.PROD_BRANCH:
            repo_text += f"\nstage={config.STAGE_BRANCH}  prod={config.PROD_BRANCH}"
        self.card_repo_var.set(repo_text)

        groups = config.MONITORED_GROUP_IDS or []
        if groups:
            self.card_groups_var.set(f"{len(groups)} group(s)\n" + ", ".join(str(g) for g in groups))
        else:
            self.card_groups_var.set("(none configured)")

        # Full config text.
        self.summary_txt.configure(state="normal")
        self.summary_txt.delete("1.0", "end")
        self.summary_txt.insert("end", config.summarize())
        self.summary_txt.configure(state="disabled")

    def _pump_logs(self):
        drained = 0
        while drained < 200:
            try:
                level, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_txt.configure(state="normal")
            tag = level if level in LOG_TAG_COLORS else "INFO"
            self.log_txt.insert("end", line + "\n", tag)
            # Bounded buffer.
            line_count = int(self.log_txt.index("end-1c").split(".")[0])
            if line_count > self.LOG_BUFFER_LINES:
                self.log_txt.delete("1.0", "500.0")
            self.log_txt.see("end")
            self.log_txt.configure(state="disabled")
            drained += 1
        self.after(self.POLL_MS, self._pump_logs)

    def _first_run_nudge(self):
        values = env_editor.read_env(config.ENV_FILE)
        if env_editor.missing_required(values):
            self.nb.select(self.tab_settings)

    # ===== Closing =====

    def _on_close_to_tray(self):
        """Window-close → hide to tray. Bot keeps running.

        Show a one-time tray notification so the user knows the bot didn't
        actually shut down — Windows users often expect 'X' to fully exit.
        """
        self.withdraw()
        if self._tray_icon and not getattr(self, "_tray_notified_once", False):
            try:
                self._tray_icon.notify(
                    "Oyna yashirildi, lekin bot ishlashda davom etmoqda. "
                    "Boshqarish uchun tray belgisini bosing.",
                    APP_TITLE,
                )
                self._tray_notified_once = True
            except Exception:  # noqa: BLE001
                # notify() isn't supported on every platform — silent fallback.
                pass

    def _real_quit(self):
        """Tray → Quit. Stops the bot, removes tray, destroys the window."""
        if self.controller.state != "stopped":
            try:
                self.controller.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.destroy()
        finally:
            # Ensure pystray's thread doesn't hold the process open.
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
