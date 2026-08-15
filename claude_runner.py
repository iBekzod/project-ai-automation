"""Runs the locally-installed Claude Code CLI non-interactively inside the target repo."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time

import budget
import config
import db

log = logging.getLogger(__name__)


# Global cap on concurrent Claude CLI invocations. The main Stage-2 `analyze`
# call, Stage-1 `classify_via_claude`, chat-mode `chat_with_claude` and the
# DM-intent classifier all respect the same semaphore — that way, whether
# updates arrive concurrently from groups, chats or DMs, we never spawn more
# than MAX_PARALLEL_CLAUDE Claude processes at once. Rate limits / CPU sanity.
#
# Initialised lazily: the semaphore must be created inside a running event
# loop (asyncio primitives bind to the current loop at construction time).
_parallel_sem: asyncio.Semaphore | None = None


def _get_parallel_sem() -> asyncio.Semaphore:
    global _parallel_sem
    if _parallel_sem is None:
        try:
            raw = int(os.environ.get("MAX_PARALLEL_CLAUDE", "5"))
        except ValueError:
            raw = 5
        _parallel_sem = asyncio.Semaphore(max(1, raw))
    return _parallel_sem


class BudgetDenied(RuntimeError):
    """Raised when the budget gate refuses a call. Carries the reason."""

    def __init__(self, decision):
        super().__init__(decision.reason)
        self.decision = decision


def _add_dir_args() -> list[str]:
    """`--add-dir` for every configured extra root that exists.

    Claude only reaches outside its working directory when told to, which is
    why the agent memory — a separate repo beside the CRM one — came back as
    "permission denied, cannot confirm it exists".

    Missing paths are skipped rather than passed through: the CLI rejects a
    non-existent --add-dir and the whole run would fail, turning a stale
    setting into a dead bot.
    """
    import os
    out: list[str] = []
    for d in (getattr(config, "CLAUDE_ADD_DIRS", None) or []):
        if os.path.isdir(d):
            out.extend(["--add-dir", d])
        else:
            log.warning("CLAUDE_ADD_DIRS: skipping missing path %s", d)
    return out


async def run_capped(fn, *args, priority: str = budget.NORMAL, _kind: str = "", **kwargs):
    """Dispatch a Claude call: budget gate first, then the concurrency cap.

    Every Claude CLI invocation — analyze, classify_via_claude,
    classify_dm_intent, chat_with_claude, run_claude — goes through here. That
    makes it the one place where "should we spend this?" can be asked, which is
    exactly why the policy lives here rather than at each call site: four call
    sites means four chances to forget the fifth.

    Order matters. The budget check happens BEFORE the semaphore, so a denied
    call does not sit in the queue holding a slot it was never going to use.

    Raises BudgetDenied when refused. Callers decide what that means — the
    group classifier treats it as "assume it is a problem, let a human look",
    which is the safe direction: we would rather over-report than silently
    swallow a complaint because the month was expensive.
    """
    kind = _kind or getattr(fn, "__name__", "unknown")
    decision = budget.check(kind, priority)
    if not decision.allowed:
        raise BudgetDenied(decision)

    sem = _get_parallel_sem()
    async with sem:
        return await asyncio.to_thread(fn, *args, **kwargs)


def _resolve_cli(name: str) -> str:
    """Resolve the Claude CLI to an absolute path.

    On Windows, `claude` is typically installed as `claude.cmd` via npm.
    subprocess.run(..., shell=False) does not honor PATHEXT, so a bare
    "claude" raises FileNotFoundError even when `where claude` finds it.
    shutil.which() does honor PATHEXT — use it.
    """
    if os.path.isabs(name) and os.path.exists(name):
        return name
    found = shutil.which(name)
    if found:
        return found
    # Last resort: try common npm-global locations on Windows.
    if os.name == "nt":
        for candidate in (
            os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
            os.path.expandvars(r"%APPDATA%\npm\claude.exe"),
        ):
            if os.path.exists(candidate):
                return candidate
    return name  # let subprocess raise a clear FileNotFoundError


INVESTIGATE_PROMPT = """Staff complaint from group "{group}": "{message}"
{image_section}
Your task has TWO parts. Do PART 1 first, always.

=== PART 1 — SCOPE TRIAGE ===

Classify the complaint into exactly one of these categories:

- backend_bug   — root cause is Laravel/PHP code in THIS repo (xonsaroy-latest)
- frontend_bug  — UI, mobile, or Flutter side. NOT this repo.
- infra_issue   — Kubernetes pod, Supervisor, queue worker not running, nginx/webhook down, DB full, disk full, migration not applied on server, etc. Operations concern, not code.
- user_error    — staff is using the system incorrectly; not actually a bug
- unclear       — the complaint is too vague, or evidence points multiple directions

Base your decision on CLAUDE.md, the visible code structure, and any screenshot attached.

=== PART 2 — DIAGNOSIS ===

