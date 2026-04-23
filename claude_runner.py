"""Runs the locally-installed Claude Code CLI non-interactively inside the target repo."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess

import config

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


async def run_capped(fn, *args, **kwargs):
    """Wrapper around asyncio.to_thread that respects MAX_PARALLEL_CLAUDE.

    Every Claude CLI invocation — analyze, classify_via_claude, classify_dm_intent,
    chat_with_claude, run_claude — should be dispatched through this so that
    parallel chats can't spawn an unbounded pile of `claude.exe` processes.
    """
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


def run_claude(prompt: str, timeout: int | None = None) -> str:
    """Invoke `claude --print` in REPO_PATH and return stdout.

    The prompt is piped via STDIN rather than passed on the command line.
    Reasons:
      * Windows `claude.cmd` → cmd.exe → node.exe re-parses argv, mangling long
        multi-line content (confirmed empirically: 2935-char argv prompts
        arrived at Claude with their embedded investigation body missing).
      * stdin sidesteps argv length limits, quoting rules, and newline escaping.
      * Reads config.* each call so Settings → Save → Start reflects new values.
    """
    cli_setting = config.CLAUDE_CLI
    cli = _resolve_cli(cli_setting)
    effective_timeout = timeout or config.CLAUDE_TIMEOUT
    log.info(
        "invoking claude (%s, prompt=%d chars via stdin, cwd=%s)",
        cli, len(prompt), config.REPO_PATH,
    )
    try:
        result = subprocess.run(
            [cli, "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(config.REPO_PATH),
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
        return ""

    if result.returncode != 0:
        log.error(
            "claude exited %d. stderr=%s stdout=%s",
            result.returncode,
            (result.stderr or "")[:500],
            (result.stdout or "")[:200],
        )
        return ""
    if result.stderr:
        log.debug("claude stderr: %s", result.stderr[:500])
    return (result.stdout or "").strip()


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

CLASSIFIER_PROMPT_TEMPLATE = """You are a fast classifier for a Telegram support bot.

PROJECT SCOPE — this bot only helps with bugs in the "Xonsaroy" Laravel 11 backend API:
- Real-estate management: apartments, buildings, bookings, clients
- Contract scanning / OCR / AI signature and field extraction
- Orders, payments, payment history
- V4 REST API (/api/v4/...)
- Telegram bot notifications emitted BY the backend
- Queue workers, supervisor, scheduled jobs, backend-side integrations

NOT covered by this bot:
- Frontend / mobile app / Flutter UI / web CSS / HTML
- Infrastructure: Kubernetes, deployment, nginx, server, DB admin, disk/CPU
- Non-software: HR, workflow, sales, scheduling

MESSAGE:
<<<
{message}
>>>
{image_note}
Classify into exactly ONE of:
- OUR_PROBLEM   — complaint about a bug in the Xonsaroy backend scope above
- OUT_OF_SCOPE  — real complaint but NOT in our scope (frontend, infra, other system)
- CHAT          — casual chat, greeting, acknowledgment, joke, status, off-topic

Languages you may see: Uzbek (Latin), Russian, English, mixed.

Guidance:
- A screenshot of a 500 error / stacktrace / API response → OUR_PROBLEM.
- A screenshot of a mobile app UI glitch or web styling issue → OUT_OF_SCOPE.
- Vague complaint ("ishlamayapti") with no context → lean OUR_PROBLEM if it mentions anything backend (API, data, contract, order, payment); lean OUT_OF_SCOPE if it's clearly about a screen/button.
- If genuinely unsure between OUR_PROBLEM and OUT_OF_SCOPE, choose OUR_PROBLEM — dev can reclassify.
- Never choose CHAT if the message describes something not working, even casually.

Reply with EXACTLY this shape (no code fences, no prose before it):
VERDICT: OUR_PROBLEM
REASON: <one short Uzbek Latin sentence>

