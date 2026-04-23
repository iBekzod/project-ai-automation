"""Multi-session chat state for the developer DM chat mode.

Each session represents an independent conversation thread with Claude Code,
analogous to a separate chat on claude.ai. Sessions run in parallel — one
chat's in-flight Claude call does not block another. State persists across
bot restarts via `chats.json` next to the module.

Public API (used by main.py and claude_runner.py):
  - get_user_chats(user_id) -> UserChats
  - create_chat(user_id, name)
  - switch_chat(user_id, name)
  - delete_chat(user_id, name)
  - active_session(user_id)
  - record_turn(user_id, name, session_id_from_claude)
  - set_current_task / clear_current_task / cancel_current_task
  - export_for_list(user_id) -> list[dict]  (for /chatlist UI)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

CHATS_FILE = Path(__file__).resolve().parent / "chats.json"


@dataclass
class ChatSession:
    name: str
    # Claude Code session UUID — set after first turn when we capture it from
    # `claude --print --output-format json` response. Used for --resume.
    session_id: str | None = None
    turn_count: int = 0
    last_activity: str = field(default_factory=lambda: datetime.now().isoformat())
    # In-memory only — the asyncio.Task for the currently-running Claude call,
    # if any. Set when a call is dispatched, cleared on completion, cancelled
    # by /stop.
    current_task: asyncio.Task | None = None

    def to_json(self) -> dict:
        return {
            "session_id": self.session_id,
            "turn_count": self.turn_count,
            "last_activity": self.last_activity,
        }


@dataclass
class UserChats:
    user_id: int
    active: str | None = None  # active session name
    sessions: dict[str, ChatSession] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "active": self.active,
            "sessions": {n: s.to_json() for n, s in self.sessions.items()},
        }


# Module-level state.
_users: dict[int, UserChats] = {}


def _load_from_disk() -> None:
    if not CHATS_FILE.exists():
        return
    try:
        data = json.loads(CHATS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.exception("chats.json load failed: %s", exc)
        return
    for uid_str, ud in data.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        sessions: dict[str, ChatSession] = {}
        for name, sd in (ud.get("sessions") or {}).items():
            sessions[name] = ChatSession(
                name=name,
                session_id=sd.get("session_id"),
                turn_count=int(sd.get("turn_count") or 0),
                last_activity=sd.get("last_activity")
                or datetime.now().isoformat(),
            )
        _users[uid] = UserChats(
            user_id=uid,
            active=ud.get("active"),
            sessions=sessions,
        )


def _save_to_disk() -> None:
    data = {str(uid): uc.to_json() for uid, uc in _users.items()}
    try:
        CHATS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.exception("chats.json save failed")


def get_user_chats(user_id: int) -> UserChats:
    if user_id not in _users:
        _users[user_id] = UserChats(user_id=user_id)
    return _users[user_id]


def create_chat(user_id: int, name: str) -> ChatSession:
    uc = get_user_chats(user_id)
    if name not in uc.sessions:
        uc.sessions[name] = ChatSession(name=name)
    uc.active = name
    _save_to_disk()
    return uc.sessions[name]


def switch_chat(user_id: int, name: str) -> ChatSession | None:
    uc = get_user_chats(user_id)
    if name not in uc.sessions:
        return None
    uc.active = name
    _save_to_disk()
    return uc.sessions[name]


def delete_chat(user_id: int, name: str) -> bool:
    uc = get_user_chats(user_id)
    if name not in uc.sessions:
        return False
    # Cancel any in-flight task before removing.
    s = uc.sessions[name]
    if s.current_task and not s.current_task.done():
        s.current_task.cancel()
    del uc.sessions[name]
    if uc.active == name:
        uc.active = next(iter(uc.sessions), None)
    _save_to_disk()
    return True


def active_session(user_id: int) -> ChatSession | None:
    uc = get_user_chats(user_id)
    if uc.active and uc.active in uc.sessions:
        return uc.sessions[uc.active]
    return None


def record_turn(
    user_id: int,
    name: str,
    new_session_id: str | None = None,
) -> None:
    """Bump turn count + activity timestamp. If Claude returned a fresh
    session_id (first turn of a brand-new session), store it for future
    --resume calls."""
    uc = get_user_chats(user_id)
    s = uc.sessions.get(name)
    if s is None:
        return
    s.turn_count += 1
    s.last_activity = datetime.now().isoformat()
    if new_session_id and not s.session_id:
        s.session_id = new_session_id
    _save_to_disk()


def set_current_task(user_id: int, name: str, task: asyncio.Task) -> None:
    uc = get_user_chats(user_id)
    s = uc.sessions.get(name)
    if s is not None:
        s.current_task = task


def clear_current_task(user_id: int, name: str) -> None:
    uc = get_user_chats(user_id)
    s = uc.sessions.get(name)
    if s is not None:
        s.current_task = None


def cancel_current_task(user_id: int, name: str | None = None) -> bool:
    """Cancel the in-flight Claude call for the named chat (or the active one
    if name is None). Returns True if something was cancelled."""
    uc = get_user_chats(user_id)
    target_name = name or uc.active
    if not target_name:
        return False
    s = uc.sessions.get(target_name)
    if s is None or s.current_task is None or s.current_task.done():
        return False
    s.current_task.cancel()
    return True


def export_for_list(user_id: int) -> list[dict]:
    """Return serialisable summaries for building the /chatlist inline keyboard.

    Each entry: {name, turn_count, last_activity, busy, active}
    """
    uc = get_user_chats(user_id)
    out: list[dict] = []
    for name, s in uc.sessions.items():
        busy = bool(s.current_task and not s.current_task.done())
        out.append({
            "name": name,
            "turn_count": s.turn_count,
            "last_activity": s.last_activity,
            "busy": busy,
            "active": name == uc.active,
        })
    # Newest activity first
    out.sort(key=lambda d: d["last_activity"], reverse=True)
    return out


# Load on import so bot restarts resurrect prior sessions.
_load_from_disk()