Do the appropriate next step for the category you chose:

- backend_bug:
    Search this repo, locate file:line, explain the bug, then show COMPLETE new
    contents for every file you would change. Full file only — no diffs, no
    ellipses. Use fenced blocks labelled like:
        ```path=app/Modules/Name/Services/Example.php
        <FULL file contents>
        ```

- infra_issue:
    Name the suspect subsystem (worker, nginx, supervisor, migration, webhook)
    and suggest a SPECIFIC diagnostic/fix command the developer can run on the
    server. Examples: `kubectl logs pod/...`, `supervisorctl status`,
    `php artisan queue:restart`, `php artisan migrate --force`. Do NOT propose
    code fixes.

- frontend_bug:
    Briefly say which layer you suspect (Flutter screen, React page, CDN,
    caching). Do NOT propose code fixes — our repo is backend-only.

- user_error:
    Explain in one or two sentences what the user should do instead. No fix.

- unclear:
    State what specific information is missing (which screen, which contract
    id, which error message, which step). Request it. No fix.

Your narrative response will be handed to a formatter pass that extracts
structured JSON, so be concrete and explicit about which category you picked
and why.
"""


RETRY_INVESTIGATE_PROMPT = """Developer follow-up: "{instruction}"

Context — staff complaint from "{group}": "{message}"
{image_section}
Re-investigate with the developer's instruction applied. Follow the same
two-part structure as a fresh investigation (PART 1 scope triage, then PART 2
diagnosis appropriate to the category). Include COMPLETE new file contents in
fenced `path=...` blocks for every file you would modify.
"""


def _image_section(image_paths: list[str] | None) -> str:
    """Render the 'attached images' block if any photos came with the complaint."""
    if not image_paths:
        return ""
    lines = "\n".join(f"- {p}" for p in image_paths)
    return (
        "\nAttached screenshot(s). Use your Read tool on each path below to view "
        "them before deciding the category:\n"
        f"{lines}\n"
    )


# NOTE: FORMAT_PROMPT deliberately does NOT use Python str.format() for
# investigation substitution. The investigation text routinely contains `{` /
# `}` from PHP code, JSON examples, and fenced code blocks; .format() either
# raises KeyError mid-assembly or mangles the content silently. We build the
# final prompt with plain concatenation inside `_build_format_prompt` below.
FORMAT_PROMPT_HEAD = """You are converting a bug investigation report into a single strict JSON object.

The investigation report is enclosed between <<<INVESTIGATION>>> and <<<END>>> below. Read it in full before replying.

<<<INVESTIGATION>>>
"""

FORMAT_PROMPT_TAIL = """
<<<END>>>

Output rules — these are non-negotiable:
1. Reply with ONE JSON object and nothing else. No prose. No markdown. No code fences. No commentary.
2. Schema:
{
  "category": "backend_bug" | "frontend_bug" | "infra_issue" | "user_error" | "unclear",
  "is_bug": true or false,
  "summary": "Uzbek Latin, plain language, 1-2 sentences for non-technical staff",
  "technical_summary": "English diagnosis with file:line references (backend_bug) OR suggested ops command (infra_issue) OR brief explanation (other categories)",
  "eta_minutes": integer,
  "files_to_change": {"relative/path.ext": "FULL new file contents"},
  "confidence": "high" or "medium" or "low"
}
3. "category" MUST match exactly one of: backend_bug, frontend_bug, infra_issue, user_error, unclear.
4. "is_bug" rules:
   - true  only when category == "backend_bug" and the investigation located the defect concretely.
   - false otherwise (including infra_issue — those are real problems but not code bugs in this repo).
5. "summary" MUST be Uzbek (Latin script). "technical_summary" stays English.
6. "files_to_change":
   - Non-empty ONLY when category == "backend_bug" AND the investigation provided complete new contents.
   - Otherwise set files_to_change to {}.
   - Do NOT invent file contents. Do NOT propose file changes for non-backend categories.