(or VERDICT: OUT_OF_SCOPE / VERDICT: CHAT with matching REASON)
"""


def classify_via_claude(
    text: str,
    image_paths: list[str] | None = None,
) -> tuple[bool, str]:
    """Stage 1 classifier — ask Claude if the message is a problem report.

    Runs in the bot's own directory (not REPO_PATH), so Claude does NOT load
    CLAUDE.md or search the 107-module Laravel repo — keeping this call cheap
    (~10-15s). Uses its own short timeout so a stalled classifier doesn't
    block a real complaint for 15 minutes.

    Returns (is_problem, reason). On any failure, defaults to (True, ...) —
    we'd rather run Stage 2 on a chat message than miss a real problem.
    """
    text = (text or "").strip()
    if not text and not image_paths:
        return (False, "empty_message")

    image_note = ""
    if image_paths:
        count = len(image_paths)
        image_note = (
            f"\nNOTE: {count} screenshot(s) attached. Staff rarely send random "
            "photos; lean PROBLEM unless the text is clearly chat.\n"
        )

    prompt = CLASSIFIER_PROMPT_TEMPLATE.format(
        message=text[:800] if text else "(no text — screenshot only)",
        image_note=image_note,
    )

    classifier_cwd = os.path.dirname(os.path.abspath(__file__))
    cli = _resolve_cli(config.CLAUDE_CLI)
    log.info(
        "stage1 classifier invoking claude (%s, prompt=%d chars, cwd=%s)",
        cli, len(prompt), classifier_cwd,
    )
    try:
        result = subprocess.run(
            [cli, "--print"],
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
        return (True, "classifier_cli_missing")
    except subprocess.TimeoutExpired:
        log.warning(
            "stage1 classifier timed out after %ss; defaulting to PROBLEM",
            CLASSIFIER_TIMEOUT,
        )
        return (True, "classifier_timeout")
    except Exception as exc:  # noqa: BLE001
        log.warning("stage1 classifier failed (%s); defaulting to PROBLEM", exc)
        return (True, "classifier_error")

    if result.returncode != 0:
        log.warning(
            "stage1 classifier exit %d: %s",
            result.returncode, (result.stderr or "")[:200],
        )
        return (True, "classifier_nonzero_exit")

    out = (result.stdout or "").strip()
    if not out:
        return (True, "classifier_empty_output")

    verdict_line = ""
    reason_line = ""
    for line in out.splitlines():
        s = line.strip()
        if s.upper().startswith("VERDICT:") and not verdict_line:
            verdict_line = s
        elif s.upper().startswith("REASON:") and not reason_line:
            reason_line = s
    verdict_upper = verdict_line.upper()
    reason = (reason_line.split(":", 1)[1].strip() if ":" in reason_line else "")[:120]

    if "OUR_PROBLEM" in verdict_upper:
        return (True, f"our_problem: {reason}" if reason else "our_problem")
    if "OUT_OF_SCOPE" in verdict_upper:
        return (False, f"out_of_scope: {reason}" if reason else "out_of_scope")
    if "CHAT" in verdict_upper:
        return (False, f"chat: {reason}" if reason else "chat")
    # Older PROBLEM/CHAT responses still handled as a safety net.
    if "PROBLEM" in verdict_upper:
        return (True, f"our_problem(legacy): {reason}" if reason else "our_problem")

    # Malformed output — Claude didn't follow the format. Lean "proceed".
    log.warning("stage1 classifier unparseable verdict: %s", out[:200])
    return (True, "classifier_unparseable")


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
    try:
        result = subprocess.run(
            [cli, "--print"],
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
        return ("CHAT", "intent_classifier_error")
    except Exception as exc:  # noqa: BLE001
        log.warning("dm intent classifier subprocess err: %s", exc)
        return ("CHAT", "intent_classifier_error")

    if result.returncode != 0:
        log.warning(
            "dm intent classifier exit %d: %s",
            result.returncode, (result.stderr or "")[:200],
        )
        return ("CHAT", "intent_classifier_nonzero")

    out = (result.stdout or "").strip()
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
    args = [cli, "--print", "--output-format", "json"]
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
        if new_sid and not session_id:
            return (text_out.strip() or "(bo'sh javob)", new_sid)
        return (text_out.strip() or "(bo'sh javob)", session_id)
    except json.JSONDecodeError:
        log.warning("chat: claude stdout not JSON; using raw text")
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
) -> dict | None:
    """Return diagnosis dict with category+is_bug, or None when nothing parseable.

    Two-pass:
      1. Free-form investigation (Claude Code's natural mode).
      2. Strict JSON extraction from the investigation text.
    Pass 1's output is optimistically parsed first — if it already contains
    valid JSON, pass 2 is skipped. If pass 2 fails, we fall back to an
    "unclear" diagnosis that carries pass 1's narrative so the dev still sees
    Claude's work.

    Returns a diagnosis for ALL classified categories (backend_bug,
    frontend_bug, infra_issue, user_error, unclear). The caller decides which
    categories trigger the fix pipeline.
    """
    prompt = INVESTIGATE_PROMPT.format(
        group=group,
        message=message,
        image_section=_image_section(image_paths),
    )
    investigation = run_claude(prompt)
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
