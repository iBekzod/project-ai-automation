"""Tkinter desktop UI for the bot: Start / Pause / Stop / Settings / Logs."""
from __future__ import annotations

import logging
import logging.handlers
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import config
import env_editor
from bot_controller import BotController

APP_TITLE = "Xonsaroy Support Bot"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 3


def _install_file_handler(env_file: Path) -> None:
    """Write rotating logs to bot.log next to the .env file.

    The file persists across bot Start/Stop cycles and lets future
    debugging sessions inspect past activity without copy-pasting.
    """
    log_path = env_file.parent / "bot.log"
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    # Avoid attaching twice if this function runs more than once.
    for existing in root.handlers:
        if isinstance(existing, logging.handlers.RotatingFileHandler) and \
                Path(existing.baseFilename) == log_path:
            return
    root.addHandler(handler)


# ---------- logging bridge ----------

class _QueueLogHandler(logging.Handler):
    """Ships formatted log records into a thread-safe queue for the UI to consume."""

    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


def _install_log_bridge() -> "queue.Queue[str]":
    q: queue.Queue[str] = queue.Queue(maxsize=2000)
    handler = _QueueLogHandler(q)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root = logging.getLogger()
    # Don't duplicate if main() also installs basicConfig; just add our handler.
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return q


# ---------- app ----------

