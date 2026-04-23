"""Reads/writes the bot's .env file, preserving comments and ordering.

Exposes the canonical list of fields surfaced in the Settings tab.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    hint: str = ""
    secret: bool = False
    required: bool = False
    kind: str = "text"  # text | bool | path


FIELDS: list[Field] = [
    Field("TELEGRAM_BOT_TOKEN",    "Telegram bot token",   "From @BotFather",                         secret=True,  required=True),
    Field("TELEGRAM_DEVELOPER_ID", "Your Telegram user ID", "Numeric ID (ask @userinfobot)",           required=True),
    Field("MONITORED_GROUP_IDS",   "Monitored group IDs",   "Comma-separated, e.g. -1001234,-100567"),
    Field("GITHUB_TOKEN",          "GitHub token",          "PAT with repo scope",                     secret=True),
    Field("GITHUB_REPO",           "GitHub repo",           "owner/repo, e.g. iBekzod/xonsaroy"),
    Field("REPO_PATH",             "Local repo path",       "Folder Claude CLI runs in",               required=True, kind="path"),
    Field("STAGE_BRANCH",          "Stage branch",          "Default: stage"),
    Field("PROD_BRANCH",           "Prod branch",           "Default: dev"),
    Field("TRIGGER_KEYWORDS",      "Trigger keywords",      "Comma-separated. Empty = all messages."),
    Field("DRY_RUN",               "Dry run",               "true = skip push / PR / merge",           kind="bool"),
    Field("CLAUDE_CLI",            "Claude CLI command",    "Usually just: claude"),
    Field("CLAUDE_TIMEOUT",        "Claude timeout (s)",    "Default: 300"),
]


_DEFAULT_TEMPLATE = """# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_DEVELOPER_ID=
MONITORED_GROUP_IDS=

# GitHub
GITHUB_TOKEN=
GITHUB_REPO=

# Target repo (Claude CLI runs here)
REPO_PATH=

# Branches
STAGE_BRANCH=stage
PROD_BRANCH=dev

# Behaviour
TRIGGER_KEYWORDS=
DRY_RUN=true
CLAUDE_CLI=claude
CLAUDE_TIMEOUT=300
"""


def ensure_env_file(env_path: Path) -> None:
    """Create a blank, commented .env if one doesn't exist yet."""
    if env_path.exists():
        return
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(_DEFAULT_TEMPLATE, encoding="utf-8")


def read_env(env_path: Path) -> dict[str, str]:
    ensure_env_file(env_path)
    raw = dotenv_values(str(env_path))
    return {k: (v or "") for k, v in raw.items()}


def save_env(env_path: Path, values: dict[str, str]) -> None:
    """Write each value back via set_key so comments and layout survive."""
    ensure_env_file(env_path)
    for field in FIELDS:
        v = values.get(field.key, "")
        # set_key with empty value still writes KEY=''; acceptable since load_dotenv ignores blanks.
        set_key(str(env_path), field.key, v, quote_mode="never")


def missing_required(values: dict[str, str]) -> list[str]:
    return [f.key for f in FIELDS if f.required and not values.get(f.key, "").strip()]
