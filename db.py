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

-- ============================================================================
-- Multi-project model — a "project" is an ecosystem (e.g. "Xonsaroy") that
-- contains one or more repos (backend, mobile, frontend, admin, ...). One
-- Telegram group may discuss multiple projects (M:N via group_project_links).
-- ============================================================================

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,                 -- short slug, e.g. "xonsaroy"
    name          TEXT NOT NULL,                    -- display name
    description   TEXT,                              -- scope hint for classifier
    github_token  TEXT,                              -- per-project PAT (NULL = use settings.github_token)
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL,                    -- "backend" / "mobile" / "frontend" / "admin" / ...
    label         TEXT,                              -- display label
    description   TEXT,                              -- per-repo scope hint
    repo_path     TEXT NOT NULL,                    -- local clone absolute path
    github_repo   TEXT NOT NULL,                    -- "owner/repo"
    stage_branch  TEXT NOT NULL DEFAULT 'stage',
    prod_branch   TEXT NOT NULL DEFAULT 'main',
    test_command  TEXT,
    is_active     INTEGER DEFAULT 1,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, role),                       -- one role per project
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_repos_project ON repos(project_id);

CREATE TABLE IF NOT EXISTS group_project_links (
    chat_id     INTEGER NOT NULL,
    project_id  TEXT NOT NULL,
    PRIMARY KEY (chat_id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gpl_proj ON group_project_links(project_id);

-- Developers — moves from TELEGRAM_DEVELOPER_IDS env var to a table you can
-- edit at runtime. .env value seeds this on first run.
CREATE TABLE IF NOT EXISTS developers (
    user_id   INTEGER PRIMARY KEY,
    label     TEXT,
    added_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    added_by  INTEGER
);

-- Per-call cost and token accounting for every Claude CLI invocation.
--
-- The CLI already returns this on every call (total_cost_usd + a usage block);
-- until now the code parsed out the answer text and threw the rest away. This
-- table is what makes the question "where is the budget going?" answerable at
-- all — and every later decision (priority, caps, which agent to run) has to
-- rest on it rather than on a guess.
--
-- Cache columns are kept separate on purpose: on this workload they dominate.
-- A trivial call measured 2 input + 4 output tokens but 9,549 cache-creation
-- tokens, because each invocation reloads CLAUDE.md and the project context.
-- Lumping them into one "tokens" number would hide the actual cost driver.
CREATE TABLE IF NOT EXISTS claude_usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,     -- classify | classify_dm | analyze | chat
    project_id     TEXT,
    repo_role      TEXT,
    issue_id       TEXT,
    cost_usd       REAL DEFAULT 0,
    input_tokens         INTEGER DEFAULT 0,
    output_tokens        INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0,
    duration_ms    INTEGER DEFAULT 0,
    num_turns      INTEGER DEFAULT 0,
    ok             INTEGER DEFAULT 1, -- 0 = call failed / timed out
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_claude_usage_created ON claude_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_claude_usage_kind    ON claude_usage(kind, created_at);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, col: str, decl: str,
) -> None:
    """SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Emulate it."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        log.info("schema: added %s.%s", table, col)


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only column additions for existing DBs."""
    _add_column_if_missing(conn, "issues", "project_id", "TEXT")
    _add_column_if_missing(conn, "issues", "repo_role",  "TEXT")
    # The group status message this report owns. Every pipeline stage edits
    # THIS message instead of posting a new one, so the id has to survive a
    # restart — otherwise a bot restart mid-pipeline orphans the message and
    # the reporter is left looking at "ko'rib chiqyapman" forever.
    _add_column_if_missing(conn, "issues", "ack_message_id", "INTEGER")


def init() -> None:
    """Open / create the DB and apply schema. Safe to call multiple times."""
    global _db_path, _initialized
    with _init_lock:
        if _initialized:
            return
        _db_path = config.ENV_FILE.parent / "bot.db"
        with _connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            _migrate(conn)
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
                id, project, project_id, repo_role, group_id, group_title,
                user_message_id, message, diagnosis_json, category, branch,
                pr_url, merged_to_stage, awaiting_retry, retry_initiator,
                ack_message_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                project=excluded.project,
                project_id=excluded.project_id,
                repo_role=excluded.repo_role,
                group_title=excluded.group_title,
                message=excluded.message,
                diagnosis_json=excluded.diagnosis_json,
                category=excluded.category,
                branch=excluded.branch,
                pr_url=excluded.pr_url,
                merged_to_stage=excluded.merged_to_stage,
                awaiting_retry=excluded.awaiting_retry,
                retry_initiator=excluded.retry_initiator,
                ack_message_id=excluded.ack_message_id
            """,
            (
                issue["id"],
                issue.get("project"),
                issue.get("project_id"),
                issue.get("repo_role"),
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
                issue.get("ack_message_id"),
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
    keys = row.keys()
    return {
        "id":                    row["id"],
        "project":               row["project"],
        "project_id":            row["project_id"] if "project_id" in keys else None,
        "repo_role":             row["repo_role"] if "repo_role" in keys else None,
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
        # Guarded like project_id/repo_role: a DB written before this column
        # existed has no such key, and row["..."] would raise on it.
        "ack_message_id":        row["ack_message_id"] if "ack_message_id" in keys else None,
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

# ============================================================================
# projects + repos — a project is an ecosystem (e.g. "Xonsaroy") containing
# multiple repos (backend / mobile / frontend / ...). Each project may be
# linked to one or more Telegram groups.
# ============================================================================

def list_projects() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,),
        ).fetchone()
    return dict(row) if row else None


def upsert_project(
    project_id: str,
    name: str,
    description: str | None = None,
    github_token: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO projects(id, name, description, github_token, updated_at)
               VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name,
                   description=excluded.description,
                   github_token=COALESCE(excluded.github_token, projects.github_token),
                   updated_at=CURRENT_TIMESTAMP""",
            (project_id, name, description, github_token),
        )