class App(tk.Tk):
    POLL_MS = 200

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x560")
        self.minsize(700, 480)

        self.log_queue = _install_log_bridge()
        self.controller = BotController(on_state_change=self._on_state_change)

        self._build_ui()
        self._pump_logs()
        self._refresh_buttons()
        self._first_run_nudge()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- UI construction -----

    def _build_ui(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.tab_controls = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.tab_logs     = ttk.Frame(self.nb)
        self.nb.add(self.tab_controls, text="Controls")
        self.nb.add(self.tab_settings, text="Settings")
        self.nb.add(self.tab_logs,     text="Logs")

        self._build_controls()
        self._build_settings()
        self._build_logs()

        self.status_var = tk.StringVar(value=f"Config file: {config.ENV_FILE}")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=8, pady=(0, 4)
        )

    def _build_controls(self):
        f = self.tab_controls

        self.state_var = tk.StringVar(value="State: stopped")
        ttk.Label(f, textvariable=self.state_var, font=("Segoe UI", 16, "bold")).pack(pady=(24, 8))

        btn_frame = ttk.Frame(f)
        btn_frame.pack(pady=8)
        self.btn_start  = ttk.Button(btn_frame, text="Start",  width=12, command=self._on_start)
        self.btn_pause  = ttk.Button(btn_frame, text="Pause",  width=12, command=self._on_pause)
        self.btn_resume = ttk.Button(btn_frame, text="Resume", width=12, command=self._on_resume)
        self.btn_stop   = ttk.Button(btn_frame, text="Stop",   width=12, command=self._on_stop)
        for i, b in enumerate([self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop]):
            b.grid(row=0, column=i, padx=6, pady=6)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=20, pady=10)

        ttk.Label(f, text="Current configuration", font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20)
        self.summary_txt = tk.Text(f, height=10, wrap="word", state="disabled")
        self.summary_txt.pack(fill="both", expand=True, padx=20, pady=(4, 20))
        self._refresh_summary()

    def _build_settings(self):
        outer = self.tab_settings

        # Scrollable area in case the form grows
        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        form = ttk.Frame(canvas)
        form_id = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(form_id, width=e.width))

        values = env_editor.read_env(config.ENV_FILE)
        self._entries: dict[str, tk.Variable] = {}

        for i, field in enumerate(env_editor.FIELDS):
            label = f"{field.label}{' *' if field.required else ''}"
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", padx=12, pady=4)

            current = values.get(field.key, "")
            if field.kind == "bool":
                var = tk.StringVar(value="true" if current.strip().lower() in ("1", "true", "yes", "on") else "false")
                cb = ttk.Combobox(form, values=["true", "false"], textvariable=var, state="readonly", width=10)
                cb.grid(row=i, column=1, sticky="w", padx=12, pady=4)
                self._entries[field.key] = var
            elif field.kind == "path":
                var = tk.StringVar(value=current)
                row = ttk.Frame(form)
                row.grid(row=i, column=1, sticky="we", padx=12, pady=4)
                ttk.Entry(row, textvariable=var, width=56).pack(side="left", fill="x", expand=True)
                ttk.Button(row, text="Browse…", command=lambda v=var: self._browse_dir(v)).pack(side="left", padx=(6, 0))
                self._entries[field.key] = var
            else:
                var = tk.StringVar(value=current)
                entry = ttk.Entry(form, textvariable=var, width=60, show="*" if field.secret else "")
                entry.grid(row=i, column=1, sticky="we", padx=12, pady=4)
                self._entries[field.key] = var

            if field.hint:
                ttk.Label(form, text=field.hint, foreground="#666").grid(
                    row=i, column=2, sticky="w", padx=8, pady=4
                )

        form.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(form)
        btn_row.grid(row=len(env_editor.FIELDS), column=0, columnspan=3, pady=16, sticky="w", padx=12)
        ttk.Button(btn_row, text="Save",   command=self._on_save_settings).pack(side="left", padx=6)
        ttk.Button(btn_row, text="Reload", command=self._on_reload_settings).pack(side="left", padx=6)
        ttk.Label(form, text="* required", foreground="#888").grid(
            row=len(env_editor.FIELDS) + 1, column=0, columnspan=3, sticky="w", padx=12
        )

    def _build_logs(self):
        f = self.tab_logs
        header = ttk.Frame(f)
        header.pack(fill="x", padx=6, pady=4)
        ttk.Button(header, text="Clear", command=self._clear_logs).pack(side="right")

        self.log_txt = tk.Text(f, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_txt.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # ----- actions -----

    def _browse_dir(self, var: tk.Variable):
        current = var.get() or ""
        chosen = filedialog.askdirectory(initialdir=current if current else None, title="Select repo folder")
        if chosen:
            var.set(chosen)

    def _on_start(self):
        values = env_editor.read_env(config.ENV_FILE)
        missing = env_editor.missing_required(values)
        if missing:
            messagebox.showerror(APP_TITLE, f"Missing required settings: {', '.join(missing)}")
            self.nb.select(self.tab_settings)
            return
        ok, err = self.controller.start()
        if not ok:
            messagebox.showwarning(APP_TITLE, err or "could not start")
        self._refresh_summary()

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
        self._refresh_summary()
        changed_while_running = self.controller.state in ("running", "paused")
        msg = "Saved."
        if changed_while_running:
            msg += " Stop and start the bot for changes to fully apply."
        messagebox.showinfo(APP_TITLE, msg)

    def _on_reload_settings(self):
        values = env_editor.read_env(config.ENV_FILE)
        for key, var in self._entries.items():
            v = values.get(key, "")
            if isinstance(var, tk.StringVar):
                var.set(v)
        config.reload()
        self._refresh_summary()

    def _clear_logs(self):
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    # ----- state + pumps -----

    def _on_state_change(self, new: str, detail: str | None):
        # Marshal to Tk thread.
        self.after(0, lambda: self._apply_state(new, detail))

    def _apply_state(self, new: str, detail: str | None):
        label = f"State: {new}"
        if detail:
            label += f"  ({detail})"
        self.state_var.set(label)
        self._refresh_buttons()
        self._refresh_summary()

    def _refresh_buttons(self):
        s = self.controller.state
        self.btn_start.configure(state="normal" if s == "stopped" else "disabled")
        self.btn_pause.configure(state="normal" if s == "running" else "disabled")
        self.btn_resume.configure(state="normal" if s == "paused" else "disabled")
        self.btn_stop.configure(state="normal" if s in ("running", "paused", "starting") else "disabled")

    def _refresh_summary(self):
        self.summary_txt.configure(state="normal")
        self.summary_txt.delete("1.0", "end")
        self.summary_txt.insert("end", config.summarize())
        self.summary_txt.configure(state="disabled")

    def _pump_logs(self):
        drained = 0
        while drained < 200:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_txt.configure(state="normal")
            self.log_txt.insert("end", line + "\n")
            # Keep buffer bounded
            if int(self.log_txt.index("end-1c").split(".")[0]) > 2000:
                self.log_txt.delete("1.0", "500.0")
            self.log_txt.see("end")
            self.log_txt.configure(state="disabled")
            drained += 1
        self.after(self.POLL_MS, self._pump_logs)

    def _first_run_nudge(self):
        values = env_editor.read_env(config.ENV_FILE)
        if env_editor.missing_required(values):
            self.nb.select(self.tab_settings)

    def _on_close(self):
        if self.controller.state != "stopped":
            if not messagebox.askyesno(APP_TITLE, "Bot is running. Stop and quit?"):
                return
            self.controller.stop()
        self.destroy()


def main():
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    env_editor.ensure_env_file(config.ENV_FILE)
    _install_file_handler(config.ENV_FILE)
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
