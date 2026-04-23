"""SQLite-backed persistence layer for the bot.

Replaces a handful of in-memory dicts and one JSON file (chats.json) so
state survives restarts:

  - ISSUES dict           → `issues` table
  - _PENDING_TASKS dict   → `pending_tasks` table
  - ONGOING_ACKS dict     → `ongoing_acks` table  (the asyncio.Task itself
                            is in-memory only; restart loses cancellation
                            ability but supersede-via-edit_message_text
                            still works because we keep ack_message_id)
  - chats.json            → `chats` table (+ optional `chat_turns` later)

Also pre-creates `actions` (audit log) and `settings` (mutable bot config)
tables for upcoming phases.

### Concurrency

Every public function does the minimum work synchronously inside a fresh
short-lived connection. SQLite WAL mode lets readers and writers
coexist; one bot process at our scale means we never need the more
elaborate single-writer-thread pattern.

### File location

`bot.db` lives next to `.env`. Gitignored. Survives bot restarts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import config

log = logging.getLogger(__name__)

# Resolved on first init() call so we honour any post-startup `config.reload()`
# that might point at a different .env directory.
_db_path: Path | None = None
_init_lock = threading.Lock()
_initialized = False


# Schema. Each statement is idempotent so init() can be called repeatedly.
# Add to the bottom — never reorder, never drop columns without writing a
# migration first (we don't have one yet; deal with it when needed).
_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by  INTEGER
);

CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor_id     INTEGER,
    action       TEXT NOT NULL,
    project      TEXT,
    issue_id     TEXT,
    payload      TEXT,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_issue ON actions(issue_id);
CREATE INDEX IF NOT EXISTS idx_actions_ts    ON actions(ts);

CREATE TABLE IF NOT EXISTS issues (
    id                 TEXT PRIMARY KEY,
    project            TEXT,
    group_id           INTEGER,
    group_title        TEXT,
    user_message_id    INTEGER,
    message            TEXT,
    diagnosis_json     TEXT,
    category           TEXT,
    branch             TEXT,
    pr_url             TEXT,
    merged_to_stage    INTEGER DEFAULT 0,
    awaiting_retry     INTEGER DEFAULT 0,
    retry_initiator    INTEGER,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at          TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_issues_open ON issues(closed_at);

CREATE TABLE IF NOT EXISTS chats (
    user_id        INTEGER NOT NULL,
    name           TEXT NOT NULL,
    session_id     TEXT,
    turn_count     INTEGER DEFAULT 0,
    last_activity  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active      INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_chats_active ON chats(user_id, is_active);

CREATE TABLE IF NOT EXISTS pending_tasks (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER,
    text        TEXT,
    image_path  TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ongoing_acks (
    ack_message_id   INTEGER PRIMARY KEY,
    chat_id          INTEGER NOT NULL,
    original_msg_id  INTEGER,
    started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_acks_orig ON ongoing_acks(original_msg_id);
"""


def init() -> None:
    """Open / create the DB and apply schema. Safe to call multiple times."""
    global _db_path, _initialized
    with _init_lock:
        if _initialized:
            return
        _db_path = config.ENV_FILE.parent / "bot.db"
        with _connect() as conn:
            conn.executescript(_SCHEMA_SQL)
        _initialized = True
        log.info("sqlite ready at %s", _db_path)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Per-call connection. Auto-commits each statement (isolation_level=None)."""
    if _db_path is None:
        raise RuntimeError("db.init() must be called before any DB op")
    conn = sqlite3.connect(str(_db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ============================================================================
# settings — mutable runtime config (Phase 6 will gradually move .env keys here)
# ============================================================================

def get_setting(key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, updated_by: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO settings(key, value, updated_by) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   updated_at=CURRENT_TIMESTAMP,
                   updated_by=excluded.updated_by""",
            (key, value, updated_by),
        )


# ============================================================================
# actions — structured audit log (queryable, complements bot.log)
# ============================================================================

