"""Pull bug reports out of the CRM and feed them into the pipeline.

WHY POLLING AND NOT TELEGRAM
The original plan was for the CRM to post reports into the IT group and let
this bot read them there. That cannot work: Telegram never delivers one bot's
message to another bot, in any privacy mode
(https://core.telegram.org/bots/faq). The CRM still posts to the group so
humans see it immediately — but the bot's input comes from here.

WHY THE CURSOR LIVES ON OUR SIDE
`after_id` is stored in our own settings table, not on the server. This bot
runs on a laptop that gets closed, sleeps, and loses its network; a
server-side "unread" flag would be wrong the moment we crash between fetching
and processing. Owning the cursor means a restart never double-processes and
never skips.

AUTH
X-API-Key / X-API-Secret — a machine credential, not a person's login. See
the CRM's BugReportAgentApiKey for why.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import db

log = logging.getLogger(__name__)

CURSOR_KEY = "crm_bug_cursor"
_TIMEOUT = 30


def _cfg(key: str, default: str = "") -> str:
    return (db.get_setting(key) or default).strip()


def is_configured() -> bool:
    return bool(_cfg("crm_api_url") and _cfg("crm_agent_key") and _cfg("crm_agent_secret"))


def _post(path: str, payload: dict) -> dict | None:
    """POST to the CRM agent API. Returns None on any failure (logged)."""
    base = _cfg("crm_api_url").rstrip("/")
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": _cfg("crm_agent_key"),
            "X-API-Secret": _cfg("crm_agent_secret"),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        # 401 is worth shouting about: it means the key pair is wrong and
        # every future poll will fail the same way until someone fixes it.
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:  # noqa: BLE001
            pass
        log.warning("crm bugs: HTTP %s on %s %s", exc.code, path, detail)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("crm bugs: %s failed: %s", path, exc)
        return None


def fetch_pending(limit: int = 10) -> list[dict]:
    """New reports since our cursor. Cursor is NOT advanced here.

    Advancing on fetch would lose a report if we crash between fetching and
    processing it. The caller advances per item, after the work is safely
    handed off.
    """
    if not is_configured():
        return []
    after = 0
    raw = db.get_setting(CURSOR_KEY)
    if raw and str(raw).isdigit():
        after = int(raw)

    data = _post("/agent/bug-report/pending", {"after_id": after, "limit": limit})
    if not data:
        return []
    return data.get("data") or []


def advance_cursor(report_id: int) -> None:
    """Move the cursor forward — never backward, so a late reply cannot rewind it."""
    raw = db.get_setting(CURSOR_KEY)
    current = int(raw) if raw and str(raw).isdigit() else 0
    if report_id > current:
        db.set_setting(CURSOR_KEY, str(report_id))


def ack(report_id: int, status: str = "in_progress", note: str = "") -> bool:
    """Tell the CRM we picked this up (or are rejecting it)."""
    payload = {"id": int(report_id), "status": status}
    if note:
        payload["note"] = note[:2000]
    return _post("/agent/bug-report/ack", payload) is not None


def as_complaint(report: dict) -> str:
    """Render a report as the plain complaint text the classifier expects.

    Deliberately shaped like something a person would type. The classifier was
    trained on staff messages; handing it a JSON blob would push it toward
    "not our problem" for no good reason.
    """
    lines = [report.get("description") or ""]
    page = report.get("page_title") or ""
    route = report.get("route") or ""
    if page or route:
        lines.append(f"Sahifa: {page} {route}".strip())
    if report.get("marker"):
        lines.append("Muammo joyi skrinshotda qizil ramka bilan belgilangan.")
    if report.get("browser"):
        lines.append(f"Brauzer: {report['browser'][:120]}")
    if report.get("screen_size"):
        lines.append(f"Ekran: {report['screen_size']}")
    return "\n".join(x for x in lines if x)
