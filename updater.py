"""Self-updater for the bot.

Checks the GitHub repo for newer commits on the configured branch and
either notifies the developers or auto-applies (`git pull` + restart).

Designed for colleagues who clone the repo and run `python gui.py`
locally — each install independently follows `origin/main` (or whichever
branch is configured) and stays current without manual intervention.

Storage: settings live in the SQLite `settings` table.

Defaults (all editable via `db.set_setting`):
- update_repo_api    = "https://api.github.com/repos/iBekzod/project-ai-automation"
- update_branch      = "main"
- update_check_hours = 6   (0 disables periodic checks)
- update_auto_apply  = "false"  ("true" → pull + restart automatically)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import config
import db

log = logging.getLogger(__name__)

DEFAULT_REPO_API = "https://api.github.com/repos/iBekzod/project-ai-automation"
DEFAULT_BRANCH = "main"
DEFAULT_CHECK_HOURS = 6
DEFAULT_AUTO_APPLY = False


# ---------- helpers ----------

def _bot_dir() -> Path:
    """Directory containing this file — assumed to be the git clone root."""
    return Path(__file__).resolve().parent


def is_git_clone() -> bool:
    return (_bot_dir() / ".git").exists()


def _git(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a `git` command in the bot directory."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(_bot_dir()),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


# ---------- version detection ----------

def current_commit() -> str | None:
    """Return the local HEAD commit SHA, or None if not a git clone."""
    if not is_git_clone():
        return None
    rc, out, _ = _git("rev-parse", "HEAD")
    return out if rc == 0 and out else None


def current_branch() -> str | None:
    if not is_git_clone():
        return None
    rc, out, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 and out else None


def latest_remote_commit(
    api_base: str | None = None,
    branch: str | None = None,
) -> dict | None:
    """Query the GitHub API for the most recent commit on `branch`.

    Returns dict with sha + first-line message + author + date, or None
    on network/API failure.
    """
    api_base = (api_base or db.get_setting("update_repo_api") or DEFAULT_REPO_API).rstrip("/")
    branch = branch or db.get_setting("update_branch") or DEFAULT_BRANCH
    url = f"{api_base}/commits/{branch}"

    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"},
    )
    # If a GitHub token is configured (private repo), authenticate.
    token = config.GITHUB_TOKEN
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch latest commit from %s: %s", url, exc)
        return None

    try:
        return {
            "sha": data["sha"],
            "message": data["commit"]["message"].split("\n")[0][:120],
            "author": data["commit"]["author"]["name"],
            "date": data["commit"]["author"]["date"],
            "html_url": data.get("html_url"),
        }
    except (KeyError, TypeError):
        log.warning("unexpected GitHub API response shape from %s", url)
        return None


def check_for_update() -> dict | None:
    """Return the remote commit dict iff it differs from local HEAD."""
    local = current_commit()
    if not local:
        return None
    remote = latest_remote_commit()
    if not remote:
        return None
    if remote["sha"] == local:
        return None
    return remote


# ---------- apply update ----------

def apply_update() -> tuple[bool, str]:
    """Stash local changes (safety) + `git pull origin <branch>`.

    Returns (ok, human-readable output). Files in `.gitignore` (`.env`,
    `bot.db`, `chats.json`, etc.) are untouched. Tracked file conflicts
    abort the pull and return False.
    """
    if not is_git_clone():
        return False, "Bot directory is not a git clone — auto-update unavailable."

    branch = db.get_setting("update_branch") or DEFAULT_BRANCH

    # Best-effort stash. If there's nothing to stash, returncode is non-zero
    # but harmless — we ignore it and proceed.
    _git("stash", "push", "-u", "-m", "auto-update-stash")

    rc, out, err = _git("fetch", "origin", branch, timeout=60)
    if rc != 0:
        return False, f"git fetch failed: {err or out}"

    rc, out, err = _git("pull", "--ff-only", "origin", branch, timeout=60)
    if rc != 0:
        return False, f"git pull failed: {err or out}"

    return True, out or "Already up-to-date."


# ---------- restart ----------

def restart_bot() -> None:
    """Replace the current process with a fresh one (same args).

    Spawns a detached child running the same Python + sys.argv, then
    exits the current process via `os._exit(0)`. Works for both the
    GUI (`python gui.py`) and CLI (`python main.py`) entry points.
    """
    log.warning("restarting bot via spawn + os._exit(0)")
    spawn_kwargs: dict = {
        "stdin":  subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    # Detach on Windows so the child outlives our termination.
    if hasattr(subprocess, "DETACHED_PROCESS"):
        spawn_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    try:
        subprocess.Popen([sys.executable] + sys.argv, **spawn_kwargs)
    except Exception:  # noqa: BLE001
        log.exception("failed to spawn replacement process; not exiting")
        return
    os._exit(0)


# ---------- settings glue ----------

def check_interval_seconds() -> int:
    """How often the periodic checker should fire (seconds). 0 disables."""
    raw = db.get_setting("update_check_hours")
    try:
        h = int(raw) if raw is not None else DEFAULT_CHECK_HOURS
    except ValueError:
        h = DEFAULT_CHECK_HOURS
    return max(0, h) * 3600


def auto_apply_enabled() -> bool:
    raw = (db.get_setting("update_auto_apply") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")