7. "eta_minutes": realistic integer. For non-backend categories, use 0.
8. Use only information present in the investigation above. Do not add new findings.
"""


def _build_format_prompt(investigation: str) -> str:
    """Safely assemble the formatter prompt without triggering str.format() parsing."""
    return FORMAT_PROMPT_HEAD + investigation + FORMAT_PROMPT_TAIL


def _parse_envelope(stdout: str) -> tuple[str, dict | None]:
    """Split a `--output-format json` response into (answer_text, envelope).

    Falls back to (raw_stdout, None) when the payload is not the JSON envelope
    we expect. That fallback is deliberate: an accounting change must never be
    able to break the answer path. Losing one usage row is acceptable; losing a
    diagnosis because the ledger could not parse something is not.
    """
    text = (stdout or "").strip()
    if not text:
        return "", None
    try:
        envelope = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text, None
    if not isinstance(envelope, dict):
        return text, None
    answer = envelope.get("result") or envelope.get("text") or ""
    return (answer.strip() if isinstance(answer, str) else text), envelope


def run_claude(
    prompt: str,
    timeout: int | None = None,
    cwd_override: str | None = None,
    *,
    kind: str = "analyze",
    project_id: str | None = None,
    repo_role: str | None = None,
    issue_id: str | None = None,
) -> str:
    """Invoke `claude --print` and return the answer text.

    Runs with `--output-format json` so the cost/token block the CLI already
    produces is captured instead of discarded — see db.claude_usage. The
    RETURN VALUE is unchanged (the answer text), so callers need no edits.

    The prompt is piped via STDIN rather than passed on the command line.
    Reasons:
      * Windows `claude.cmd` → cmd.exe → node.exe re-parses argv, mangling long
        multi-line content (confirmed empirically: 2935-char argv prompts
        arrived at Claude with their embedded investigation body missing).
      * stdin sidesteps argv length limits, quoting rules, and newline escaping.
      * Reads config.* each call so Settings → Save → Start reflects new values.

    `cwd_override` (optional): when set, Claude runs in that directory
    instead of `config.REPO_PATH`. Used by the multi-project router so
    each repo's own CLAUDE.md / .ruflo agents are picked up.
    """
    cli_setting = config.CLAUDE_CLI
    cli = _resolve_cli(cli_setting)
    effective_timeout = timeout or config.CLAUDE_TIMEOUT
    cwd = cwd_override or str(config.REPO_PATH)
    log.info(
        "invoking claude (%s, prompt=%d chars via stdin, cwd=%s)",
        cli, len(prompt), cwd,
    )
    meta = {"project_id": project_id, "repo_role": repo_role, "issue_id": issue_id}
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = subprocess.run(
            [cli, "--print", "--output-format", "json", *_add_dir_args()],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=effective_timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        log.error(
            "claude CLI not found (CLAUDE_CLI=%s, resolved=%s). "
            "Set CLAUDE_CLI in Settings to the full path of claude.cmd "
            "(e.g. %%APPDATA%%\\npm\\claude.cmd).",
            cli_setting, cli,
        )
        raise
    except subprocess.TimeoutExpired:
        log.error("claude call timed out after %ss", effective_timeout)
        # A timeout still consumed wall-clock and almost certainly tokens; the
        # ledger has to show it, otherwise a repeatedly-timing-out prompt looks
        # free right up until the bill arrives.
        db.record_usage("timeout:" + kind, None, ok=False,
                        duration_ms=_elapsed_ms(), **meta)
        return ""

    if result.returncode != 0:
        log.error(
            "claude exited %d. stderr=%s stdout=%s",
            result.returncode,
            (result.stderr or "")[:500],
            (result.stdout or "")[:200],
        )
        db.record_usage(kind, None, ok=False, duration_ms=_elapsed_ms(), **meta)
        return ""
    if result.stderr:
        log.debug("claude stderr: %s", result.stderr[:500])

    text, envelope = _parse_envelope(result.stdout or "")
    db.record_usage(kind, envelope, ok=True, duration_ms=_elapsed_ms(), **meta)
    return text


def parse_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from claude's reply."""
    if not text:
        return {}
    # Preferred: content inside <diagnosis>...</diagnosis> tags.
    m = re.search(r"<diagnosis>\s*(\{.*?\})\s*</diagnosis>", text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Raw JSON (no tags, no prose).
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fenced block, e.g. ```json\n{...}\n```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: greedy match for the outermost { ... }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _extract_diagnosis(investigation: str) -> dict:
    """Pass 2 — ask Claude to convert the investigation narrative into strict JSON.

    Claude Code reliably ignores JSON-only instructions when it's also doing code
    investigation, so we split the work: pass 1 investigates freely, pass 2 is a pure
    extraction task with no repo access needed.
    """
    if not investigation:
        return {}
    log.info("formatting investigation into JSON (%d chars in)", len(investigation))
    formatted = run_claude(_build_format_prompt(investigation))
    data = parse_json(formatted)
    if not data:
        log.warning("formatter returned non-JSON: %s", formatted[:500])
    return data


VALID_CATEGORIES = {"backend_bug", "frontend_bug", "infra_issue", "user_error", "unclear"}


def _normalize_diagnosis(data: dict) -> dict:
    """Clamp/validate fields so downstream code can trust the shape."""
    if not data:
        return {}
    cat = str(data.get("category") or "").strip().lower()
    if cat not in VALID_CATEGORIES:
        cat = "unclear"
    data["category"] = cat
    # is_bug must be a bool and only True when backend_bug
    data["is_bug"] = bool(data.get("is_bug")) and cat == "backend_bug"
    # Non-backend categories must not carry files_to_change
    if cat != "backend_bug":
        data["files_to_change"] = {}
    return data


CLASSIFIER_TIMEOUT = 120  # seconds — classifier must stay snappy

# The scope section is built dynamically from the DB at call time so it always
# reflects the current set of projects + repos + descriptions.
CLASSIFIER_PROMPT_TEMPLATE = """You are a fast classifier for a Telegram support bot.

The bot manages these projects (each with one or more code repos):

{scope}

NOT covered by this bot:
- Anything outside the projects/repos listed above
- Pure infrastructure issues (Kubernetes, deployment, server, nginx, DB admin, disk/CPU) — those belong to ops, not code fixes
- Non-software: HR, workflow, sales, scheduling

MESSAGE:
<<<
{message}
>>>
{image_note}
Classify into exactly ONE of:
- OUR_PROBLEM   — complaint about a bug in one of the listed projects/repos
- OUT_OF_SCOPE  — real complaint but NOT in any of our projects (e.g. infra, third-party system, frontend not listed above)
- CHAT          — casual chat, greeting, acknowledgment, joke, status, off-topic

Languages you may see: Uzbek (Latin), Russian, English, mixed.

If OUR_PROBLEM, ALSO identify which project the issue belongs to AND which
repo within that project. The project_id and repo_role values are taken
EXACTLY from the list above. Use 'none' for both if not OUR_PROBLEM.

Guidance:
- A 500 error / stacktrace / API response screenshot → usually a backend repo.
- A mobile-app UI glitch screenshot → mobile repo if one exists, else OUT_OF_SCOPE.
- A web admin panel glitch → frontend / admin repo if one exists.
- Vague complaint ("ishlamayapti") + no context → lean OUR_PROBLEM with the
  project that best matches the group's typical traffic; dev can reclassify.
- Never choose CHAT if the message describes something not working.

Reply EXACTLY in this shape (no code fences, no prose before it):
VERDICT: OUR_PROBLEM | OUT_OF_SCOPE | CHAT
PROJECT: <project_id> | none
REPO: <repo_role> | none
REASON: <one short Uzbek Latin sentence>
"""


def _build_scope_section(chat_id: int | None) -> str:
    """Render every (project, repo) the group can see, for the classifier prompt.

    If the group is not linked to any project, all projects are listed (open
    config) — useful before the link table is populated. If no projects exist
    at all, returns a placeholder so Claude can still classify CHAT vs other.
    """
    try:
        import db as _db
        if _db._db_path is None:
            return "(no projects configured yet)"
        projects = _db.projects_for_group(chat_id) if chat_id is not None else []
        if not projects:
            projects = _db.list_projects()
        if not projects:
            return "(no projects configured yet)"
        out: list[str] = []
        for p in projects:
            head = f"=== Project: {p['id']} — {p['name']} ==="
            out.append(head)
            if p.get("description"):
                out.append(f"Description: {p['description']}")
            for r in _db.list_repos(p["id"]):
                line = f"  - repo '{r['role']}': {r.get('label') or r['github_repo']}"
                out.append(line)
                if r.get("description"):
                    out.append(f"    Scope: {r['description']}")
            out.append("")
        return "\n".join(out)
    except Exception:  # noqa: BLE001
        log.exception("could not build classifier scope section")
        return "(scope unavailable — DB read failed; treat as single-project bot)"


def classify_via_claude(
    text: str,
    image_paths: list[str] | None = None,
    chat_id: int | None = None,
) -> tuple[bool, str | None, str | None, str]:
    """Stage 1 classifier — ask Claude if the message is a problem report,
    and if so, which project and repo it belongs to.

    Runs in the bot directory (no CLAUDE.md, no repo search) with a short
    timeout. Returns (is_problem, project_id, repo_role, reason). Both
    project_id and repo_role are None for CHAT/OUT_OF_SCOPE.

    On any failure defaults to (True, None, None, ...) — better to run Stage
    2 on a borderline message than silently drop a real problem. Stage 2's
    routing logic falls back to the bootstrap 'main' project / 'backend' repo
    when project/repo are None.
    """
    text = (text or "").strip()
    if not text and not image_paths:
        return (False, None, None, "empty_message")

    image_note = ""
    if image_paths:
        count = len(image_paths)
        image_note = (
            f"\nNOTE: {count} screenshot(s) attached. Staff rarely send random "
            "photos; lean PROBLEM unless the text is clearly chat.\n"
        )

    prompt = CLASSIFIER_PROMPT_TEMPLATE.format(
        scope=_build_scope_section(chat_id),
        message=text[:800] if text else "(no text — screenshot only)",
        image_note=image_note,
    )

    classifier_cwd = os.path.dirname(os.path.abspath(__file__))
    cli = _resolve_cli(config.CLAUDE_CLI)
    log.info(
        "stage1 classifier invoking claude (%s, prompt=%d chars, cwd=%s)",
        cli, len(prompt), classifier_cwd,
    )
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            [cli, "--print", "--output-format", "json", *_add_dir_args()],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=classifier_cwd,
            timeout=CLASSIFIER_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        log.warning("stage1 classifier: claude CLI not found; defaulting to PROBLEM")
        return (True, None, None, "classifier_cli_missing")
    except subprocess.TimeoutExpired:
        log.warning(
            "stage1 classifier timed out after %ss; defaulting to PROBLEM",
            CLASSIFIER_TIMEOUT,
        )
        db.record_usage("timeout:classify", None, ok=False,
                        duration_ms=int((time.monotonic() - _t0) * 1000))
        return (True, None, None, "classifier_timeout")
    except Exception as exc:  # noqa: BLE001
        log.warning("stage1 classifier failed (%s); defaulting to PROBLEM", exc)
        return (True, None, None, "classifier_error")

    if result.returncode != 0:
        log.warning(
            "stage1 classifier exit %d: %s",
            result.returncode, (result.stderr or "")[:200],
        )
        db.record_usage("classify", None, ok=False,
                        duration_ms=int((time.monotonic() - _t0) * 1000))
        return (True, None, None, "classifier_nonzero_exit")

    # This is the call that fires on EVERY group message, so it is the one most
    # likely to dominate the bill — measuring it is the whole point.
    out, _envelope = _parse_envelope(result.stdout or "")
    db.record_usage("classify", _envelope, ok=True,
                    duration_ms=int((time.monotonic() - _t0) * 1000))
    if not out:
        return (True, None, None, "classifier_empty_output")

    verdict_line = ""
    project_line = ""
    repo_line = ""
    reason_line = ""
    for line in out.splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("VERDICT:") and not verdict_line:
            verdict_line = s
        elif u.startswith("PROJECT:") and not project_line:
            project_line = s
        elif u.startswith("REPO:") and not repo_line:
            repo_line = s
        elif u.startswith("REASON:") and not reason_line:
            reason_line = s
    verdict_upper = verdict_line.upper()

    def _strip_field(line: str) -> str:
        return (line.split(":", 1)[1].strip() if ":" in line else "")[:120]

    project = _strip_field(project_line) or None
    repo = _strip_field(repo_line) or None
    if project and project.lower() in ("none", "n/a", "-"):
        project = None
    if repo and repo.lower() in ("none", "n/a", "-"):
        repo = None
    reason = _strip_field(reason_line)

    if "OUR_PROBLEM" in verdict_upper:
        return (True, project, repo, f"our_problem: {reason}" if reason else "our_problem")
    if "OUT_OF_SCOPE" in verdict_upper:
        return (False, None, None, f"out_of_scope: {reason}" if reason else "out_of_scope")
    if "CHAT" in verdict_upper:
        return (False, None, None, f"chat: {reason}" if reason else "chat")
    # Legacy PROBLEM/CHAT responses still handled as a safety net.
    if "PROBLEM" in verdict_upper:
        return (True, project, repo, f"our_problem(legacy): {reason}" if reason else "our_problem")

    log.warning("stage1 classifier unparseable verdict: %s", out[:200])
    return (True, None, None, "classifier_unparseable")


# =============================================================================
# DM intent classifier — decides how to route a developer DM in chat-mode.
# =============================================================================

DM_INTENT_TIMEOUT = 90  # seconds

DM_INTENT_PROMPT_TEMPLATE = """You are routing a developer's DM to a Telegram support bot.

MESSAGE:
<<<
{message}
>>>
{image_note}
Classify into exactly one:
- ASK    — a question about the codebase / how something works / where to look. No file changes expected. Examples: "what does OrderService do?", "where is the V4 route for contracts?", "explain the queue flow"
- TASK   — an explicit request to change, add, fix, refactor, or delete code. Will trigger heavy analysis + potential commits. Examples: "add logging to OrderService when X", "fix the bitmask decoding in ContractComparison", "refactor OcrFirstExtractor to support a new tier"
- CHAT   — follow-up to a prior exchange, thinking out loud, casual exploration, short remark. Examples: "hmm, why though?", "ok, tell me more", "that's what I thought"

Reply EXACTLY in this format (no code fences, no prose before it):
INTENT: ASK | TASK | CHAT
REASON: <one short Uzbek Latin sentence>
"""


def classify_dm_intent(
    text: str,
    has_image: bool = False,
) -> tuple[str, str]:
    """Classify a developer DM into ASK / TASK / CHAT. Returns (intent, reason).

    Runs in the bot directory (no CLAUDE.md, no repo search). Timeout 90s.
    On any failure, defaults to ('CHAT', ...) — safest route: send to active
    chat session rather than silently triggering a heavyweight task.
    """
    text = (text or "").strip()
    if not text:
        return ("CHAT", "empty_message")

    image_note = ""
    if has_image:
        image_note = (
            "\nNOTE: a screenshot is attached. Screenshots usually accompany "
            "TASK or CHAT requests (sharing context), rarely pure ASK.\n"
        )

    prompt = DM_INTENT_PROMPT_TEMPLATE.format(
        message=text[:1200],
        image_note=image_note,
    )
    classifier_cwd = os.path.dirname(os.path.abspath(__file__))
    cli = _resolve_cli(config.CLAUDE_CLI)
    log.info("dm intent classifier invoking claude (prompt=%d chars)", len(prompt))
    _t0 = time.monotonic()
    try:
        result = subprocess.run(
            [cli, "--print", "--output-format", "json", *_add_dir_args()],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=classifier_cwd,
            timeout=DM_INTENT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("dm intent classifier failed (%s); defaulting to CHAT", exc)
        db.record_usage("timeout:classify_dm", None, ok=False,
                        duration_ms=int((time.monotonic() - _t0) * 1000))
        return ("CHAT", "intent_classifier_error")
    except Exception as exc:  # noqa: BLE001
        log.warning("dm intent classifier subprocess err: %s", exc)
        return ("CHAT", "intent_classifier_error")

    if result.returncode != 0:
        log.warning(
            "dm intent classifier exit %d: %s",
            result.returncode, (result.stderr or "")[:200],
        )
        db.record_usage("classify_dm", None, ok=False,
                        duration_ms=int((time.monotonic() - _t0) * 1000))
        return ("CHAT", "intent_classifier_nonzero")

    out, _envelope = _parse_envelope(result.stdout or "")
    db.record_usage("classify_dm", _envelope, ok=True,
                    duration_ms=int((time.monotonic() - _t0) * 1000))
    verdict_line = ""
    reason_line = ""
    for line in out.splitlines():
        s = line.strip()
        if s.upper().startswith("INTENT:") and not verdict_line:
            verdict_line = s
        elif s.upper().startswith("REASON:") and not reason_line:
            reason_line = s
    verdict_upper = verdict_line.upper()
    reason = (reason_line.split(":", 1)[1].strip() if ":" in reason_line else "")[:120]

    if "TASK" in verdict_upper:
        return ("TASK", reason or "task")
    if "ASK" in verdict_upper:
        return ("ASK", reason or "ask")
    if "CHAT" in verdict_upper:
        return ("CHAT", reason or "chat")

    log.warning("dm intent classifier unparseable: %s", out[:200])
    return ("CHAT", "intent_classifier_unparseable")


# =============================================================================
# Personal agent mode — the owner's assistant, not a code reviewer.
# =============================================================================

AGENT_PROMPT_HEADER = """You are Bekzod's personal engineering assistant at Xonsaroy, reached over Telegram.

HOW TO BEHAVE
Act like the assistant he talks to in his terminal: do the work, do not describe it and wait. You have the full tool set and the whole machine. When something is ambiguous, make the reasonable call and say which call you made — ask only when getting it wrong would be costly or irreversible.

Reply in the language he writes in (Uzbek Latin when he writes Uzbek). Telegram, not a terminal: no ANSI, keep formatting light, and put the answer first — he often reads it on a phone.

WHAT YOU CAN REACH
- D:\\projects\\xonsaroy — every repo: xonsaroy-latest (CRM backend), frontend, kubernetes, project-ai-automation, xonsaroy-agent-memory
- The cluster through `ssh xonsaroy-master kubectl ...`, GitHub through `gh`
- Databases through the DBeaver tunnel host (95.217.156.3)

MEMORY — READ IT, THEN ADD TO IT
xonsaroy-agent-memory is the shared notebook, linked to Obsidian. Before non-trivial work, read what is already known there (CRM/dev/ for architecture and traps, General/dev/ for environments and people). After work that taught you something durable — a trap, a fact that contradicts the docs, a decision and its reason — append it in the same style. Skip what the code or git history already says; write what a person could not derive by reading the repo.

This is how you get stronger over time. A session that solves a problem and records nothing has to solve it again.

SAFETY
`git push` is blocked while DRY_RUN is on: prepare the change, commit locally, and say it is ready. Before anything hard to reverse — dropping data, restarting production, sending to clients — say what you are about to do and wait for his answer.

His message:
"""


def agent_chat(
    session_id: str | None,
    message: str,
    image_paths: list[str] | None = None,
    timeout: int | None = None,
) -> tuple[str, str | None]:
    """A real working session, unlike chat_with_claude which is read-only.

    The read-only mode exists so a developer can ask about the codebase without
    risk. This one is the opposite: it is the owner's assistant and it is
    expected to change files, run commands and finish tasks. The gate is who
    may reach it — developer DMs only — not what it is allowed to touch.

    Same session plumbing: the id comes back on the first turn and is reused
    with --resume, so the conversation keeps its context across messages.
    """
    text = (message or "").strip()
    if not text and not image_paths:
        return ("(bo'sh xabar)", session_id)

    image_note = ""
    if image_paths:
        lines = "\n".join(f"- {p}" for p in image_paths)
        image_note = (
            "\n\nAttached image(s) — Read each one:\n" + lines + "\n"
        )

    full_prompt = AGENT_PROMPT_HEADER + text + image_note

    cli = _resolve_cli(config.CLAUDE_CLI)
    args = [cli, "--print", "--output-format", "json", *_add_dir_args()]

    # No --disallowedTools here, and prompts bypassed: with --print there is no
    # terminal to approve anything, so without this the assistant can plan work
    # but never carry it out.
    if getattr(config, "AGENT_FULL_ACCESS", True):
        args.extend(["--permission-mode", "bypassPermissions"])

    if session_id:
        args.extend(["--resume", session_id])

    effective_timeout = timeout or config.CLAUDE_TIMEOUT
    log.info(
        "agent invoking claude (session_id=%s, prompt=%d chars, full_access=%s)",
        session_id or "new", len(full_prompt),
        getattr(config, "AGENT_FULL_ACCESS", True),
    )

    try:
        result = subprocess.run(
            args,
            input=full_prompt,
            capture_output=True,
            text=True,
            cwd=str(config.REPO_PATH),
            timeout=effective_timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return (f"Vaqt tugadi ({effective_timeout}s). Vazifani kichikroq bo'laklarga bo'ling.", session_id)
    except FileNotFoundError:
        return ("Claude CLI topilmadi — Sozlamalarda CLAUDE_CLI yo'lini ko'rsating.", session_id)

    if result.returncode != 0:
        log.error("agent claude failed rc=%s stderr=%s", result.returncode, (result.stderr or "")[:400])
        return (f"Claude xato qaytardi (rc={result.returncode}).", session_id)

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ((result.stdout or "").strip()[:3500] or "(bo'sh javob)", session_id)

    reply = (payload.get("result") or "").strip()
    new_sid = payload.get("session_id")
    db.record_usage("agent", payload, ok=True)

    return (reply or "(bo'sh javob)", new_sid if not session_id else None)

# =============================================================================
# Chat mode — multi-turn Claude Code conversation per session.
# =============================================================================

CHAT_PROMPT_HEADER = """You are a coding assistant for the Xonsaroy Laravel 11 backend repo.

You MAY use Read, Grep, Glob, and read-only Bash (git log, cat, ls, php -l) to inspect the codebase. You MUST NOT use Edit, Write, MultiEdit or any other tool that modifies files. If the developer asks for changes, describe exactly what you would change and in which file, but DO NOT apply the changes yourself — tell them to reply with `!` prefix or `/task` if they want the change committed.

Respond in the language the developer is writing in (Uzbek Latin if they use Uzbek; otherwise match their language).

Developer message:
"""


def chat_with_claude(
    session_id: str | None,
    message: str,
    image_paths: list[str] | None = None,
    timeout: int | None = None,
) -> tuple[str, str | None]:
    """Send a message to Claude Code in chat mode.

    Returns (response_text, new_session_id). `new_session_id` is populated on
    the first turn of a new session (captured from `--output-format json`)
    and None on subsequent turns (we already know the id). The caller records
    the new id via chat_sessions.record_turn so later turns can --resume.

    Runs with cwd=REPO_PATH so Claude has full codebase access, but the
    CHAT_PROMPT_HEADER instructs it not to edit files. This is a soft
    constraint — Phase 2 will harden it with --disallowedTools once that's
    verified to work in --print mode.
    """
    text = (message or "").strip()
    if not text and not image_paths:
        return ("(bo'sh xabar)", session_id)

    image_note = ""
    if image_paths:
        lines = "\n".join(f"- {p}" for p in image_paths)
        image_note = (
            "\n\nAttached screenshot(s) (use your Read tool on each):\n"
            f"{lines}\n"
        )

    full_prompt = CHAT_PROMPT_HEADER + text + image_note

    cli = _resolve_cli(config.CLAUDE_CLI)
    args = [cli, "--print", "--output-format", "json", *_add_dir_args()]
    # Hard tool-gate: chat mode is read-only. Claude can Read / Grep / Glob /
    # run read-only Bash, but cannot mutate files. Any edit requested in chat
    # gets described, not applied — the dev promotes it to /task or `!` for
    # an actual commit. If the CLI ever renames these tools, the soft prompt
    # header still describes the rule as a backstop.
    args.extend([
        "--disallowedTools",
        "Edit,Write,MultiEdit,NotebookEdit",
    ])
    if session_id:
        args.extend(["--resume", session_id])

    effective_timeout = timeout or config.CLAUDE_TIMEOUT
    log.info(
        "chat invoking claude (session_id=%s, prompt=%d chars, cwd=%s)",
        session_id or "new", len(full_prompt), config.REPO_PATH,
    )
    try:
        result = subprocess.run(
            args,
            input=full_prompt,
            capture_output=True,
            text=True,
            cwd=str(config.REPO_PATH),
            timeout=effective_timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        log.error("chat: claude CLI not found (CLAUDE_CLI=%s)", config.CLAUDE_CLI)
        raise
    except subprocess.TimeoutExpired:
        log.error("chat: claude call timed out after %ss", effective_timeout)
        return ("(javob vaqt tugashi tufayli qaytmadi)", session_id)

    if result.returncode != 0:
        log.error(
            "chat: claude exited %d. stderr=%s stdout=%s",
            result.returncode,
            (result.stderr or "")[:500],
            (result.stdout or "")[:200],
        )
        return (
            f"(Claude xato {result.returncode} bilan chiqdi — loglarni tekshiring)",
            session_id,
        )

    stdout = (result.stdout or "").strip()
    if not stdout:
        return ("(bo'sh javob)", session_id)

    # Parse Claude's JSON envelope. Fall back to raw stdout if JSON parse fails.
    try:
        envelope = json.loads(stdout)
        text_out = envelope.get("result") or envelope.get("text") or ""
        new_sid = envelope.get("session_id") or envelope.get("sessionId")
        # The cost block was already arriving here and being dropped on the
        # floor. Chat turns are the long ones, so this is where a single
        # runaway conversation shows up first.
        db.record_usage("chat", envelope, ok=True)
        if new_sid and not session_id:
            return (text_out.strip() or "(bo'sh javob)", new_sid)
        return (text_out.strip() or "(bo'sh javob)", session_id)
    except json.JSONDecodeError:
        log.warning("chat: claude stdout not JSON; using raw text")
        db.record_usage("chat", None, ok=False)
        return (stdout, session_id)


def _fallback_unclear(investigation: str) -> dict:
    """When Pass 2 fails, wrap Pass 1's narrative as an 'unclear' diagnosis.

    This keeps the developer in the loop — they see Claude's raw investigation
    in the DM and can hit Retry with a clarifying instruction instead of the
    bot going silent.
    """
    body = (investigation or "").strip()
    if not body:
        return {}
    log.warning("formatter pass failed; surfacing Pass 1 text as unclear diagnosis")
    return {
        "category": "unclear",
        "is_bug": False,
        "summary": (
            "Claude tahlilni qaytardi, lekin tuzilgan JSON chiqmadi. "
            "Batafsil natija texnik qismda. Qayta urinib ko'ring yoki tafsilotlarni qo'shing."
        ),
        "technical_summary": body[:2000],
        "eta_minutes": 0,
        "files_to_change": {},
        "confidence": "low",
    }


def analyze(
    message: str,
    group: str,
    image_paths: list[str] | None = None,
    repo_path_override: str | None = None,
) -> dict | None:
    """Return diagnosis dict with category+is_bug, or None when nothing parseable.

    Two-pass:
      1. Free-form investigation (Claude Code's natural mode).
      2. Strict JSON extraction from the investigation text.
    Pass 1's output is optimistically parsed first — if it already contains
    valid JSON, pass 2 is skipped. If pass 2 fails, we fall back to an
    "unclear" diagnosis that carries pass 1's narrative so the dev still sees
    Claude's work.

    `repo_path_override` (optional): when set, Claude runs in that directory
    instead of `config.REPO_PATH`. Used by the multi-project router so each
    repo's own CLAUDE.md and `.ruflo/` are loaded.

    Returns a diagnosis for ALL classified categories (backend_bug,
    frontend_bug, infra_issue, user_error, unclear). The caller decides which
    categories trigger the fix pipeline.
    """
    prompt = INVESTIGATE_PROMPT.format(
        group=group,
        message=message,
        image_section=_image_section(image_paths),
    )
    investigation = run_claude(prompt, cwd_override=repo_path_override)
    if not investigation:
        log.warning("claude investigation returned empty output")
        return None
    log.info("investigation (%d chars): %s", len(investigation), investigation[:300])

    data = parse_json(investigation)
    if not data or "category" not in data:
        data = _extract_diagnosis(investigation)

    data = _normalize_diagnosis(data)
    if not data:
        data = _normalize_diagnosis(_fallback_unclear(investigation))
    if not data:
        return None

    log.info(
        "claude: category=%s is_bug=%s — %s",
        data.get("category"), data.get("is_bug"),
        (data.get("summary") or "")[:120],
    )
    return data


def analyze_with_instruction(
    message: str,
    group: str,
    instruction: str,
    image_paths: list[str] | None = None,
) -> dict | None:
    prompt = RETRY_INVESTIGATE_PROMPT.format(
        instruction=instruction,
        group=group,
        message=message,
        image_section=_image_section(image_paths),
    )
    investigation = run_claude(prompt)
    if not investigation:
        return None
    log.info("retry investigation (%d chars): %s", len(investigation), investigation[:300])

    data = parse_json(investigation)
    if not data or "category" not in data:
        data = _extract_diagnosis(investigation)

    data = _normalize_diagnosis(data)
    if not data:
        data = _normalize_diagnosis(_fallback_unclear(investigation))
    if not data:
        return None
    return data
