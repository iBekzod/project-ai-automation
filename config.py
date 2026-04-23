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
TELEGRAM_DEVELOPER_ID: int = _int("TELEGRAM_DEVELOPER_ID")
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


def reload() -> None:
    """Re-read the .env file and refresh module-level settings in place."""
    load_dotenv(ENV_FILE, override=True)
    g = globals()
    g["TELEGRAM_BOT_TOKEN"]     = os.getenv("TELEGRAM_BOT_TOKEN", "")
    g["TELEGRAM_DEVELOPER_ID"]  = _int("TELEGRAM_DEVELOPER_ID")
    g["MONITORED_GROUP_IDS"]    = _list_int("MONITORED_GROUP_IDS")
    g["GITHUB_TOKEN"]           = os.getenv("GITHUB_TOKEN", "")
    g["GITHUB_REPO"]            = os.getenv("GITHUB_REPO", "")
    g["REPO_PATH"]              = Path(os.getenv("REPO_PATH", "")).resolve()
    g["STAGE_BRANCH"]           = os.getenv("STAGE_BRANCH", "stage")
    g["PROD_BRANCH"]            = os.getenv("PROD_BRANCH", "dev")
    g["TRIGGER_KEYWORDS"]       = _list_str("TRIGGER_KEYWORDS")
    g["DRY_RUN"]                = _bool("DRY_RUN", True)
    g["CLAUDE_CLI"]             = os.getenv("CLAUDE_CLI", "claude")
    g["CLAUDE_TIMEOUT"]         = _int("CLAUDE_TIMEOUT", 900)


def summarize() -> str:
    return (
        f"REPO_PATH={REPO_PATH}\n"
        f"GITHUB_REPO={GITHUB_REPO}\n"
        f"STAGE_BRANCH={STAGE_BRANCH}  PROD_BRANCH={PROD_BRANCH}\n"
        f"MONITORED_GROUP_IDS={MONITORED_GROUP_IDS}\n"
        f"TRIGGER_KEYWORDS={TRIGGER_KEYWORDS or '(all messages)'}\n"
        f"DRY_RUN={DRY_RUN}  CLAUDE_CLI={CLAUDE_CLI}  CLAUDE_TIMEOUT={CLAUDE_TIMEOUT}"
    )