def delete_project(project_id: str) -> None:
    """Drops the project. Cascades to its repos and group links via FK."""
    with _connect() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def list_repos(project_id: str | None = None) -> list[dict]:
    with _connect() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM repos WHERE project_id=? AND is_active=1 ORDER BY role",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM repos WHERE is_active=1 ORDER BY project_id, role",
            ).fetchall()
    return [dict(r) for r in rows]


def get_repo(project_id: str, role: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM repos WHERE project_id=? AND role=?",
            (project_id, role),
        ).fetchone()
    return dict(row) if row else None


def upsert_repo(
    project_id: str,
    role: str,
    repo_path: str,
    github_repo: str,
    *,
    label: str | None = None,
    description: str | None = None,
    stage_branch: str = "stage",
    prod_branch: str = "main",
    test_command: str | None = None,
    is_active: bool = True,
) -> int:
    """Insert or update by (project_id, role). Returns the row id."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO repos(
                project_id, role, label, description, repo_path, github_repo,
                stage_branch, prod_branch, test_command, is_active
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, role) DO UPDATE SET
                label=excluded.label,
                description=excluded.description,
                repo_path=excluded.repo_path,
                github_repo=excluded.github_repo,
                stage_branch=excluded.stage_branch,
                prod_branch=excluded.prod_branch,
                test_command=excluded.test_command,
                is_active=excluded.is_active
            """,
            (
                project_id, role, label, description, repo_path, github_repo,
                stage_branch, prod_branch, test_command, int(is_active),
            ),
        )
        row = conn.execute(
            "SELECT id FROM repos WHERE project_id=? AND role=?",
            (project_id, role),
        ).fetchone()
    return int(row["id"])


def deactivate_repo(project_id: str, role: str) -> None:
    """Soft-delete (keeps history; flips is_active=0)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE repos SET is_active=0 WHERE project_id=? AND role=?",
            (project_id, role),
        )


# ----- group ↔ project links (M:N) -----

def link_group_to_project(chat_id: int, project_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO group_project_links(chat_id, project_id) VALUES(?, ?)",
            (chat_id, project_id),
        )


def unlink_group_from_project(chat_id: int, project_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM group_project_links WHERE chat_id=? AND project_id=?",
            (chat_id, project_id),
        )


def projects_for_group(chat_id: int) -> list[dict]:
    """Every project this Telegram group monitors. Empty list = all projects
    are considered (useful before any links are configured)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT p.* FROM projects p
               JOIN group_project_links g ON g.project_id = p.id
               WHERE g.chat_id = ?
               ORDER BY p.name""",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def groups_for_project(project_id: str) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM group_project_links WHERE project_id=?",
            (project_id,),
        ).fetchall()
    return [int(r["chat_id"]) for r in rows]


# ============================================================================
# developers — replaces .env TELEGRAM_DEVELOPER_IDS as the source of truth.
# .env value seeds this on first start; after that, edit via DB / future GUI.
# ============================================================================

def list_developers() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, label, added_at, added_by FROM developers ORDER BY added_at",
        ).fetchall()
    return [dict(r) for r in rows]


def add_developer(user_id: int, label: str | None = None, added_by: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO developers(user_id, label, added_by) VALUES(?, ?, ?)""",
            (user_id, label, added_by),
        )


