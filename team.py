"""The agent team — named roles that narrate what the pipeline is doing.

WHY NAMES AT ALL
One bot doing six different jobs reads like noise. The same bot saying
"Aziz (Testirovchik): 21 ta test o'tdi" reads like a team, and you can tell at
a glance which part of the pipeline you are looking at. That is the whole
value: legibility, not role-play.

WHAT A PERSONA IS — AND IS NOT
A persona is a LABEL ON A REAL PIPELINE STEP. It is not a separate Claude
invocation. Naming is free; spawning one agent per role would multiply the
bill by six for the same work. When a step genuinely splits into its own
agent later, the label is already in place and nothing above has to change.

NO EMPTY CHAIRS
A role is only listed once something real runs under it. UX/UI has no step in
the current pipeline, so Muhsin is defined but never speaks — a persona
announcing work nobody did is theatre, and it teaches you to distrust the
whole channel. Give him a step and he starts talking.

WHERE THEY SPEAK
Developer DM only. The monitored group is where staff describe problems and
gets exactly one status message per report (see main.STAGE_TEXT); dropping
team chatter in there would bury the next person's complaint.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Member:
    key: str
    name: str
    role: str
    emoji: str

    def say(self, text: str) -> str:
        return f"{self.emoji} {self.name} ({self.role}):\n{text}"


TEAM: dict[str, Member] = {
    # The bot itself — the one Bekzod actually talks to.
    "product": Member("product", "Ravshanaka", "Product Manager", "🧭"),
    # Triage: is this ours, whose is it, how urgent.
    "pm":      Member("pm", "Samar", "Project Manager", "📋"),
    # The two that map onto repos.role — the classifier already routes by it.
    "backend": Member("backend", "Bekzod", "Backend", "⚙️"),
    "frontend": Member("frontend", "Shaxzod", "Frontend", "🎨"),
    # Runs tests / verifies a fix.
    "qa":      Member("qa", "Aziz", "Testirovchik", "🧪"),
    # Branch, PR, merge, release.
    "devops":  Member("devops", "Muhammadjon", "DevOps", "🚀"),
    # Defined, deliberately silent until a UX step exists.
    "ux":      Member("ux", "Muhsin", "UX/UI dizayner", "🖌"),
}

# repos.role → persona. Anything unmapped falls back to the PM rather than
# inventing a name for a repo nobody assigned an owner to.
_REPO_ROLE_MAP = {
    "backend": "backend",
    "frontend": "frontend",
    "mobile": "frontend",
    "admin": "frontend",
}


def get(key: str) -> Member:
    return TEAM.get(key) or TEAM["pm"]


def for_repo_role(repo_role: str | None) -> Member:
    """Which engineer owns work in this repo."""
    if not repo_role:
        return TEAM["pm"]
    return TEAM.get(_REPO_ROLE_MAP.get(repo_role.lower(), ""), TEAM["pm"])


def say(key: str, text: str) -> str:
    """Render a line in a persona's voice."""
    return get(key).say(text)


def roster() -> str:
    """Who is on the team, and who is currently idle. Used by /team."""
    lines = ["AI jamoa", ""]
    for m in TEAM.values():
        idle = "  — hozircha bosqichi yo'q" if m.key == "ux" else ""
        lines.append(f"{m.emoji} {m.name} — {m.role}{idle}")
    lines += [
        "",
        "Hammasi bitta bot orqali gapiradi. Nom — quvurdagi bosqich belgisi, "
        "har biri alohida agent emas: alohida bo'lsa xarajat olti barobar "
        "bo'lardi, ish esa o'sha-o'sha.",
    ]
    return "\n".join(lines)
