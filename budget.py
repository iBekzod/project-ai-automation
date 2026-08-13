"""Budget policy for the PM bot — decides whether a Claude call may run.

WHY THIS EXISTS
The bot is the PM for a team of agents, and agents spend money. Without a
gate, spend is whatever the message volume happens to be: a chatty group
burns the same budget as a real incident, and nobody notices until the bill.

WHERE IT SITS
One gate, in claude_runner.run_capped(). That is already the single
choke point every Claude invocation passes through (analyze, both
classifiers, chat). Putting the policy anywhere else means repeating it at
four call sites and forgetting the fifth.

WHAT IT SACRIFICES FIRST
Priority decides what gets dropped when money is tight — and the order is
deliberate:

  critical  a developer is waiting on a reply (/ask, /task, chat, retry).
            Never denied. If the human asked, the human gets an answer.
  normal    analysing a real reported problem. Denied only past the hard cap.
  low       the speculative classifier that fires on EVERY group message,
            including "rahmat" and "ok". First to go, and rightly so: it is
            the only tier that spends money without anyone having asked for
            anything.

DEFAULT IS WARN-ONLY
`budget_enforce` ships false. The limits below are guesses until real data
exists, and a wrong guess that silently refuses real work is worse than an
overspend nobody noticed for a day. So it logs and warns first; you turn
enforcement on once the numbers are real. Every limit is a DB setting, so
tuning needs no redeploy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import db

log = logging.getLogger(__name__)

# Priorities, cheapest-to-lose first.
LOW = "low"
NORMAL = "normal"
CRITICAL = "critical"

_ORDER = {LOW: 0, NORMAL: 1, CRITICAL: 2}

# Defaults. Placeholders until a week of real usage exists — see module docstring.
DEFAULTS = {
    "budget_enforce": "false",
    "budget_hour_soft_usd": "3.0",
    "budget_hour_hard_usd": "6.0",
    "budget_day_soft_usd": "25.0",
    "budget_day_hard_usd": "50.0",
}


@dataclass
class Decision:
    allowed: bool
    reason: str
    level: str          # ok | soft | hard
    spent_hour: float
    spent_day: float

    def __bool__(self) -> bool:
        return self.allowed


def _setting_float(key: str) -> float:
    raw = db.get_setting(key)
    if raw is None:
        raw = DEFAULTS.get(key, "0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("budget: setting %s is not a number (%r); using default", key, raw)
        return float(DEFAULTS.get(key, "0"))


def enforcing() -> bool:
    raw = db.get_setting("budget_enforce")
    if raw is None:
        raw = DEFAULTS["budget_enforce"]
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def check(kind: str, priority: str = NORMAL) -> Decision:
    """Should this call run?

    Never raises and never blocks on failure: if the ledger cannot be read we
    ALLOW. A broken accounting table must not be able to halt the bot — that
    would turn a reporting bug into an outage.
    """
    try:
        hour = float(db.usage_totals(hours=1).get("cost_usd") or 0.0)
        day = float(db.usage_totals(hours=24).get("cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        log.exception("budget: could not read usage; allowing call")
        return Decision(True, "ledger unreadable", "ok", 0.0, 0.0)

    hour_soft = _setting_float("budget_hour_soft_usd")
    hour_hard = _setting_float("budget_hour_hard_usd")
    day_soft = _setting_float("budget_day_soft_usd")
    day_hard = _setting_float("budget_day_hard_usd")

    over_hard = (hour_hard > 0 and hour >= hour_hard) or (day_hard > 0 and day >= day_hard)
    over_soft = (hour_soft > 0 and hour >= hour_soft) or (day_soft > 0 and day >= day_soft)

    rank = _ORDER.get(priority, _ORDER[NORMAL])

    if over_hard:
        level = "hard"
        # Only a waiting human gets through. Everything else waits for the
        # window to roll over.
        allow = rank >= _ORDER[CRITICAL]
        reason = "hard limit" if not allow else "hard limit, critical allowed"
    elif over_soft:
        level = "soft"
        # Drop the speculative tier only. Real work continues.
        allow = rank >= _ORDER[NORMAL]
        reason = "soft limit" if not allow else "soft limit, normal allowed"
    else:
        return Decision(True, "within budget", "ok", hour, day)

    if not allow and not enforcing():
        log.warning(
            "budget %s reached (hour $%.2f / day $%.2f) — would have blocked "
            "%s/%s, but budget_enforce is false",
            level, hour, day, kind, priority,
        )
        return Decision(True, f"{reason} (warn only)", level, hour, day)

    if not allow:
        log.warning(
            "budget %s reached (hour $%.2f / day $%.2f) — blocking %s/%s",
            level, hour, day, kind, priority,
        )

    return Decision(allow, reason, level, hour, day)


def snapshot() -> dict:
    """Everything needed to render a status line. Used by /budget and the watchdog."""
    hour = db.usage_totals(hours=1)
    day = db.usage_totals(hours=24)
    return {
        "enforcing": enforcing(),
        "hour": hour,
        "day": day,
        "by_kind": db.usage_by_kind(hours=24),
        "limits": {
            "hour_soft": _setting_float("budget_hour_soft_usd"),
            "hour_hard": _setting_float("budget_hour_hard_usd"),
            "day_soft": _setting_float("budget_day_soft_usd"),
            "day_hard": _setting_float("budget_day_hard_usd"),
        },
    }


def format_report() -> str:
    """Human-readable budget summary (Telegram-safe plain text)."""
    s = snapshot()
    h, d, lim = s["hour"], s["day"], s["limits"]

    lines = [
        "Sarf hisoboti",
        "",
        "Oxirgi 1 soat : $%.2f  (%d chaqiruv)" % (h.get("cost_usd", 0), h.get("calls", 0)),
        "Oxirgi 24 soat: $%.2f  (%d chaqiruv)" % (d.get("cost_usd", 0), d.get("calls", 0)),
        "",
        "Chegara: soatiga $%.2f / $%.2f, kuniga $%.2f / $%.2f" % (
            lim["hour_soft"], lim["hour_hard"], lim["day_soft"], lim["day_hard"],
        ),
        "Rejim : %s" % ("cheklaydi" if s["enforcing"] else "faqat ogohlantiradi"),
    ]

    if d.get("failed"):
        lines.append("Muvaffaqiyatsiz: %d ta (ular ham pul sarfladi)" % d["failed"])

    by_kind = s["by_kind"]
    if by_kind:
        lines += ["", "Nimaga ketdi (24 soat):"]
        for row in by_kind[:6]:
            lines.append("  %-14s %3d ta  $%.2f" % (row["kind"], row["calls"], row["cost_usd"]))

    # Cache is the cost driver on this workload; showing it stops the obvious
    # wrong conclusion ("our answers are too long").
    cache = d.get("cache_creation_tokens", 0)
    if cache:
        lines += [
            "",
            "Kesh yaratish: %s token — har chaqiruvda loyiha konteksti "
            "qaytadan yuklanadi. Asosiy xarajat shu yerda, javob uzunligida emas."
            % f"{cache:,}".replace(",", " "),
        ]

    return "\n".join(lines)