def remove_developer(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM developers WHERE user_id=?", (user_id,))


def is_developer_in_db(user_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM developers WHERE user_id=?", (user_id,),
        ).fetchone()
    return row is not None


def import_chats_json_if_needed(json_path: Path) -> int:
    """One-shot import of legacy chats.json into the chats table."""
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


def bootstrap_from_env(
    *,
    repo_path: str,
    github_repo: str,
    stage_branch: str,
    prod_branch: str,
    monitored_groups: list[int],
    developer_ids: list[int],
    github_token: str | None = None,
    trigger_keywords: str = "",
    dry_run: bool = True,
    claude_cli: str = "claude",
    claude_timeout: int = 900,
    max_parallel_claude: int = 5,
) -> None:
    """First-run population: project + repo + group links + dev list + settings.

    Idempotent — only writes the project/repo if no projects exist yet, and
    only seeds the developer table for IDs not already present. Always
    refreshes the settings table from the provided values when missing
    (won't overwrite an already-set DB value).
    """
    if not list_projects():
        # No projects yet — create a default "main" one from .env values.
        upsert_project(
            "main",
            name="Default Project",
            description="Auto-created from .env on first start. Rename/extend later.",
            github_token=github_token,
        )
        if repo_path and github_repo:
            upsert_repo(
                "main", "backend",
                repo_path=repo_path,
                github_repo=github_repo,
                label="Backend (auto-imported)",
                description="Initial backend repo from .env REPO_PATH.",
                stage_branch=stage_branch or "stage",
                prod_branch=prod_branch or "main",
            )
        for chat_id in monitored_groups or []:
            link_group_to_project(chat_id, "main")
        log.info("bootstrapped 'main' project from .env values")

    # Seed developer allow-list (idempotent; INSERT OR IGNORE).
    for uid in developer_ids or []:
        add_developer(uid)

    # Seed mutable settings only if not yet in the table — never overwrite.
    seed_pairs = [
        ("TRIGGER_KEYWORDS",     trigger_keywords),
        ("DRY_RUN",              "true" if dry_run else "false"),
        ("CLAUDE_CLI",           claude_cli),
        ("CLAUDE_TIMEOUT",       str(claude_timeout)),
        ("MAX_PARALLEL_CLAUDE",  str(max_parallel_claude)),
        ("STAGE_BRANCH",         stage_branch or "stage"),
        ("PROD_BRANCH",          prod_branch or "main"),
        ("GITHUB_REPO",          github_repo or ""),
        ("REPO_PATH",            repo_path or ""),
    ]
    if github_token:
        seed_pairs.append(("GITHUB_TOKEN", github_token))
    for key, val in seed_pairs:
        if get_setting(key) is None:
            set_setting(key, val)


# ============================================================================
# claude_usage — har chaqiruvning sarfi
# ============================================================================

def record_usage(
    kind: str,
    envelope: dict | None,
    *,
    project_id: str | None = None,
    repo_role: str | None = None,
    issue_id: str | None = None,
    ok: bool = True,
    duration_ms: int = 0,
) -> None:
    """Store one Claude CLI call's cost/token usage.

    `envelope` is the parsed `--output-format json` object. None means the call
    failed before producing one (timeout, non-zero exit, unparseable stdout) —
    the row is still written with ok=0, because a call that burned wall-clock
    and produced nothing is exactly what a budget review needs to see. Silently
    dropping failures would make the ledger look healthier than reality.

    Never raises: accounting must not be able to break the thing it measures.
    """
    env = envelope or {}
    usage = env.get("usage") or {}
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO claude_usage(
                    kind, project_id, repo_role, issue_id, cost_usd,
                    input_tokens, output_tokens,
                    cache_creation_tokens, cache_read_tokens,
                    duration_ms, num_turns, ok
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kind,
                    project_id,
                    repo_role,
                    issue_id,
                    float(env.get("total_cost_usd") or 0.0),
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                    int(usage.get("cache_read_input_tokens") or 0),
                    int(env.get("duration_api_ms") or duration_ms or 0),
                    int(env.get("num_turns") or 0),
                    1 if ok else 0,
                ),
            )
    except Exception:  # noqa: BLE001
        log.exception("could not record claude usage (kind=%s)", kind)


def usage_totals(hours: int = 24) -> dict:
    """Rolling-window totals across all calls."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT
                   COUNT(*)                      AS calls,
                   COALESCE(SUM(cost_usd), 0)    AS cost_usd,
                   COALESCE(SUM(input_tokens), 0)          AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)         AS output_tokens,
                   COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                   COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens,
                   COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed
               FROM claude_usage
               WHERE created_at >= datetime('now', ?)""",
            (f"-{int(hours)} hours",),
        ).fetchone()
        return dict(row) if row else {}


def usage_by_kind(hours: int = 24) -> list[dict]:
    """Same window, split by call kind — shows WHERE the budget goes."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT kind,
                      COUNT(*)                   AS calls,
                      COALESCE(SUM(cost_usd), 0) AS cost_usd
               FROM claude_usage
               WHERE created_at >= datetime('now', ?)
               GROUP BY kind
               ORDER BY cost_usd DESC""",
            (f"-{int(hours)} hours",),
        ).fetchall()
        return [dict(r) for r in rows]