def record_action(
    action: str,
    actor_id: int | None = None,
    project: str | None = None,
    issue_id: str | None = None,
    payload: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one row to the audit log. Errors swallowed — never let logging
    break the actual flow."""
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO actions(actor_id, action, project, issue_id, payload, duration_ms)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    actor_id,
                    action,
                    project,
                    issue_id,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    duration_ms,
                ),
            )
    except Exception:  # noqa: BLE001
        log.exception("record_action failed (action=%s)", action)


def recent_actions(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY ts DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# issues — replaces the in-memory ISSUES dict (issues survive restart now)
# ============================================================================

def save_issue(issue: dict) -> None:
    """Insert or upsert an issue. Pass a dict mirroring the Issue dataclass."""
    diagnosis = issue.get("diagnosis") or {}
    with _connect() as conn:
        conn.execute(
            """INSERT INTO issues(
                id, project, group_id, group_title, user_message_id,
                message, diagnosis_json, category, branch, pr_url,
                merged_to_stage, awaiting_retry, retry_initiator
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project=excluded.project,
                group_title=excluded.group_title,
                message=excluded.message,
                diagnosis_json=excluded.diagnosis_json,
                category=excluded.category,
                branch=excluded.branch,
                pr_url=excluded.pr_url,
                merged_to_stage=excluded.merged_to_stage,
                awaiting_retry=excluded.awaiting_retry,
                retry_initiator=excluded.retry_initiator
            """,
            (
                issue["id"],
                issue.get("project"),
                issue.get("group_id"),
                issue.get("group_title"),
                issue.get("user_message_id"),
                issue.get("message"),
                json.dumps(diagnosis, ensure_ascii=False) if diagnosis else None,
                diagnosis.get("category"),
                issue.get("branch"),
                issue.get("pr_url"),
                int(bool(issue.get("merged_to_stage"))),
                int(bool(issue.get("awaiting_retry_prompt"))),
                issue.get("retry_initiator"),
            ),
        )


def load_open_issues() -> list[dict]:
    """Return open issues (closed_at IS NULL), newest first."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM issues WHERE closed_at IS NULL
               ORDER BY datetime(created_at) DESC""",
        ).fetchall()
    return [_row_to_issue(r) for r in rows]


def close_issue(issue_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE issues SET closed_at=CURRENT_TIMESTAMP WHERE id=?",
            (issue_id,),
        )


def _row_to_issue(row: sqlite3.Row) -> dict:
    """Hydrate a row back into the dict shape that mirrors the Issue dataclass."""
    return {
        "id":                    row["id"],
        "project":               row["project"],
        "group_id":              row["group_id"],
        "group_title":           row["group_title"],
        "user_message_id":       row["user_message_id"],
        "message":               row["message"],
        "diagnosis":             json.loads(row["diagnosis_json"]) if row["diagnosis_json"] else {},
        "branch":                row["branch"],
        "pr_url":                row["pr_url"],
        "merged_to_stage":       bool(row["merged_to_stage"]),
        "awaiting_retry_prompt": bool(row["awaiting_retry"]),
        "retry_initiator":       row["retry_initiator"],
        "created_at":            row["created_at"],
    }


# ============================================================================
# pending_tasks — per-token TASK confirmations (replaces _PENDING_TASKS dict)
# ============================================================================

def save_pending_task(
    token: str,
    user_id: int,
    text: str,
    image_path: str | None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pending_tasks(token, user_id, text, image_path)
               VALUES(?, ?, ?, ?)""",
            (token, user_id, text, image_path),
        )


def pop_pending_task(token: str) -> tuple[str, str | None] | None:
    """Return (text, image_path) or None; deletes on read."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT text, image_path FROM pending_tasks WHERE token=?", (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_tasks WHERE token=?", (token,))
    return (row["text"], row["image_path"])


# ============================================================================
# chats — per-dev multi-session state (replaces chats.json)
# ============================================================================

def upsert_chat(
    user_id: int,
    name: str,
    session_id: str | None,
    turn_count: int,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO chats(user_id, name, session_id, turn_count, last_activity, is_active)
               VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP, 0)
               ON CONFLICT(user_id, name) DO UPDATE SET
                   session_id=excluded.session_id,
                   turn_count=excluded.turn_count,
                   last_activity=CURRENT_TIMESTAMP""",
            (user_id, name, session_id, turn_count),
        )


def set_active_chat(user_id: int, name: str | None) -> None:
    """Mark `name` as the active chat for `user_id`, demoting all others.
    Pass None to clear active without picking a new one."""
    with _connect() as conn:
        conn.execute("UPDATE chats SET is_active=0 WHERE user_id=?", (user_id,))
        if name:
            conn.execute(
                "UPDATE chats SET is_active=1 WHERE user_id=? AND name=?",
                (user_id, name),
            )


def delete_chat(user_id: int, name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM chats WHERE user_id=? AND name=?", (user_id, name),
        )


def list_chats(user_id: int) -> list[dict]:
    """Return all chats for a user, most recent activity first."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT user_id, name, session_id, turn_count, last_activity, is_active
               FROM chats WHERE user_id=?
               ORDER BY datetime(last_activity) DESC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# ongoing_acks — in-flight "AI tahlil qilmoqda..." replies (so a restart
# doesn't lose the supersede-on-thread-resolution feature)
# ============================================================================

def save_ongoing_ack(ack_msg_id: int, chat_id: int, original_msg_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO ongoing_acks(ack_message_id, chat_id, original_msg_id)
               VALUES(?, ?, ?)""",
            (ack_msg_id, chat_id, original_msg_id),
        )


def remove_ongoing_ack(ack_msg_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM ongoing_acks WHERE ack_message_id=?", (ack_msg_id,),
        )


def find_ack_by_reply(reply_to_msg_id: int) -> dict | None:
    """Look up an ongoing ack by either its own message_id or its triggering
    complaint's message_id. Used to detect 'boldi togrlandi' thread updates."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT ack_message_id, chat_id, original_msg_id, started_at
               FROM ongoing_acks
               WHERE ack_message_id = ? OR original_msg_id = ?""",
            (reply_to_msg_id, reply_to_msg_id),
        ).fetchone()
    return dict(row) if row else None


# ============================================================================
# Migration helpers
# ============================================================================

def import_chats_json_if_needed(json_path: Path) -> int:
    """One-shot import of legacy chats.json into the chats table.

    Returns number of chats imported. Safe to call repeatedly — already-
    present rows get upserted (no duplicates) but existing turn counts
    will be overwritten on subsequent runs, so this is intended to fire
    exactly once during the migration window.
    """
    if not json_path.exists():
        return 0
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.exception("could not read %s for migration", json_path)
        return 0
    imported = 0
    for uid_str, ud in data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        active_name = ud.get("active")
        for name, sd in (ud.get("sessions") or {}).items():
            upsert_chat(
                uid, name,
                session_id=sd.get("session_id"),
                turn_count=int(sd.get("turn_count") or 0),
            )
            imported += 1
        if active_name:
            set_active_chat(uid, active_name)
    log.info("migrated %d chats from %s", imported, json_path)
    return imported
