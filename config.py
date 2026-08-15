"""Env-driven configuration for the bot."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _resolve_base_dir() -> Path:
    """Directory that holds the .env file.

    * Frozen (PyInstaller .exe): next to the executable.
    * Script run: next to this file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR: Path = _resolve_base_dir()
ENV_FILE: Path = BASE_DIR / ".env"

load_dotenv(ENV_FILE, override=True)


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list_int(name: str) -> list[int]:
    raw = os.getenv(name, "")
    return [int(x) for x in (p.strip() for p in raw.split(",")) if x]


def _list_str(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [x.lower() for x in (p.strip() for p in raw.split(",")) if x]


TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _developer_ids() -> list[int]:
    """Read the dev allow-list from either the new plural env var or the
    legacy singular one (kept for backward compat with existing .env files).
    """
    plural = _list_int("TELEGRAM_DEVELOPER_IDS")
    if plural:
        return plural
    return _list_int("TELEGRAM_DEVELOPER_ID")


TELEGRAM_DEVELOPER_IDS: list[int] = _developer_ids()
# Legacy alias kept so older code paths and the GUI still resolve a primary
# id (e.g. for fallback DM target). Prefer is_developer()/iterating
# TELEGRAM_DEVELOPER_IDS for new code.
TELEGRAM_DEVELOPER_ID: int = TELEGRAM_DEVELOPER_IDS[0] if TELEGRAM_DEVELOPER_IDS else 0


def is_developer(user_id: int | None) -> bool:
    """True when `user_id` is in the dev allow-list.

    Checks both the DB `developers` table (source of truth at runtime) and
    the cached TELEGRAM_DEVELOPER_IDS list (covers the very-first-call
    window before db.init() has run).
    """
    if user_id is None:
        return False
    if user_id in TELEGRAM_DEVELOPER_IDS:
        return True
    try:
        import db as _db
        if _db._db_path is not None:
            return _db.is_developer_in_db(user_id)
    except Exception:
        pass
    return False


MONITORED_GROUP_IDS: list[int] = _list_int("MONITORED_GROUP_IDS")

GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "")

REPO_PATH: Path = Path(os.getenv("REPO_PATH", "")).resolve()

STAGE_BRANCH: str = os.getenv("STAGE_BRANCH", "stage")
PROD_BRANCH: str = os.getenv("PROD_BRANCH", "dev")

TRIGGER_KEYWORDS: list[str] = _list_str("TRIGGER_KEYWORDS")
DRY_RUN: bool = _bool("DRY_RUN", True)

CLAUDE_CLI: str = os.getenv("CLAUDE_CLI", "claude")
# Default 900s (15 min). Each analysis does two Claude CLI calls; Pass 1
# (investigation) reads CLAUDE.md, .ruflo/agents/*.yaml, and searches the repo,
# which on a 107-module Laravel app regularly takes several minutes. 300s was
# too tight and caused empty investigations.
CLAUDE_TIMEOUT: int = _int("CLAUDE_TIMEOUT", 900)

# Extra directories Claude may read and write, beyond REPO_PATH.
#
# WHY THIS EXISTS
# Claude is started with cwd=REPO_PATH and refuses anything outside it.
# The agent memory lives in a SEPARATE repo next door, so every attempt to
# read it was denied by the permission prompt and the bot reported it could
# not confirm the folder even existed — exactly what happened when it was
# asked to use the Obsidian-linked notes.
#
# Comma-separated absolute paths. Empty means "repo only", the old
# behaviour.
#
# This widens what an autonomous agent can touch, so it is a LIST and not a
# switch: naming the folders keeps the blast radius to what the work needs.
# The gate that makes it acceptable is TELEGRAM_DEVELOPER_IDS — only those
# accounts can command the bot; everyone else gets a redirect.
# Personal-agent mode: run Claude with no permission prompts.
#
# WHY IT IS NEEDED
# Claude runs here non-interactively (`--print`). There is no terminal
# to answer a permission prompt, so anything needing approval simply
# fails — which is why the assistant could describe work but never do
# it. Bypassing prompts is what makes it able to act at all.
#
# WHY IT IS ACCEPTABLE HERE, AND ONLY HERE
# This mode is reachable only from a DM by an id in
# TELEGRAM_DEVELOPER_IDS — everyone else gets a redirect and never
# reaches Claude. It is the owner's own machine, driven by the owner.
# The group triage path does NOT use it.
#
# What it does NOT do: DRY_RUN still blocks `git push`, so nothing
# reaches a server without a person saying so.
AGENT_FULL_ACCESS: bool = _bool("AGENT_FULL_ACCESS", True)

# Model for the routing classifiers (group triage stage 1, DM intent).
# Empty leaves the CLI default. See _classifier_model_args() for the numbers
# that motivated this.
CLASSIFIER_MODEL: str = os.getenv("CLASSIFIER_MODEL", "haiku")

