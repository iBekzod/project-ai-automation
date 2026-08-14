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

WHY SEVERAL SOURCES
`crm_api_url` holds a LIST. Stage and prod run the same repository, so a bug
reported on either is the same bug and gets the same fix — pointing the bot at
one of them only decided which reports it never saw. It watches both, with a
SEPARATE cursor per environment: ids restart from 1 in each, so a shared cursor
would silently swallow everything below the other environment's high-water mark.

Every report carries the label of the environment it came from, because "#1"
alone is ambiguous once two servers are in play.

AUTH
X-API-Key / X-API-Secret — a machine credential, not a person's login. See
the CRM's BugReportAgentApiKey for why.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

import db

log = logging.getLogger(__name__)

CURSOR_KEY = "crm_bug_cursor"
_TIMEOUT = 30


def _cfg(key: str, default: str = "") -> str:
    return (db.get_setting(key) or default).strip()


def _sources() -> list[tuple[str, str]]:
    """(label, base_url) per configured environment.

    `crm_api_url` accepts several URLs separated by comma, semicolon or
    newline. One URL still works — it is just a list of one.

    The label comes from the host so it survives the URL changing, and it is
    what the cursor is keyed on; renaming it would replay that environment's
    backlog, so it is derived, never typed.
    """
    raw = _cfg("crm_api_url")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").replace("\n", ",").split(","):
        url = part.strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        host = (urllib.parse.urlparse(url).hostname or url).lower()
        label = "stage" if "stage" in host else ("dev" if "dev" in host else "prod")
        # Two URLs that map to the same label would fight over one cursor.
        base_label, n = label, 2
        while any(lbl == label for lbl, _ in out):
            label = f"{base_label}{n}"
            n += 1
        out.append((label, url))
    return out


def is_configured() -> bool:
    return bool(_sources() and _cfg("crm_agent_key") and _cfg("crm_agent_secret"))


def _post(base: str, path: str, payload: dict) -> dict | None:
    """POST to one CRM agent API. Returns None on any failure (logged)."""
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


def _cursor_key(label: str) -> str:
    return f"{CURSOR_KEY}:{label}"


def _read_cursor(label: str) -> int:
    raw = db.get_setting(_cursor_key(label))
    if raw is None:
        # First run after this became multi-source: inherit the single old
        # cursor rather than starting at 0, which would replay every report
        # that environment has ever filed — each replay being a paid analysis.
        raw = db.get_setting(CURSOR_KEY)
    return int(raw) if raw and str(raw).isdigit() else 0


def fetch_pending(limit: int = 10) -> list[dict]:
    """New reports since our cursor, across every configured environment.

    The cursor is NOT advanced here: advancing on fetch would lose a report if
    we crash between fetching and processing it. The caller advances per item,
    after the work is safely handed off.

    Each returned report gains `_source` (label) and `_base` (url) so the ack
    goes back to the server it came from — acking prod for a stage report would
    hit an unrelated row with the same id.
    """
    if not is_configured():
        return []

    out: list[dict] = []
    for label, base in _sources():
        data = _post(base, "/agent/bug-report/pending", {
            "after_id": _read_cursor(label),
            "limit": limit,
        })
        for report in (data or {}).get("data") or []:
            report["_source"] = label
            report["_base"] = base
            out.append(report)
    return out


def advance_cursor(report: dict) -> None:
    """Move one environment's cursor forward — never backward, so a late reply
    cannot rewind it."""
    label = report.get("_source") or "prod"
    rid = int(report.get("id") or 0)
    if rid > _read_cursor(label):
        db.set_setting(_cursor_key(label), str(rid))


def ack(report: dict, status: str = "in_progress", note: str = "") -> bool:
    """Tell the CRM the report came from that we picked this up."""
    base = report.get("_base")
    if not base:
        return False
    payload = {"id": int(report.get("id") or 0), "status": status}
    if note:
        payload["note"] = note[:2000]
    return _post(base, "/agent/bug-report/ack", payload) is not None


def label_of(report: dict) -> str:
    """Environment label for display — "#1" alone is ambiguous across servers."""
    return report.get("_source") or "prod"


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