CLAUDE_ADD_DIRS: list = [
    p.strip() for p in os.getenv("CLAUDE_ADD_DIRS", r"D:\projects\xonsaroy\xonsaroy-agent-memory").split(",")
    if p.strip()
]


# Mutable settings keys that live in the SQLite `settings` table when
# present. .env values seed these on first run; runtime edits via the GUI
# Settings tab or `/status` toggle write back to the table.
_DB_BACKED_KEYS = {
    "TRIGGER_KEYWORDS",
    "DRY_RUN",
    "CLAUDE_CLI",
    "CLAUDE_TIMEOUT",
    "CLAUDE_ADD_DIRS",
    "AGENT_FULL_ACCESS",
    "CLASSIFIER_MODEL",
    "MAX_PARALLEL_CLAUDE",
    "STAGE_BRANCH",
    "PROD_BRANCH",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "REPO_PATH",
}


def _settings_from_db() -> dict[str, str]:
    """Return all DB-backed settings, or an empty dict if DB isn't ready yet
    (e.g. during the very first build_app() call before db.init())."""
    try:
        import db as _db  # local import — avoids circular at startup
        if _db._db_path is None:  # init() hasn't run yet
            return {}
        out: dict[str, str] = {}
        for key in _DB_BACKED_KEYS:
            v = _db.get_setting(key)
            if v is not None:
                out[key] = v
        return out
    except Exception:
        return {}


def _resolved(name: str, env_default: str = "") -> str:
    """DB value if present (from settings table), else .env, else default."""
    db_overrides = _settings_from_db()
    if name in db_overrides:
        return db_overrides[name]
    return os.getenv(name, env_default)


def reload() -> None:
    """Re-read .env + DB-backed settings; refresh module-level attributes."""
    load_dotenv(ENV_FILE, override=True)
    g = globals()
    overrides = _settings_from_db()

    def pick(name: str, env_default: str = "") -> str:
        return overrides.get(name, os.getenv(name, env_default))

    g["TELEGRAM_BOT_TOKEN"]     = os.getenv("TELEGRAM_BOT_TOKEN", "")
    g["TELEGRAM_DEVELOPER_IDS"] = _developer_ids()
    g["TELEGRAM_DEVELOPER_ID"]  = g["TELEGRAM_DEVELOPER_IDS"][0] if g["TELEGRAM_DEVELOPER_IDS"] else 0
    g["MONITORED_GROUP_IDS"]    = _list_int("MONITORED_GROUP_IDS")
    g["GITHUB_TOKEN"]           = pick("GITHUB_TOKEN")
    g["GITHUB_REPO"]            = pick("GITHUB_REPO")
    g["REPO_PATH"]              = Path(pick("REPO_PATH")).resolve() if pick("REPO_PATH") else Path()
    g["STAGE_BRANCH"]           = pick("STAGE_BRANCH", "stage")
    g["PROD_BRANCH"]            = pick("PROD_BRANCH", "dev")
    raw_trig = pick("TRIGGER_KEYWORDS", "")
    g["TRIGGER_KEYWORDS"]       = [
        x.lower() for x in (p.strip() for p in raw_trig.split(",")) if x
    ]
    raw_dry = pick("DRY_RUN", "true").strip().lower()
    g["DRY_RUN"]                = raw_dry in ("1", "true", "yes", "on")
    g["CLAUDE_CLI"]             = pick("CLAUDE_CLI", "claude")
    raw_to = pick("CLAUDE_TIMEOUT", "900").strip()
    g["CLAUDE_TIMEOUT"]         = int(raw_to) if raw_to.isdigit() else 900
    raw_dirs = pick("CLAUDE_ADD_DIRS", r"D:\projects\xonsaroy\xonsaroy-agent-memory")
    g["CLAUDE_ADD_DIRS"]        = [x.strip() for x in raw_dirs.split(",") if x.strip()]
    raw_full = pick("AGENT_FULL_ACCESS", "true").strip().lower()
    g["AGENT_FULL_ACCESS"]      = raw_full in ("1", "true", "yes", "on")
    g["CLASSIFIER_MODEL"]       = pick("CLASSIFIER_MODEL", "haiku")


def summarize() -> str:
    return (
        f"REPO_PATH={REPO_PATH}\n"
        f"GITHUB_REPO={GITHUB_REPO}\n"
        f"STAGE_BRANCH={STAGE_BRANCH}  PROD_BRANCH={PROD_BRANCH}\n"
        f"DEVELOPER_IDS={TELEGRAM_DEVELOPER_IDS}\n"
        f"MONITORED_GROUP_IDS={MONITORED_GROUP_IDS}\n"
        f"TRIGGER_KEYWORDS={TRIGGER_KEYWORDS or '(all messages)'}\n"
        f"DRY_RUN={DRY_RUN}  CLAUDE_CLI={CLAUDE_CLI}  CLAUDE_TIMEOUT={CLAUDE_TIMEOUT}\n"
        f"CLAUDE_ADD_DIRS={CLAUDE_ADD_DIRS or '(repo only)'}"
    )
