"""Telegram bot entry point.

Flow:
  group message  ->  claude analyzes  ->  DM developer with Accept/Retry/Skip
  Accept  ->  write files, push branch, open PR, merge to stage  ->  Publish button
  Publish ->  merge stage -> prod, push, notify group
  Retry   ->  bot waits for a follow-up DM, re-runs claude with the instruction
  Skip    ->  drop the issue
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import chat_sessions
import db
import git_ops
import github_ops
import updater
from bot_state import state as bot_state
from claude_runner import (
    analyze,
    analyze_with_instruction,
    chat_with_claude,
    classify_dm_intent,
    classify_via_claude,
    run_capped,
    run_claude,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")


async def _safe_md_send(send_fn, text: str, **kwargs) -> None:
    """Send `text` via `send_fn` with parse_mode='Markdown', falling back to
    plain text if Telegram rejects the parse.

    Telegram's legacy Markdown is fragile around stray underscores
    (`DRY_RUN` becomes "DRY" + italic-open + "RUN" → unterminated entity)
    and other special chars. Rather than escape every dynamic value at the
    call site, we attempt the formatted send once and degrade gracefully
    on failure.
    """
    try:
        await send_fn(text, parse_mode="Markdown", **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("markdown send failed (%s); retrying as plain text", exc)
        try:
            await send_fn(text, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("plain-text fallback also failed")


async def _dm_all_devs(ctx, text: str, **kwargs) -> None:
    """Send `text` to every configured developer.

    Used for messages that should reach every dev who can act on them
    (diagnosis cards, status updates after Accept/Push/Rollback). Per-dev DM
    failures are logged but don't abort — if one dev's chat is broken, the
    others still get the message.

    For confirmations tied to ONE dev's tap (TASK confirmation, retry input
    prompt), keep using direct `q.message.reply_text(...)` or
    `ctx.bot.send_message(chat_id=that_dev_id, ...)` — those are
    intentionally per-user.
    """
    for dev_id in config.TELEGRAM_DEVELOPER_IDS or []:
        try:
            await ctx.bot.send_message(chat_id=dev_id, text=text, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("DM to dev %s failed: %s", dev_id, exc)


def _image_tmp_dir() -> Path:
    """Return the directory where photo attachments get staged.

    Must live INSIDE the target repo so Claude Code's Read tool (sandboxed to
    the cwd and below) can open the image. `.ruflo/tmp/` is gitignored in
    xonsaroy-latest's .gitignore. Computed per-call so `config.reload()` picks
    up a new REPO_PATH without needing a process restart.
    """
    return config.REPO_PATH / ".ruflo" / "tmp" / "images"


# ---------- in-memory issue store ----------

@dataclass
class Issue:
    id: str
    group_id: int
    group_title: str
    message: str
    user_message_id: int
    diagnosis: dict
    # The bot's status message in the group. Every later stage (approved,
    # testing, released) edits THIS message rather than posting a new one, so
    # one report never turns into five posts. None for DM-originated reports —
    # set_issue_stage() no-ops then.
    ack_message_id: int | None = None
    # Routing — set by Stage 1 classifier (None means "fall back to default
    # 'main' / 'backend' for backwards compat with single-project setups").
    project_id: str | None = None
    repo_role: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    merged_to_stage: bool = False
    awaiting_retry_prompt: bool = False
    # When a dev taps the Qayta button or runs `/retry` without a hint, we set
    # this to their telegram user_id. Only that dev's next DM is consumed as
    # the retry instruction — prevents cross-talk between multiple devs who
    # may be acting on different issues at the same time.
    retry_initiator: int | None = None
    created_at: datetime = field(default_factory=datetime.now)


ISSUES: dict[str, Issue] = {}


def _persist_issue(issue: Issue) -> None:
    """Mirror the in-memory Issue to SQLite. Called after every mutation."""
    try:
        from dataclasses import asdict
        d = asdict(issue)
        # asdict() turns datetime into a datetime object; SQLite is happy
        # with the ISO string representation, but we never need to round-
        # trip created_at so just drop it from the persistence dict.
        d.pop("created_at", None)
        db.save_issue(d)
    except Exception:  # noqa: BLE001
        log.exception("could not persist issue %s", issue.id)


def _drop_issue(issue_id: str) -> None:
    """Remove from in-memory cache AND mark closed in DB."""
    ISSUES.pop(issue_id, None)
    try:
        db.close_issue(issue_id)
    except Exception:  # noqa: BLE001
        log.exception("could not close issue %s in db", issue_id)


def _hydrate_issues_from_db() -> None:
    """Called once at startup so open issues from the previous run come back."""
    try:
        for d in db.load_open_issues():
            issue = Issue(
                id=d["id"],
                group_id=d.get("group_id") or 0,
                group_title=d.get("group_title") or "",
                message=d.get("message") or "",
                user_message_id=d.get("user_message_id") or 0,
                diagnosis=d.get("diagnosis") or {},
                project_id=d.get("project_id"),
                repo_role=d.get("repo_role"),
                branch=d.get("branch"),
                pr_url=d.get("pr_url"),
                merged_to_stage=bool(d.get("merged_to_stage")),
                awaiting_retry_prompt=bool(d.get("awaiting_retry_prompt")),
                retry_initiator=d.get("retry_initiator"),
            )
            ISSUES[issue.id] = issue
        if ISSUES:
            log.info("hydrated %d open issue(s) from sqlite", len(ISSUES))
    except Exception:  # noqa: BLE001
        log.exception("issue hydration failed")


# =============================================================================
# Stage 1 — Message Classifier (Claude-powered)
# -----------------------------------------------------------------------------
# Every group message is classified as "problem" or "chat" by a short Claude
# CLI call. Only problems flow to Stage 2 (the full solution-finder analyze).
#
# The Claude classifier lives in claude_runner.classify_via_claude(). It runs
# with cwd=bot_dir (no CLAUDE.md, no repo search), a 120s timeout, and a tiny
# prompt — so it stays fast (~10-15s per call). On any failure it defaults to
# `is_problem=True` so real complaints never get silently dropped.
#
# DM entry point (on_dm) does NOT use this — developer DMs are intentional
# tasks and always go straight to Stage 2.
# =============================================================================


def _diagnosis_keyboard(issue_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Qabul",      callback_data=f"acc:{issue_id}"),
        InlineKeyboardButton("Qayta",      callback_data=f"rty:{issue_id}"),
        InlineKeyboardButton("O'tkazish",  callback_data=f"skp:{issue_id}"),
    ]])


def _retry_only_keyboard(issue_id: str) -> InlineKeyboardMarkup:
    """For non-backend diagnoses: no Accept (no fix to apply), just Retry/Skip."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Qayta",     callback_data=f"rty:{issue_id}"),
        InlineKeyboardButton("O'tkazish", callback_data=f"skp:{issue_id}"),
    ]])


# Category → (group reply template, developer DM prefix, keyboard builder or None)
# The group reply is what staff in the monitored chat see; the developer DM is
# private and shows the full diagnosis.
CATEGORY_GROUP_REPLY = {
    "backend_bug":  "Tekshirilmoqda ({id}). Taxminan ~{eta} daqiqa.",
    "frontend_bug": "Bu frontend tomonida bo'lishi mumkin — frontend jamoaga o'tkazildi.",
    "infra_issue":  "Server/infra muammosi bo'lishi mumkin — texnik jamoaga o'tkazildi.",
    "user_error":   "Bu dastur xatosi emas: {summary}",
    "unclear":      "Qo'shimcha ma'lumot kerak — iltimos, screenshot yuboring yoki qayerda xato sodir bo'lishini aniqroq yozing.",
}

# =============================================================================
# Guruhdagi holat xabari — BITTA xabar, bosqichma-bosqich tahrirlanadi
# =============================================================================
#
# The group gets exactly one message per report and that message is edited as
# the pipeline advances. Message count stays 1; the content is always current.
#
# Why edit instead of posting updates: the group is where staff describe
# problems. A stream of "PR ochildi" / "merge qilindi" posts buries the next
# person's report and trains everyone to scroll past the channel. But silence
# is just as bad — the pipeline runs for minutes and can sit waiting for a
# human, and a reporter staring at "analysing..." for ten minutes assumes it
# is stuck.
#
# Language is deliberately non-technical: no category keys, no file:line, no
# branch names. The person who reported it wants to know what is broken and
# when it will work. The technical detail belongs in the developer's DM card.
#
# A new MESSAGE is only justified when the bot needs something FROM the person
# (a question). A status change never is.

STAGE_TEXT = {
    "received":  "Ko'rdim, ko'rib chiqyapman...",
    "diagnosed": "Muammo topildi: {detail}\nTuzatish tayyorlanmoqda.",
    "awaiting":  "Tuzatish tayyor, tasdiq kutilmoqda.",
    "testing":   "Tasdiqlandi, sinovdan o'tkazilmoqda.",
    "released":  "✅ Tuzatildi va ishga tushdi.",
    "rejected":  "Bu safar tuzatilmadi. IT bo'limi qo'lda ko'rib chiqadi.",
    "failed":    "⚠️ Avtomatik tahlil bajarilmadi. IT bo'limi qo'lda ko'radi.",
}


async def set_stage(bot, chat_id: int, message_id: int, stage: str, detail: str = "") -> bool:
    """Edit a group status message to `stage`. Returns True when it changed.

    Takes chat_id/message_id rather than a PTB Message so it still works after
    a restart, when the Message object is gone but the ids survive in SQLite.

    Telegram raises BadRequest("message is not modified") when the new text is
    byte-identical to the old one. That is a normal outcome here — two stages
    can produce the same line — so it is swallowed rather than logged as an
    error. Every other failure is logged and reported back as False, because a
    status message that silently stops updating is worse than no status at all.
    """
    template = STAGE_TEXT.get(stage)
    if template is None:
        log.warning("unknown stage %r", stage)
        return False

    body = template.format(detail=detail) if "{detail}" in template else template
    text = f"{body}\n\nYangilandi: {datetime.now().strftime('%H:%M')}"

    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        return True
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return False
        log.warning("stage %s edit failed (%s→%s): %s", stage, chat_id, message_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("stage %s edit failed (%s→%s): %s", stage, chat_id, message_id, exc)
        return False


async def set_issue_stage(bot, issue, stage: str, detail: str = "") -> bool:
    """Advance the group status message belonging to `issue`.

    No-op when the issue never had a group message (DM-originated reports), so
    callers do not each have to guard for it.
    """
    chat_id = getattr(issue, "group_id", None)
    message_id = getattr(issue, "ack_message_id", None)
    if not chat_id or not message_id:
        return False
    return await set_stage(bot, chat_id, message_id, stage, detail)

CATEGORY_LABEL = {
    "backend_bug":  "Backend (bu repo)",
    "frontend_bug": "Frontend",
    "infra_issue":  "Infra / server",
    "user_error":   "Foydalanuvchi xatosi",
    "unclear":      "Aniq emas",
}


async def _download_image(ctx: ContextTypes.DEFAULT_TYPE, msg: Message) -> Path | None:
    """Download a photo (or image document) attached to `msg` to IMAGE_TMP_DIR.

    Returns the absolute path, or None if the message has no image-like attachment.
    The caller is responsible for deleting the file after analysis.
    """
    file_id: str | None = None
    suffix = ".jpg"
    if msg.photo:
        file_id = msg.photo[-1].file_id  # largest resolution
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
        # Preserve a sensible suffix for Claude's image reader (png/jpg/webp...).
        mime = msg.document.mime_type or "image/jpeg"
        suffix = "." + mime.split("/", 1)[1].split(";")[0].strip() or ".jpg"
    if not file_id:
        return None

    tmp_dir = _image_tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{msg.chat.id}_{msg.message_id}{suffix}"
    try:
        tg_file = await ctx.bot.get_file(file_id)
        await tg_file.download_to_drive(path)
        log.info("downloaded image to %s", path)
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("image download failed: %s", exc)
        return None


def _cleanup_image(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log.debug("could not remove %s", path)


def _publish_keyboard(issue_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Prodga chiqarish", callback_data=f"pub:{issue_id}"),
        InlineKeyboardButton("Kutish",           callback_data=f"hld:{issue_id}"),
    ]])


def _format_diagnosis(issue: Issue) -> str:
    d = issue.diagnosis
    files = d.get("files_to_change") or {}
    files_list = "\n".join(f"  - {p}" for p in files.keys()) or "  (yo'q)"
    msg = issue.message if len(issue.message) < 400 else issue.message[:400] + "..."
    cat = d.get("category") or "unclear"
    cat_label = CATEGORY_LABEL.get(cat, cat)
    routing = ""
    if issue.project_id or issue.repo_role:
        proj = issue.project_id or "?"
        role = issue.repo_role or "?"
        routing = f"Loyiha: {proj} / {role}\n"
    return (
        f"Muammo {issue.id} — guruh: {issue.group_title}\n"
        f"{routing}"
        f"Kategoriya: {cat_label}\n\n"
        f"Xodim yozdi: {msg}\n\n"
        f"Xulosa: {d.get('summary', '-')}\n"
        f"Texnik: {d.get('technical_summary', '-')}\n"
        f"Ishonch: {d.get('confidence', '-')}  Vaqt: {d.get('eta_minutes', '?')}m\n\n"
        f"O'zgaradigan fayllar:\n{files_list}"
    )


async def _send_diagnosis_dm(ctx: ContextTypes.DEFAULT_TYPE, issue_id: str):
    """Send the diagnosis DM. Keyboard depends on category.

    - backend_bug → Accept / Retry / Skip (can apply a fix)
    - anything else → Retry / Skip only (no fix to apply)
    """
    issue = ISSUES[issue_id]
    cat = (issue.diagnosis.get("category") or "unclear")
    kb = _diagnosis_keyboard(issue_id) if cat == "backend_bug" else _retry_only_keyboard(issue_id)
    await _dm_all_devs(ctx, _format_diagnosis(issue), reply_markup=kb)


# ---------- handlers ----------

async def _process_task(
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    source_chat_id: int,
    source_chat_title: str,
    source_message_id: int,
    ack_message,
    image_paths: list[Path] | None = None,
    project_id: str | None = None,
    repo_role: str | None = None,
):
    """Run analyze() on `text` (+ optional image paths) and store an Issue.

    Shared by group + DM entry points. `ack_message` is the bot's "AI tahlil
    qilmoqda..." (group) or "Xabar tahlil qilinmoqda..." (DM) message that we
    edit in place with the final category-specific verdict once analysis
    completes.

    Routing: if (project_id, repo_role) resolve to a known repo in the DB,
    Stage 2 runs in that repo's `repo_path` (CLAUDE.md picked up from there).
    Falls back to config.REPO_PATH for backwards compat.

    After analysis, attached images are deleted.
    """
    image_strs = [str(p) for p in (image_paths or [])]
    repo_row = None
    repo_path_override: str | None = None
    if project_id and repo_role:
        repo_row = db.get_repo(project_id, repo_role)
        if repo_row and repo_row.get("repo_path"):
            repo_path_override = repo_row["repo_path"]
            log.info(
                "stage2 routing → project=%s repo=%s cwd=%s",
                project_id, repo_role, repo_path_override,
            )
        else:
            log.warning(
                "classifier suggested project=%s repo=%s but no matching repo "
                "row in DB; falling back to default REPO_PATH",
                project_id, repo_role,
            )
    try:
        try:
            diagnosis = await run_capped(
                analyze, text, source_chat_title, image_strs or None,
                repo_path_override,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("analyze failed")
            # Plain language in the group; the exception text goes to the log,
            # not to the staff member who just reported a broken screen.
            await set_stage(ctx.bot, source_chat_id, ack_message.message_id, "failed")
            return

        if not diagnosis:
            await ack_message.edit_text(
                "Tahlil natija qaytarmadi — iltimos, aniqroq tushuntirib yoki screenshot qo'shib qayta yuboring."
            )
            return

        issue_id = uuid.uuid4().hex[:8]
        new_issue = Issue(
            id=issue_id,
            group_id=source_chat_id,
            group_title=source_chat_title,
            message=text,
            user_message_id=source_message_id,
            diagnosis=diagnosis,
            project_id=project_id,
            repo_role=repo_role,
            ack_message_id=getattr(ack_message, "message_id", None),
        )
        ISSUES[issue_id] = new_issue
        _persist_issue(new_issue)

        category = diagnosis.get("category") or "unclear"
        summary = diagnosis.get("summary") or ""
        eta = diagnosis.get("eta_minutes") or "?"

        # backend_bug is the only category that continues into the fix pipeline,
        # so it is the only one that gets a "still moving" stage. Everything else
        # is terminal for the group: say what it is, in plain language, once.
        if category == "backend_bug":
            await set_issue_stage(ctx.bot, new_issue, "diagnosed", summary or "aniqlanmoqda")
        else:
            template = CATEGORY_GROUP_REPLY.get(category, CATEGORY_GROUP_REPLY["unclear"])
            group_reply = template.format(id=issue_id, eta=eta, summary=summary)
            await ack_message.edit_text(group_reply)

        # Always DM the developer with full diagnosis + appropriate keyboard.
        try:
            await _send_diagnosis_dm(ctx, issue_id)
            log.info("%s: DM sent to developer with %s keyboard", issue_id, category)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: failed to DM developer — %s", issue_id, exc)

        if category != "backend_bug":
            log.info(
                "%s classified as %s — no fix pipeline will run",
                issue_id, category,
            )
    finally:
        for p in image_paths or []:
            _cleanup_image(p)


async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    if msg.chat.id not in config.MONITORED_GROUP_IDS:
        return
    if bot_state.paused:
        log.info("paused: ignoring group message")
        return

    # Accept text, photo-with-caption, photo-alone, or image documents.
    text = (msg.text or msg.caption or "").strip()
    has_image = bool(msg.photo) or (
        msg.document and (msg.document.mime_type or "").startswith("image/")
    )
    if not text and not has_image:
        return

    # === STAGE 1 — Claude 3-way classifier ===
    # OUR_PROBLEM | OUT_OF_SCOPE | CHAT. Runs in the bot dir (no CLAUDE.md, no
    # repo search). Only OUR_PROBLEM proceeds to Stage 2 AND gets the
    # "AI tahlil qilmoqda" ack in the group. OUT_OF_SCOPE and CHAT are dropped
    # silently. The image is downloaded first so the classifier has the
    # screenshot signal.
    image_path = await _download_image(ctx, msg) if has_image else None
    # Classifier reads project/repo scope from the DB based on the chat_id
    # so it knows which projects this group monitors and which repos belong
    # to each. Returns 4-tuple (is_problem, project_id, repo_role, reason).
    is_problem, classified_project, classified_repo, reason = await run_capped(
        classify_via_claude,
        text,
        [str(image_path)] if image_path else None,
        msg.chat.id,
    )
    if not is_problem:
        _cleanup_image(image_path)
        # If this CHAT-classified message is a reply *into* an ongoing
        # analysis thread AND signals resolution, supersede our pending
        # "AI tahlil qilmoqda..." ack — the staff handled it themselves.
        reply_to_id = msg.reply_to_message.message_id if msg.reply_to_message else None
        related = _find_related_ack(reply_to_id)
        if related and _looks_resolved(text):
            log.info(
                "ack %d superseded by human resolution: %s",
                related.ack_message_id, text[:80],
            )
            await _supersede_ack(
                ctx, related,
                "✅ Xodim javob berdi — AI tahlili to'xtatildi.",
            )
        log.info(
            "stage1 → skip (reason=%s) in %s: %s",
            reason, msg.chat.title, text[:80] if text else "(no caption)",
        )
        return

    # === STAGE 2 — solution finder ===
    # Stage 1 said this is our project's problem. Post the "AI tahlil
    # qilmoqda" ack as a reply to staff so they know the bot is on it, then
    # run the full analyse flow. _process_task will edit the ack to the
    # final verdict (backend_bug → "Tekshirilmoqda...", unclear → "Qo'shimcha
    # ma'lumot kerak...", etc). The ack is registered in ONGOING_ACKS so a
    # subsequent "boldi togrlandi" follow-up can supersede it.
    group_title = msg.chat.title or str(msg.chat.id)
    log.info(
        "stage1 → our_problem (project=%s repo=%s reason=%s); stage2 analysing msg in %s (image=%s): %s",
        classified_project, classified_repo, reason,
        group_title, has_image, text[:80] if text else "(no caption)",
    )

    # This one message is the whole group-facing lifecycle: it is edited
    # through every later stage instead of new posts being added. See
    # STAGE_TEXT / set_stage().
    ack = await msg.reply_text(STAGE_TEXT["received"])
    ongoing = OngoingAck(
        chat_id=msg.chat.id,
        ack_message_id=ack.message_id,
        original_msg_id=msg.message_id,
        ack_message=ack,
    )
    ONGOING_ACKS[ack.message_id] = ongoing
    db.save_ongoing_ack(ack.message_id, msg.chat.id, msg.message_id)

    effective_text = text or "(Screenshot yuborildi — matn yo'q)"
    # Wrap as a Task so a thread-resolution can cancel it cleanly.
    task = asyncio.create_task(_process_task(
        ctx, effective_text, msg.chat.id, group_title, msg.message_id, ack,
        image_paths=[image_path] if image_path else None,
        project_id=classified_project, repo_role=classified_repo,
    ))
    ongoing.task = task
    try:
        await task
    except asyncio.CancelledError:
        log.info("group analysis cancelled — superseded by thread resolution")
    finally:
        # Clear from both caches; analysis-complete or cancelled, doesn't matter.
        ONGOING_ACKS.pop(ack.message_id, None)
        db.remove_ongoing_ack(ack.message_id)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    # q.answer() dismisses the Telegram loading spinner but raises if the
    # callback is older than ~15 min OR if the bot was restarted after the
    # button was sent. Either way, the user genuinely tapped something — we
    # should still honour the action, so just log and continue instead of
    # crashing the handler.
    try:
        await q.answer()
    except Exception as exc:  # noqa: BLE001
        log.warning("callback q.answer() failed (likely expired/stale): %s", exc)

    if not config.is_developer(q.from_user.id):
        try:
            await q.edit_message_text("Ruxsat yo'q.")
        except Exception as exc:  # noqa: BLE001
            log.warning("edit_message_text failed on unauthorised tap: %s", exc)
        return

    action, _, payload = (q.data or "").partition(":")

    # Chat-mode callbacks (not tied to an Issue).
    if action == "chat_switch":
        switched = chat_sessions.switch_chat(q.from_user.id, payload)
        if switched:
            await q.edit_message_text(f"Aktiv chat: {payload}")
        else:
            await q.edit_message_text(f"Chat '{payload}' topilmadi.")
        return

    if action == "chat_delete":
        ok = chat_sessions.delete_chat(q.from_user.id, payload)
        if ok:
            await q.edit_message_text(f"Chat '{payload}' o'chirildi.")
        else:
            await q.edit_message_text(f"Chat '{payload}' topilmadi.")
        return

    if action == "task_ok":
        stashed = db.pop_pending_task(payload)
        if not stashed:
            try:
                await q.edit_message_text("Vaqt tugagan — qayta yozib yuboring.")
            except Exception:  # noqa: BLE001
                pass
            return
        text, image_str = stashed
        image_paths = [Path(image_str)] if image_str else None
        await q.edit_message_text("Task sifatida tahlil qilinmoqda...")
        # Per-tap ack — only the dev who confirmed the TASK sees the in-progress
        # message; the eventual diagnosis card still fans out to all devs.
        ack = await ctx.bot.send_message(
            q.from_user.id,
            "Xabar tahlil qilinmoqda...",
        )
        await _process_task(
            ctx, text or "(screenshot)", q.message.chat.id, "Shaxsiy xabar",
            q.message.message_id, ack,
            image_paths=image_paths,
        )
        return

    if action == "task_no":
        stashed = db.pop_pending_task(payload)
        if not stashed:
            try:
                await q.edit_message_text("Vaqt tugagan — qayta yozib yuboring.")
            except Exception:  # noqa: BLE001
                pass
            return
        text, image_str = stashed
        image_paths = [Path(image_str)] if image_str else None
        await q.edit_message_text("OK, chat sifatida davom etaman.")
        session = await _ensure_active_chat(q.from_user.id, text or "chat")
        task = asyncio.create_task(
            _run_chat_turn(q.from_user.id, session.name, text, image_paths, q.message)
        )
        chat_sessions.set_current_task(q.from_user.id, session.name, task)
        return

    # Navigation buttons from /status dashboard.
    if action == "nav":
        if payload == "chatlist":
            await cmd_chatlist(update, ctx)
        elif payload == "status":
            await cmd_status(update, ctx)
        elif payload == "toggle_dry":
            # Flip DRY_RUN at runtime (writes to .env via env_editor).
            try:
                import env_editor
                new_val = "false" if config.DRY_RUN else "true"
                env_editor.save_env(config.ENV_FILE, {"DRY_RUN": new_val})
                config.reload()
                await q.edit_message_text(
                    f"DRY_RUN → {new_val}. Stop/Start tavsiya qilinadi (yoki /status bilan yangilang)."
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("toggle_dry failed")
                await q.edit_message_text(f"DRY_RUN o'zgartirib bo'lmadi: {exc}")
        return

    # /push chooser: user tapped stage / dev / prod before confirmation.
    if action == "push_ask":
        if payload not in _BRANCH_ALIAS:
            await q.edit_message_text("Noto'g'ri branch tanlovi.")
            return
        target = _BRANCH_ALIAS[payload]()
        mode_note = "(DRY_RUN — push skipped)" if config.DRY_RUN else "(LIVE — real push)"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Ha, push qil", callback_data=f"push_ok:{payload}"),
            InlineKeyboardButton("Bekor", callback_data="push_cancel:_"),
        ]])
        await q.edit_message_text(
            f"{config.STAGE_BRANCH} → {target} {mode_note}\n\nTasdiqlaysizmi?",
            reply_markup=kb,
        )
        return

    if action == "push_cancel":
        await q.edit_message_text("Push bekor qilindi.")
        return

    if action == "push_ok":
        alias = payload
        if alias not in _BRANCH_ALIAS:
            await q.edit_message_text("Noto'g'ri branch.")
            return
        target = _BRANCH_ALIAS[alias]()
        await q.edit_message_text(f"{config.STAGE_BRANCH} → {target} bajarilmoqda...")
        try:
            ok, message = await asyncio.to_thread(git_ops.push_to_branch, target)
        except Exception as exc:  # noqa: BLE001
            log.exception("push_to_branch failed")
            await _dm_all_devs(ctx, f"/push xato: {exc}")
            return
        icon = "✅" if ok else "❌"
        await _dm_all_devs(ctx, f"{icon} {message}")
        return

    if action == "roll_cancel":
        await q.edit_message_text("Rollback bekor qilindi.")
        return

    if action == "roll_ok":
        issue_id_to_revert = payload
        await _safe_md_send(
            q.edit_message_text,
            f"`{issue_id_to_revert}` revert qilinmoqda...",
        )
        try:
            ok, message = await asyncio.to_thread(
                git_ops.rollback_fix, issue_id_to_revert,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("rollback_fix failed")
            await _dm_all_devs(ctx, f"/rollback xato: {exc}")
            return
        icon = "✅" if ok else "❌"
        await _dm_all_devs(ctx, f"{icon} {message}")
        return

    # Existing Issue-bound callbacks fall through to the old logic.
    issue_id = payload
    issue = ISSUES.get(issue_id)
    if not issue:
        try:
            await q.edit_message_text("Muammo topilmadi (bot qayta ishga tushgan bo'lishi mumkin).")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not notify stale-issue tap: %s", exc)
        return

    if action == "skp":
        _drop_issue(issue_id)
        await q.edit_message_text(f"{issue_id} o'tkazib yuborildi.")
        return

    if action == "rty":
        issue.awaiting_retry_prompt = True
        issue.retry_initiator = q.from_user.id
        _persist_issue(issue)
        await q.edit_message_text(
            f"{issue_id} uchun qo'shimcha ko'rsatmani keyingi shaxsiy xabarda yuboring."
        )
        return

    if action == "acc":
        await q.edit_message_text(f"{issue_id} uchun tuzatma qo'llanmoqda...")
        await _apply_issue(ctx, issue)
        return

    if action == "pub":
        await q.edit_message_text(
            f"{issue_id} {config.PROD_BRANCH} shoxobchasiga chiqarilmoqda..."
        )
        await _publish_issue(ctx, issue)
        return

    if action == "hld":
        await q.edit_message_text(
            f"{issue_id} kutilmoqda. Tayyor bo'lganda qo'lda birlashtiring."
        )
        return


# ---------- reusable Accept / Publish / Skip logic ----------

async def _apply_issue(ctx: ContextTypes.DEFAULT_TYPE, issue: Issue) -> None:
    """Run apply_fix + create_and_merge_pr for `issue`. DMs status to dev."""
    # Multi-dev safety: if dev A already accepted this issue, dev B's tap
    # would otherwise re-run apply_fix and explode on `git checkout -b
    # fix/bot-<id>` (branch exists). Refuse politely instead.
    if issue.branch is not None:
        await _dm_all_devs(
            ctx,
            f"{issue.id} avval qo'llangan ({issue.branch}). "
            + ("stage'ga merge qilingan." if issue.merged_to_stage else "PR ochiq."),
        )
        return
    try:
        files = issue.diagnosis.get("files_to_change") or {}
        if not files:
            _drop_issue(issue.id)
            await _dm_all_devs(
                ctx,
                f"{issue.id} qo'llab bo'lmadi: tashxisda files_to_change yo'q.",
            )
            return
        branch = await asyncio.to_thread(
            git_ops.apply_fix,
            issue.id, files, issue.diagnosis.get("summary", ""),
        )
        issue.branch = branch
        _persist_issue(issue)
        pr_url, merged = await asyncio.to_thread(
            github_ops.create_and_merge_pr,
            branch,
            f"fix: bot issue {issue.id}",
            issue.diagnosis.get("technical_summary", ""),
        )
        issue.pr_url = pr_url
        issue.merged_to_stage = merged
        _persist_issue(issue)

        # Group sees plain language, no branch names or PR links: merged means
        # it is on its way to being tested, not-merged means a human has to
        # look before anything moves.
        await set_issue_stage(ctx.bot, issue, "testing" if merged else "awaiting")

        status = "stage'ga birlashtirildi" if merged else "PR ochildi (avto-merge bajarilmadi)"
        await _dm_all_devs(
            ctx,
            f"{issue.id}: {status}\n{pr_url}",
            reply_markup=_publish_keyboard(issue.id) if merged else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("apply_fix failed")
        await _dm_all_devs(ctx, f"{issue.id} qo'llanmadi: {exc}")


async def _publish_issue(ctx: ContextTypes.DEFAULT_TYPE, issue: Issue) -> None:
    """Merge stage → PROD_BRANCH + push + notify the original group."""
    try:
        ok = await asyncio.to_thread(git_ops.merge_to_prod)
    except Exception as exc:  # noqa: BLE001
        log.exception("publish failed")
        await _dm_all_devs(ctx, f"{issue.id} chiqarish bajarilmadi: {exc}")
        return

    if ok:
        # Was a NEW message in the group; now it edits the one status message
        # the report already owns. Same information, one post instead of two,
        # and it stays attached to the original complaint.
        edited = await set_issue_stage(ctx.bot, issue, "released")
        if not edited:
            # No status message to edit (DM-originated, or the edit failed).
            # Falling back to a post is right here: "it is fixed" is the one
            # thing the reporter must not miss.
            try:
                await ctx.bot.send_message(
                    issue.group_id,
                    "✅ Tuzatildi va ishga tushdi. Xabaringiz uchun rahmat!",
                    reply_to_message_id=issue.user_message_id,
                )
            except Exception:  # noqa: BLE001
                log.exception("could not notify group %s", issue.group_id)
        await _dm_all_devs(
            ctx,
            f"{issue.id} {config.PROD_BRANCH} shoxobchasiga chiqarildi.",
        )
        _drop_issue(issue.id)
    else:
        await _dm_all_devs(
            ctx,
            f"{issue.id}: {config.PROD_BRANCH} shoxobchasiga birlashtirish bajarilmadi; loglarni tekshiring.",
        )


# =============================================================================
# DM chat mode — multi-session, parallel, intent-routed.
# =============================================================================

# Pending TASK confirmations are persisted in `pending_tasks` SQLite table —
# survive restart so a "Ha, task" tap an hour after the bot was restarted
# still works. Access via db.save_pending_task / db.pop_pending_task.
# Token = sha1(text + image_path) keeps callback_data under Telegram's 64-byte cap.


def _auto_chat_name(text: str) -> str:
    """Deterministic short name for an auto-created chat session."""
    t = (text or "").strip().replace("\n", " ")
    if not t:
        return "chat"
    short = t[:28].strip()
    return short or "chat"


# =============================================================================
# Live ack tracking — supersede in-flight "AI tahlil qilmoqda..." messages
# when the staff thread resolves the issue manually before our analysis ends.
# =============================================================================

@dataclass
class OngoingAck:
    chat_id: int
    ack_message_id: int        # used for ctx.bot.edit_message_text after restart
    original_msg_id: int       # the staff complaint that triggered analysis
    ack_message: object | None = None  # PTB Message — None if we hydrated from DB
    started_at: datetime = field(default_factory=datetime.now)
    task: asyncio.Task | None = None   # in-memory only; lost on restart


# In-memory cache for current run — needed because asyncio.Task can't persist.
# Persistent fields (chat_id, ack_message_id, original_msg_id) are mirrored to
# SQLite via db.save_ongoing_ack so that even after a restart we can detect a
# "boldi togrlandi" follow-up and edit the ack message.
ONGOING_ACKS: dict[int, OngoingAck] = {}

# Resolution-language hints — if a CHAT-classified message in the same thread
# contains any of these, we treat it as the staff resolving the complaint
# manually and supersede our pending analysis.
_RESOLUTION_HINTS = {
    # Uzbek Latin
    "boldi", "bo'ldi", "to'g'rlandi", "togrlandi", "tugadi", "tugatildi",
    "tuzatildi", "tuzaldi", "hal qilindi", "hal bo'ldi", "ishladi",
    "ishlayapti", "ko'rdim", "tayyor", "rahmat", "rahmat tuzatildi",
    # Russian
    "готово", "сделано", "решено", "исправлено", "работает", "спасибо",
    # English / mixed
    "fixed", "done", "resolved", "thanks", "thank you", "ok thanks",
}


def _looks_resolved(text: str) -> bool:
    """Loose substring check for resolution language."""
    if not text:
        return False
    lowered = text.lower().strip()
    return any(h in lowered for h in _RESOLUTION_HINTS)


def _find_related_ack(reply_to_msg_id: int | None) -> OngoingAck | None:
    """Locate an ongoing ack the incoming message is replying into.

    Checks the in-memory cache first (has the asyncio.Task we'd want to
    cancel), then falls back to a SQLite lookup so post-restart acks
    can still be superseded — we just won't have a Task to cancel.
    """
    if reply_to_msg_id is None:
        return None
    # Direct: replied to our ack message.
    direct = ONGOING_ACKS.get(reply_to_msg_id)
    if direct:
        return direct
    # Indirect: replied to the original complaint.
    in_mem = next(
        (a for a in ONGOING_ACKS.values() if a.original_msg_id == reply_to_msg_id),
        None,
    )
    if in_mem:
        return in_mem
    # Restart fallback: query SQLite.
    row = db.find_ack_by_reply(reply_to_msg_id)
    if row:
        return OngoingAck(
            chat_id=row["chat_id"],
            ack_message_id=row["ack_message_id"],
            original_msg_id=row["original_msg_id"],
            ack_message=None,
        )
    return None


async def _supersede_ack(ctx, ack: OngoingAck, new_text: str) -> None:
    """Cancel the in-flight analysis (if any) and edit the bot's ack message.

    Works for both in-process acks (we have the PTB Message) and post-restart
    acks (we only have chat_id + message_id).
    """
    if ack.task and not ack.task.done():
        ack.task.cancel()
    try:
        if ack.ack_message is not None:
            await ack.ack_message.edit_text(new_text)
        else:
            # Hydrated from DB — no Message object, edit by id.
            await ctx.bot.edit_message_text(
                text=new_text,
                chat_id=ack.chat_id,
                message_id=ack.ack_message_id,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not edit superseded ack: %s", exc)
    ONGOING_ACKS.pop(ack.ack_message_id, None)
    db.remove_ongoing_ack(ack.ack_message_id)


async def _ensure_active_chat(user_id: int, seed_text: str) -> chat_sessions.ChatSession:
    """Return the active chat, creating a default one if none exists."""
    active = chat_sessions.active_session(user_id)
    if active is not None:
        return active
    name = _auto_chat_name(seed_text)
    # Disambiguate if this name already exists for some other reason.
    base = name
    i = 2
    uc = chat_sessions.get_user_chats(user_id)
    while name in uc.sessions:
        name = f"{base}-{i}"
        i += 1
    return chat_sessions.create_chat(user_id, name)


async def _run_chat_turn(
    user_id: int,
    session_name: str,
    text: str,
    image_paths: list[Path] | None,
    reply_to_msg,
) -> None:
    """Core chat-turn executor. Runs the Claude call, replies to the dev.

    This is wrapped in an asyncio.Task (stored on the session) so it can run
    in parallel with other chats' tasks and be /stop'd individually.
    """
    session = chat_sessions.get_user_chats(user_id).sessions.get(session_name)
    if session is None:
        await reply_to_msg.reply_text(f"Chat '{session_name}' topilmadi.")
        return
    image_strs = [str(p) for p in (image_paths or [])]
    try:
        response, new_sid = await run_capped(
            chat_with_claude,
            session.session_id,
            text,
            image_strs or None,
        )
        chat_sessions.record_turn(user_id, session_name, new_sid)
        out = response or "(bo'sh javob)"
        # Plain brackets — Telegram's legacy Markdown only treats `[text](url)`
        # as a link, so a bare `[name]` renders as text.
        prefix = f"*[{session_name}]*\n"
        await _send_chat_reply(reply_to_msg, prefix + out)
    except asyncio.CancelledError:
        log.info("chat turn cancelled: user=%s session=%s", user_id, session_name)
        try:
            await reply_to_msg.reply_text(f"[{session_name}] to'xtatildi.")
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("chat turn failed: %s", exc)
        try:
            await reply_to_msg.reply_text(f"[{session_name}] xato: {exc}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        chat_sessions.clear_current_task(user_id, session_name)
        for p in image_paths or []:
            _cleanup_image(p)


def _chunk_text(s: str, size: int) -> list[str]:
    """Split a long string into pieces that fit Telegram messages.

    Tries to break on fenced-code-block boundaries and newlines so we don't
    cut a ``` block in half (which would break Telegram's Markdown parser).
    """
    if len(s) <= size:
        return [s]
    chunks: list[str] = []
    rest = s
    while len(rest) > size:
        # Prefer to split at a newline before `size`; fall back to hard cut.
        cut = rest.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


# Telegram's "Markdown" (legacy) parse mode supports `*bold*`, `_italic_`,
# `` `code` ``, ` ```code blocks``` `, `[text](url)`. It does NOT support
# CommonMark-style `## headings` or `**double-asterisk bold**` — both come
# through as raw chars. Claude's output regularly uses both. We translate.
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+)$")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


def _to_telegram_markdown(text: str) -> str:
    """Convert Claude's CommonMark output to Telegram legacy-Markdown.

    Rules (kept tight to avoid corrupting code):
      - Lines outside fenced code blocks: `## Foo` → `*Foo*`; `**foo**` → `*foo*`.
      - Lines INSIDE ``` ... ``` are preserved verbatim.
      - Nothing else is touched — code, links, lists pass through.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append(f"*{m.group(1).strip()}*")
            continue
        out.append(_BOLD_RE.sub(r"*\1*", line))
    return "\n".join(out)


async def _send_chat_reply(reply_to_msg, text: str) -> None:
    """Send a Claude chat response with Markdown formatting.

    Strategy: convert CommonMark → Telegram Markdown, chunk to 4000 chars,
    send each chunk with `parse_mode="Markdown"`. If Telegram rejects the
    parse (unbalanced * or ` etc.), retry that chunk as plain text so the
    user always gets the content even if formatting fails.
    """
    formatted = _to_telegram_markdown(text)
    for chunk in _chunk_text(formatted, 4000):
        try:
            await reply_to_msg.reply_text(chunk, parse_mode="Markdown")
        except Exception as exc:  # noqa: BLE001
            log.warning("markdown send failed (%s); retrying as plain text", exc)
            try:
                await reply_to_msg.reply_text(chunk)
            except Exception:  # noqa: BLE001
                log.exception("plain text send also failed")


def _pending_task_id(text: str) -> str:
    """Short stable hash used as callback_data for TASK-confirmation buttons."""
    import hashlib
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


async def on_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.from_user or not config.is_developer(msg.from_user.id):
        return
    if msg.text and msg.text.startswith("/"):
        return  # commands handled separately
    if bot_state.paused:
        await msg.reply_text("Bot pauzada; davom etish uchun ilovadan qayta yoqing.")
        return

    raw_text = (msg.text or msg.caption or "").strip()
    has_image = bool(msg.photo) or (
        msg.document and (msg.document.mime_type or "").startswith("image/")
    )
    if not raw_text and not has_image:
        return

    # Priority 1: a Retry (from Qayta button) is waiting for a follow-up.
    # Only the dev who tapped Qayta can supply the instruction — multi-dev
    # safe (other devs' DMs flow through the intent classifier as usual).
    pending = next(
        (
            i for i in ISSUES.values()
            if i.awaiting_retry_prompt and i.retry_initiator == msg.from_user.id
        ),
        None,
    )
    if pending:
        image_path = await _download_image(ctx, msg) if has_image else None
        image_paths = [image_path] if image_path else None
        pending.awaiting_retry_prompt = False
        pending.retry_initiator = None
        await msg.reply_text(
            f"{pending.id} sizning izohingiz bilan qayta tahlil qilinmoqda..."
        )
        try:
            try:
                new_diag = await run_capped(
                    analyze_with_instruction,
                    pending.message, pending.group_title, raw_text,
                    [str(p) for p in image_paths] if image_paths else None,
                )
            except Exception as exc:  # noqa: BLE001
                await msg.reply_text(f"Qayta urinish bajarilmadi: {exc}")
                return
            if not new_diag:
                await msg.reply_text("Claude bu safar tashxis qaytarmadi.")
                return
            pending.diagnosis = new_diag
            _persist_issue(pending)
            await _send_diagnosis_dm(ctx, pending.id)
        finally:
            for p in image_paths or []:
                _cleanup_image(p)
        return

    # Priority 2: prefix overrides the classifier.
    forced: str | None = None
    text = raw_text
    if text.startswith("!"):
        forced = "TASK"
        text = text[1:].lstrip()
    elif text.startswith("?"):
        forced = "ASK"
        text = text[1:].lstrip()

    # Priority 3: intent classifier (unless we already have a forced override).
    if forced:
        intent, reason = forced, "forced_by_prefix"
    else:
        intent, reason = await run_capped(classify_dm_intent, text, has_image)
    log.info("dm intent=%s reason=%s", intent, reason)

    image_path = await _download_image(ctx, msg) if has_image else None
    image_paths = [image_path] if image_path else None
    user_id = msg.from_user.id

    if intent == "TASK" and not forced:
        # Ask confirmation before firing the heavy pipeline. Keep the image
        # path stored — db.pop_pending_task returns it on tap (survives restart).
        token = _pending_task_id(text + (str(image_path) if image_path else ""))
        db.save_pending_task(
            token, user_id, text,
            str(image_path) if image_path else None,
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Ha, task", callback_data=f"task_ok:{token}"),
            InlineKeyboardButton("Yo'q, chat", callback_data=f"task_no:{token}"),
        ]])
        await msg.reply_text(
            "Bu vazifadek ko'rinyapti. Task sifatida navbatga olay?\n"
            f"(sabab: {reason})",
            reply_markup=kb,
        )
        return

    if intent == "TASK" and forced:
        effective_text = text or "(Screenshot yuborildi — matn yo'q)"
        ack = await msg.reply_text("Xabar tahlil qilinmoqda...")
        await _process_task(
            ctx, effective_text, msg.chat.id, "Shaxsiy xabar", msg.message_id, ack,
            image_paths=image_paths,
        )
        return

    if intent == "ASK":
        # One-shot read-only Q&A. Uses chat-mode with an ephemeral (non-resumed)
        # session — no entry added to chats.json. Cheapest path.
        log.info("dm ASK (reason=%s): %s", reason, text[:80])
        thinking = await msg.reply_text("Savol tahlil qilinmoqda...")
        try:
            response, _ = await run_capped(
                chat_with_claude,
                None,
                text,
                [str(p) for p in image_paths] if image_paths else None,
            )
            await thinking.delete()
            await _send_chat_reply(msg, response or "(bo'sh javob)")
        except Exception as exc:  # noqa: BLE001
            log.exception("ASK failed")
            try:
                await thinking.edit_text(f"Savolga javob berilmadi: {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            for p in image_paths or []:
                _cleanup_image(p)
        return

    # intent == "CHAT" (default).
    session = await _ensure_active_chat(user_id, text or "chat")
    log.info(
        "dm CHAT routing to '%s' (reason=%s): %s",
        session.name, reason, text[:80],
    )
    task = asyncio.create_task(
        _run_chat_turn(user_id, session.name, text, image_paths, msg)
    )
    chat_sessions.set_current_task(user_id, session.name, task)


# ---------- commands ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not config.is_developer(update.effective_user.id):
        return
    # Backticks around the mode label so the underscore in DRY_RUN is treated
    # as code rather than Markdown italic open.
    mode = "🧪 `DRY_RUN`" if config.DRY_RUN else "🚀 `LIVE`"
    await _safe_md_send(
        update.effective_message.reply_text,
        f"👋 Bot ishlamoqda · {mode}\n\n"
        "Pastdagi tugmalardan foydalaning yoki istalgan xabar yozing — bot avtomatik tushunadi.\n\n"
        "📖 To'liq yordam: */help*\n"
        "📊 Hozirgi holat: */status*",
        reply_markup=MAIN_MENU,
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Verify claude CLI is reachable from the bot's environment."""
    if not config.is_developer(update.effective_user.id):
        return
    await update.effective_message.reply_text("Claude tekshirilmoqda...")
    try:
        out = await run_capped(run_claude, "Reply with the single word: pong")
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Claude xato: {exc}")
        return
    reply_text = out[:400] if out else "(bo'sh)"
    await update.effective_message.reply_text(f"Claude javobi: {reply_text}")


async def cmd_whereami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quick sanity check: group chat id."""
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"chat_id={chat.id}\ntitle={chat.title}\ntype={chat.type}"
    )


# ---------- chat mode commands ----------

def _format_session_row(s: dict) -> str:
    star = "⭐" if s["active"] else "  "
    busy = "⏳" if s["busy"] else "💤"
    return f"{star} {busy} {s['name']} · {s['turn_count']} turn · {s['last_activity'][:16].replace('T', ' ')}"


async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/chat` shows active chat. `/chat <name>` creates or switches to it."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    args_text = " ".join(ctx.args or []).strip()
    if not args_text:
        active = chat_sessions.active_session(user_id)
        if active is None:
            await update.effective_message.reply_text(
                "Aktiv chat yo'q. `/chat <nom>` bilan yangi chat oching yoki `/chatlist` ko'ring."
            )
            return
        await update.effective_message.reply_text(
            f"Aktiv chat: {active.name} · {active.turn_count} turn · "
            f"oxirgi: {active.last_activity[:16].replace('T', ' ')}"
        )
        return

    name = args_text[:40]
    uc = chat_sessions.get_user_chats(user_id)
    if name in uc.sessions:
        chat_sessions.switch_chat(user_id, name)
        await update.effective_message.reply_text(f"Chat '{name}' aktiv qilindi.")
    else:
        chat_sessions.create_chat(user_id, name)
        await update.effective_message.reply_text(
            f"Yangi chat '{name}' yaratildi va aktiv qilindi. Endi xabaringizni yuboring."
        )


async def cmd_chatlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/chatlist` — show sessions as inline keyboard; tap to switch."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    rows = chat_sessions.export_for_list(user_id)
    if not rows:
        await update.effective_message.reply_text(
            "Chatlar yo'q. `/chat <nom>` bilan yangi chat oching."
        )
        return

    text_lines = ["Chatlar:"]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for s in rows:
        text_lines.append(_format_session_row(s))
        kb_rows.append([
            InlineKeyboardButton(
                f"{'⭐ ' if s['active'] else ''}{s['name']}",
                callback_data=f"chat_switch:{s['name']}",
            ),
            InlineKeyboardButton("🗑", callback_data=f"chat_delete:{s['name']}"),
        ])
    await update.effective_message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel the active chat's in-flight Claude call."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    active = chat_sessions.active_session(user_id)
    if active is None:
        await update.effective_message.reply_text("Aktiv chat yo'q.")
        return
    cancelled = chat_sessions.cancel_current_task(user_id, active.name)
    if cancelled:
        await update.effective_message.reply_text(
            f"[{active.name}] ishlayotgan chaqiruv to'xtatildi."
        )
    else:
        await update.effective_message.reply_text(
            f"[{active.name}] da hozir hech narsa ishlamayapti."
        )


async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/ask <question>` — one-shot read-only Q&A."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    question = " ".join(ctx.args or []).strip()
    if not question:
        await update.effective_message.reply_text(
            "Ishlatish: `/ask <savol>`. Misol: `/ask OrderService nima qiladi?`"
        )
        return
    thinking = await update.effective_message.reply_text("Savol tahlil qilinmoqda...")
    try:
        response, _ = await run_capped(chat_with_claude, None, question, None)
        await thinking.delete()
        await _send_chat_reply(update.effective_message, response or "(bo'sh javob)")
    except Exception as exc:  # noqa: BLE001
        log.exception("/ask failed")
        try:
            await thinking.edit_text(f"Javob berilmadi: {exc}")
        except Exception:  # noqa: BLE001
            pass


# ---------- Phase 2: /task /retry /status /menu ----------

async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/task <tavsif>` — explicitly trigger the diagnosis+fix pipeline."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    desc = " ".join(ctx.args or []).strip()
    if not desc:
        await update.effective_message.reply_text(
            "Ishlatish: `/task <tavsif>`. Misol: `/task OrderService'ga log warning qo'sh`"
        )
        return
    ack = await update.effective_message.reply_text("Xabar tahlil qilinmoqda...")
    await _process_task(
        ctx, desc, update.effective_chat.id, "Shaxsiy xabar",
        update.effective_message.message_id, ack,
        image_paths=None,
    )


def _latest_issue() -> Issue | None:
    if not ISSUES:
        return None
    return max(ISSUES.values(), key=lambda i: i.created_at)


async def cmd_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/retry [hint]` — re-run the most recent diagnosis, optionally with a hint.

    No hint → sets awaiting_retry_prompt on the latest issue (same as tapping
    the Qayta button). With hint → runs immediately with that hint.
    """
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    latest = _latest_issue()
    if latest is None:
        await update.effective_message.reply_text("Oldingi tashxis topilmadi.")
        return

    hint = " ".join(ctx.args or []).strip()
    if not hint:
        latest.awaiting_retry_prompt = True
        latest.retry_initiator = update.effective_user.id
        _persist_issue(latest)
        await update.effective_message.reply_text(
            f"{latest.id} uchun qo'shimcha ko'rsatmani keyingi xabarda yuboring."
        )
        return

    await update.effective_message.reply_text(
        f"{latest.id} sizning izohingiz bilan qayta tahlil qilinmoqda..."
    )
    try:
        new_diag = await run_capped(
            analyze_with_instruction,
            latest.message, latest.group_title, hint, None,
        )
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Qayta urinish bajarilmadi: {exc}")
        return
    if not new_diag:
        await update.effective_message.reply_text("Claude bu safar tashxis qaytarmadi.")
        return
    latest.diagnosis = new_diag
    _persist_issue(latest)
    await _send_diagnosis_dm(ctx, latest.id)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/status` — quick dashboard: mode, chats, open issues, monitored groups."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    uc = chat_sessions.get_user_chats(user_id)
    active = uc.active or "(yo'q)"
    total_chats = len(uc.sessions)
    busy_chats = sum(
        1 for s in uc.sessions.values()
        if s.current_task and not s.current_task.done()
    )
    open_issues = sorted(ISSUES.values(), key=lambda i: i.created_at, reverse=True)

    # Mode text wraps the literal in backticks so the inner underscore is
    # treated as code, not Markdown italic. Without this, "DRY_RUN" makes
    # Telegram throw "can't find end of the entity" (legacy Markdown parses
    # `_..._` as italic and we never close it).
    mode = "🧪 `DRY_RUN`" if config.DRY_RUN else "🚀 `LIVE`"
    lines = [
        "📊 *Holat*",
        "",
        f"Rejim: {mode}",
        f"Aktiv chat: {active}",
        f"Chatlar: {total_chats} (band: {busy_chats})",
        f"Ochiq muammolar: {len(open_issues)}",
    ]
    if open_issues:
        lines.append("")
        lines.append("So'nggi muammolar:")
        for issue in open_issues[:5]:
            cat = issue.diagnosis.get("category", "?")
            title = (issue.group_title or "?")[:28]
            lines.append(f"  • `{issue.id}` · {cat} · {title}")
    lines.append("")
    lines.append(f"REPO\\_PATH: `{config.REPO_PATH}`")
    lines.append(f"Branches: stage=`{config.STAGE_BRANCH}` prod=`{config.PROD_BRANCH}`")
    lines.append(f"Groups: {config.MONITORED_GROUP_IDS}")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Chatlar", callback_data="nav:chatlist"),
            InlineKeyboardButton("🔄 Yangilash", callback_data="nav:status"),
        ],
        [
            InlineKeyboardButton(
                "🧪 DRY_RUN → 🚀 LIVE" if config.DRY_RUN else "🚀 LIVE → 🧪 DRY_RUN",
                callback_data="nav:toggle_dry",
            ),
        ],
    ])
    await _safe_md_send(
        update.effective_message.reply_text,
        "\n".join(lines),
        reply_markup=kb,
    )


# Persistent reply keyboard with Uzbek + emoji labels.
#
# Telegram's CommandHandler only fires when the message BEGINS with "/", so we
# can't put emojis in front of `/command`. Workaround: button labels are pure
# Uzbek (no slash), and a dedicated MessageHandler (`on_menu_button`) catches
# them and routes to the right command function via BUTTON_DISPATCH.
#
# Buttons carry no per-issue payload, so action buttons like ✅ Qabul implicitly
# operate on the LATEST qualifying issue. For surgical selection of an older
# issue_id, use the inline buttons on the diagnosis DM card.
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Holat"),     KeyboardButton("💬 Chatlar"),   KeyboardButton("📖 Yordam")],
        [KeyboardButton("✅ Qabul"),     KeyboardButton("🔄 Qayta"),     KeyboardButton("⏭ O'tkazib")],
        [KeyboardButton("🚀 Stage"),     KeyboardButton("🚀 Dev"),       KeyboardButton("🚀 Prod")],
        [KeyboardButton("📤 Chiqarish"), KeyboardButton("↩️ Qaytarish"), KeyboardButton("⏹ To'xtatish")],
        [KeyboardButton("⏸ Hammasini"),  KeyboardButton("⚙ Menyu"),      KeyboardButton("🩺 Ping")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Xabar, savol yoki tugma...",
)


# Maps reply-keyboard button labels → the command line they should execute.
# Anything not in this dict falls through to on_dm (free-text → intent classifier).
BUTTON_DISPATCH: dict[str, str] = {
    "📊 Holat":      "/status",
    "💬 Chatlar":    "/chatlist",
    "📖 Yordam":     "/help",
    "✅ Qabul":      "/accept",
    "🔄 Qayta":      "/retry",
    "⏭ O'tkazib":   "/skip",
    "🚀 Stage":      "/push stage",
    "🚀 Dev":        "/push dev",
    "🚀 Prod":       "/push prod",
    "📤 Chiqarish":  "/publish",
    "↩️ Qaytarish":  "/rollback",
    "⏹ To'xtatish": "/stop",
    "⏸ Hammasini":   "/stopall",
    "⚙ Menyu":       "/menu",
    "🩺 Ping":       "/ping",
}


async def on_menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route reply-keyboard taps (Uzbek labels) to the matching command handler.

    Registered BEFORE on_dm so button taps don't trigger the intent classifier.
    """
    msg = update.effective_message
    if not msg or not msg.text or not msg.from_user:
        return
    if not config.is_developer(msg.from_user.id):
        return
    cmd_line = BUTTON_DISPATCH.get(msg.text.strip())
    if not cmd_line:
        return  # not a button — defer to on_dm

    cmd_name, _, args_str = cmd_line.lstrip("/").partition(" ")
    handler = {
        "status":    cmd_status,
        "chatlist":  cmd_chatlist,
        "help":      cmd_help,
        "accept":    cmd_accept,
        "retry":     cmd_retry,
        "skip":      cmd_skip,
        "push":      cmd_push,
        "publish":   cmd_publish,
        "rollback":  cmd_rollback,
        "stop":      cmd_stop,
        "stopall":   cmd_stopall,
        "menu":      cmd_menu,
        "ping":      cmd_ping,
    }.get(cmd_name)
    if handler is None:
        log.warning("BUTTON_DISPATCH points to unknown command: %s", cmd_line)
        return
    ctx.args = args_str.split() if args_str else []
    log.info("menu button tapped: %r → %s", msg.text, cmd_line)
    await handler(update, ctx)


def _menu_button_filter():
    """Filter that matches only exact reply-keyboard button labels."""
    pattern = "^(" + "|".join(re.escape(k) for k in BUTTON_DISPATCH) + ")$"
    return filters.TEXT & filters.Regex(pattern)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/menu` — show the persistent command keyboard."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    await update.effective_message.reply_text(
        "Boshqaruv paneli yoqildi. Buyruqlar tugmalar orqali ham ishlaydi.",
        reply_markup=MAIN_MENU,
    )


async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/version` — show local + remote commits and whether an update is ready."""
    if not config.is_developer(update.effective_user.id):
        return
    cur = updater.current_commit()
    branch = updater.current_branch() or "?"
    cur_short = (cur[:8] + "…") if cur else "(not a git clone)"

    thinking = await update.effective_message.reply_text("📦 Tekshirilmoqda...")
    try:
        remote = await asyncio.to_thread(updater.latest_remote_commit)
    except Exception as exc:  # noqa: BLE001
        log.exception("version check failed")
        await thinking.edit_text(f"Tekshirib bo'lmadi: {exc}")
        return

    lines = [
        "📦 *Bot versiya*",
        "",
        f"Mahalliy: `{cur_short}` (branch: `{branch}`)",
    ]
    if remote:
        rem_short = remote["sha"][:8] + "…"
        lines.append(f"GitHub:    `{rem_short}` — {remote['author']}")
        lines.append(f"_{remote['message']}_")
        if cur and remote["sha"] == cur:
            lines.append("\n✅ Eng so'nggi versiyada.")
        else:
            lines.append("\n🆙 Yangi versiya mavjud — `/update` bilan yangilang.")
    else:
        lines.append("GitHub:    (tekshirib bo'lmadi)")
    auto = "yoqilgan" if updater.auto_apply_enabled() else "o'chirilgan"
    interval_h = (updater.check_interval_seconds() // 3600) if updater.check_interval_seconds() else 0
    lines.append(
        f"\nAvto-yangilash: *{auto}* · tekshirish oraligi: "
        + (f"{interval_h} soat" if interval_h else "o'chirilgan")
    )
    await thinking.delete()
    await _safe_md_send(update.effective_message.reply_text, "\n".join(lines))


async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/update` — pull latest from GitHub and restart. Refuses if open issues."""
    if not config.is_developer(update.effective_user.id):
        return
    if not updater.is_git_clone():
        await update.effective_message.reply_text(
            "Bu bot katalogi `.git`'ga ega emas — auto-update faqat `git clone` qilingan o'rnatishlarda ishlaydi."
        )
        return

    # Safety: don't update mid-Accept-flow. Pending issues survive restart
    # (they're in SQLite), so the only blocker is an in-flight Claude call
    # we'd interrupt. We use ONGOING_ACKS as a proxy for "Stage 2 running".
    if ONGOING_ACKS:
        await update.effective_message.reply_text(
            f"⚠️ {len(ONGOING_ACKS)} ta tahlil hozir ishlamoqda — avval tugashini kuting, keyin `/update` qiling.",
        )
        return

    msg = await update.effective_message.reply_text("🔄 Yangilanmoqda...")
    try:
        ok, output = await asyncio.to_thread(updater.apply_update)
    except Exception as exc:  # noqa: BLE001
        log.exception("apply_update threw")
        await msg.edit_text(f"❌ Yangilash xato: {exc}")
        return

    if not ok:
        truncated = output[:600]
        await msg.edit_text(f"❌ Yangilash bajarilmadi:\n{truncated}")
        return

    truncated = (output or "").strip()[:600] or "(o'zgarish yo'q)"
    await msg.edit_text(
        f"✅ Yangilandi:\n{truncated}\n\n♻️ Qayta ishga tushirilmoqda...",
    )
    # Give Telegram a moment to deliver the edit before we kill ourselves.
    await asyncio.sleep(2)
    try:
        await _dm_all_devs(ctx, "♻️ Bot qayta ishga tushdi.")
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(1)
    updater.restart_bot()


async def cmd_autoupdate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/autoupdate [on|off|status|hours <N>|apply on|apply off]` — manage
    the periodic GitHub poll without touching the DB directly.

    Examples:
      /autoupdate              → show current state
      /autoupdate on           → poll every 6 h (default), notify only
      /autoupdate off          → disable periodic poll
      /autoupdate hours 12     → poll every 12 hours
      /autoupdate apply on     → also auto-pull + restart on new version
      /autoupdate apply off    → notify only (don't auto-apply)
    """
    if not config.is_developer(update.effective_user.id):
        return
    args = list(ctx.args or [])

    if not args or args[0].lower() == "status":
        h = updater.check_interval_seconds() // 3600 if updater.check_interval_seconds() else 0
        on = "yoqilgan" if h > 0 else "o'chirilgan"
        applying = "yoqilgan" if updater.auto_apply_enabled() else "o'chirilgan"
        await _safe_md_send(
            update.effective_message.reply_text,
            "♻️ *Auto-update holati*\n\n"
            f"Periodik tekshirish: *{on}*"
            + (f" (har `{h}` soatda)" if h else "") + "\n"
            f"Avto-yangilash: *{applying}*\n\n"
            "Sozlash:\n"
            "`/autoupdate on` — yoqish (har 6 soatda)\n"
            "`/autoupdate hours 12` — interval (soatda)\n"
            "`/autoupdate apply on` — avto-pull + qayta ishga tushish\n"
            "`/autoupdate off` — to'liq o'chirish",
        )
        return

    cmd = args[0].lower()
    if cmd == "on":
        db.set_setting("update_check_hours", "6", updated_by=update.effective_user.id)
        await update.effective_message.reply_text(
            "✅ Periodik tekshirish yoqildi (har 6 soatda)."
        )
        return
    if cmd == "off":
        db.set_setting("update_check_hours", "0", updated_by=update.effective_user.id)
        db.set_setting("update_auto_apply", "false", updated_by=update.effective_user.id)
        await update.effective_message.reply_text(
            "✅ Auto-update to'liq o'chirildi. /version va /update qo'lda ishlaydi."
        )
        return
    if cmd == "hours" and len(args) >= 2:
        try:
            h = max(0, int(args[1]))
        except ValueError:
            await update.effective_message.reply_text("Noto'g'ri qiymat. Misol: `/autoupdate hours 12`")
            return
        db.set_setting("update_check_hours", str(h), updated_by=update.effective_user.id)
        await update.effective_message.reply_text(
            f"✅ Tekshirish oraligi: {h} soat" + (" (o'chirilgan)" if h == 0 else "")
        )
        return
    if cmd == "apply" and len(args) >= 2:
        on = args[1].lower() in ("on", "true", "1", "yes")
        db.set_setting("update_auto_apply", "true" if on else "false", updated_by=update.effective_user.id)
        await update.effective_message.reply_text(
            "✅ Avto-yangilash " + ("yoqildi" if on else "o'chirildi")
        )
        return

    await update.effective_message.reply_text(
        "Foydalanish: `/autoupdate [on|off|status|hours N|apply on|apply off]`"
    )


async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/projects` — read-only inspector for the projects/repos/group-links DB."""
    if not config.is_developer(update.effective_user.id):
        return
    projects = db.list_projects()
    if not projects:
        await update.effective_message.reply_text(
            "Loyihalar yo'q. Bot qayta ishga tushganda `.env`'dan avtomatik yaratiladi."
        )
        return
    lines: list[str] = ["📁 *Loyihalar*", ""]
    for p in projects:
        lines.append(f"🌐 *{p['id']}* — {p['name']}")
        if p.get("description"):
            lines.append(f"  _{p['description']}_")
        repos = db.list_repos(p["id"])
        if repos:
            lines.append("  *Repolar:*")
            for r in repos:
                role = r["role"]
                ghr = r["github_repo"]
                sb = r["stage_branch"]
                pb = r["prod_branch"]
                lines.append(f"    • `{role}` → `{ghr}` ({sb} → {pb})")
                rp = r.get("repo_path") or "?"
                lines.append(f"      📂 `{rp}`")
                if r.get("description"):
                    lines.append(f"      _{r['description']}_")
        else:
            lines.append("  _(repolar qo'shilmagan)_")
        groups = db.groups_for_project(p["id"])
        if groups:
            lines.append("  *Guruhlar:* " + ", ".join(f"`{g}`" for g in groups))
        lines.append("")
    devs = db.list_developers()
    if devs:
        lines.append("👥 *Dasturchilar (DB):*")
        for d in devs:
            label = d.get("label") or "(yo'q)"
            lines.append(f"  • `{d['user_id']}` — {label}")
    await _safe_md_send(update.effective_message.reply_text, "\n".join(lines))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/help` — full command reference."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    text = (
        "🤖 *Buyruqlar va tugmalar*\n\n"
        "*Tugmali panel — bir bosishda*\n"
        "📊 Holat · 💬 Chatlar · 📖 Yordam\n"
        "✅ Qabul · 🔄 Qayta · ⏭ O'tkazib\n"
        "🚀 Stage / Dev / Prod — push\n"
        "📤 Chiqarish · ↩️ Qaytarish · ⏹ To'xtatish\n"
        "⏸ Hammasini · ⚙ Menyu · 🩺 Ping\n"
        "\n"
        "_Tugmalar oxirgi muammo bo'yicha ishlaydi. Aniq tanlov uchun buyruqni id bilan yozing._\n"
        "\n"
        "*Yozma buyruqlar (argument bilan)*\n"
        "`/chat [nom]` — yangi chat yaratish yoki o'tish\n"
        "`/ask <savol>` — bir martalik savol-javob (o'zgarishsiz)\n"
        "`/task <tavsif>` — to'liq tuzatish jarayoni\n"
        "`/accept [id]` · `/skip [id]` · `/publish [id]` · `/rollback [id]`\n"
        "`/retry [izoh]` — qayta tahlil (ixtiyoriy izoh bilan)\n"
        "`/push stage|dev|prod`\n"
        "\n"
        "*Holat*\n"
        "`/status` · `/projects` · `/menu` · `/help` · `/ping` · `/whereami`\n"
        "\n"
        "*Prefikslar* (DM free-text uchun)\n"
        "`!xabar` — TASK sifatida majburlash\n"
        "`?xabar` — ASK sifatida majburlash\n"
        "\n"
        "_Free-text DM avtomatik klassifikator orqali yo'naltiriladi._"
    )
    await _safe_md_send(update.effective_message.reply_text, text, reply_markup=MAIN_MENU)


async def cmd_stopall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/stopall` — cancel in-flight Claude calls on every chat session."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    uc = chat_sessions.get_user_chats(user_id)
    cancelled: list[str] = []
    for name, s in uc.sessions.items():
        if s.current_task and not s.current_task.done():
            s.current_task.cancel()
            cancelled.append(name)
    if not cancelled:
        await update.effective_message.reply_text("Hech bir chatda ishlayotgan chaqiruv yo'q.")
    else:
        await update.effective_message.reply_text(
            "To'xtatildi: " + ", ".join(cancelled)
        )


# ---------- Phase 3: /push /rollback (with confirmations) ----------

_BRANCH_ALIAS = {
    "stage": lambda: config.STAGE_BRANCH,
    "dev":   lambda: "dev",           # literal
    "prod":  lambda: config.PROD_BRANCH,
}


async def cmd_push(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/push [stage|dev|prod]` — merge STAGE_BRANCH into the named branch + push.

    No arg → shows a 3-button chooser.
    """
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    args = ctx.args or []
    if not args:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"stage ({config.STAGE_BRANCH})", callback_data="push_ask:stage",
            ),
            InlineKeyboardButton("dev", callback_data="push_ask:dev"),
            InlineKeyboardButton(
                f"prod ({config.PROD_BRANCH})", callback_data="push_ask:prod",
            ),
        ]])
        await update.effective_message.reply_text(
            "Qaysi branch'ga push qilay?", reply_markup=kb,
        )
        return

    alias = args[0].lower()
    if alias not in _BRANCH_ALIAS:
        await update.effective_message.reply_text(
            "Ishlatish: `/push stage | dev | prod`"
        )
        return
    await _push_confirm(update.effective_message, alias)


async def _push_confirm(msg: Message, alias: str) -> None:
    target = _BRANCH_ALIAS[alias]()
    mode_note = "(DRY_RUN — push skipped)" if config.DRY_RUN else "(LIVE — real push)"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Ha, push qil", callback_data=f"push_ok:{alias}"),
        InlineKeyboardButton("Bekor", callback_data="push_cancel:_"),
    ]])
    await msg.reply_text(
        f"{config.STAGE_BRANCH} → {target} {mode_note}\n\nTasdiqlaysizmi?",
        reply_markup=kb,
    )


def _issue_for_command(issue_id: str | None, filter_fn=None) -> Issue | None:
    """Shared lookup for command-based issue actions.

    - With issue_id → exact match.
    - Without → latest issue matching optional filter_fn.
    Returns None if nothing qualifies.
    """
    if issue_id:
        iss = ISSUES.get(issue_id)
        if iss and (filter_fn is None or filter_fn(iss)):
            return iss
        return None
    candidates = [i for i in ISSUES.values() if (filter_fn is None or filter_fn(i))]
    if not candidates:
        return None
    return max(candidates, key=lambda i: i.created_at)


async def cmd_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/accept [issue_id]` — apply the fix for the given or latest backend_bug issue."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    args = ctx.args or []
    issue_id = args[0] if args else None

    def _has_files(i: Issue) -> bool:
        return bool(i.diagnosis.get("files_to_change")) and not i.merged_to_stage

    issue = _issue_for_command(issue_id, _has_files)
    if issue is None:
        await update.effective_message.reply_text(
            "Qo'llanadigan muammo yo'q (files_to_change bo'sh yoki issue topilmadi)."
        )
        return
    await update.effective_message.reply_text(
        f"{issue.id} uchun tuzatma qo'llanmoqda..."
    )
    await _apply_issue(ctx, issue)


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/skip [issue_id]` — drop an issue from ISSUES (latest by default)."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    args = ctx.args or []
    issue_id = args[0] if args else None
    issue = _issue_for_command(issue_id)
    if issue is None:
        await update.effective_message.reply_text("O'tkazib yuboriladigan muammo yo'q.")
        return
    _drop_issue(issue.id)
    await update.effective_message.reply_text(f"{issue.id} o'tkazib yuborildi.")


async def cmd_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/publish [issue_id]` — promote stage → prod for a merged issue (latest by default)."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    args = ctx.args or []
    issue_id = args[0] if args else None
    issue = _issue_for_command(issue_id, lambda i: i.merged_to_stage)
    if issue is None:
        await update.effective_message.reply_text(
            "Prodga chiqariladigan muammo yo'q (avval /accept bilan stage'ga birlashtiring)."
        )
        return
    await update.effective_message.reply_text(
        f"{issue.id} {config.PROD_BRANCH} shoxobchasiga chiqarilmoqda..."
    )
    await _publish_issue(ctx, issue)


async def cmd_rollback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """`/rollback [issue_id]` — revert a bot fix. No arg → latest issue."""
    user_id = update.effective_user.id
    if not config.is_developer(user_id):
        return
    args = ctx.args or []
    issue_id = args[0] if args else None
    if issue_id is None:
        latest = _latest_issue()
        if latest is None:
            await update.effective_message.reply_text("Rollback uchun muammo yo'q.")
            return
        issue_id = latest.id
    elif issue_id not in ISSUES:
        await _safe_md_send(
            update.effective_message.reply_text,
            f"`{issue_id}` topilmadi. Aniq ID bering yoki `/rollback` bilan oxirgisini tanlang.",
        )
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Ha, revert qil", callback_data=f"roll_ok:{issue_id}"),
        InlineKeyboardButton("Bekor", callback_data="roll_cancel:_"),
    ]])
    await _safe_md_send(
        update.effective_message.reply_text,
        f"`{issue_id}` commitini revert qilaymi?\n"
        f"stage va prod ({config.PROD_BRANCH}) branchlarida yangi revert commit yaratiladi.",
        reply_markup=kb,
    )


# ---------- startup ----------

def _require(name: str, value):
    if not value:
        raise SystemExit(f"Missing env var: {name}")


def validate_config():
    _require("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN)
    if not config.TELEGRAM_DEVELOPER_IDS:
        raise SystemExit(
            "Missing env var: TELEGRAM_DEVELOPER_IDS (or legacy TELEGRAM_DEVELOPER_ID)"
        )
    # Check the RAW configured REPO_PATH string, not config.REPO_PATH — when
    # unset, Path("").resolve() collapses to the cwd (the exe's own folder),
    # which .exists() would wrongly report True. config._resolved reads the
    # DB-backed value first, then .env.
    raw_repo = (config._resolved("REPO_PATH") or "").strip()
    if not raw_repo:
        raise SystemExit("Missing setting: REPO_PATH (set it in the GUI Settings tab)")
    if not Path(raw_repo).exists():
        raise SystemExit(f"REPO_PATH does not exist: {raw_repo}")


async def _post_init_start_periodic_jobs(app: Application) -> None:
    """Spawn long-lived background tasks that need the asyncio loop.

    Currently: the auto-update poller. Runs every `update_check_hours`,
    DMs the dev list when a new commit is detected, and (if
    `update_auto_apply` is true) pulls + restarts automatically.
    """
    asyncio.create_task(_auto_update_loop(app))


async def _auto_update_loop(app: Application) -> None:
    """Periodic GitHub poll → notify (and optionally apply) new versions."""
    # Initial 5-min grace so logs settle and we don't paste an "update
    # available" the moment a colleague starts up after pulling manually.
    await asyncio.sleep(300)
    while True:
        try:
            interval = updater.check_interval_seconds()
            if interval <= 0:
                await asyncio.sleep(3600)  # disabled — poll setting once per hour
                continue
            new = await asyncio.to_thread(updater.check_for_update)
            if new:
                short = new["sha"][:8]
                msg = new["message"]
                log.info("auto-updater: new version available %s — %s", short, msg)
                if updater.auto_apply_enabled() and not ONGOING_ACKS:
                    log.warning("auto-updater: auto-applying %s", short)
                    ok, output = await asyncio.to_thread(updater.apply_update)
                    if ok:
                        text = f"♻️ Avto-yangilandi → `{short}`\n_{msg}_\nBot qayta ishga tushmoqda..."
                        for dev_id in (config.TELEGRAM_DEVELOPER_IDS or []):
                            try:
                                await app.bot.send_message(
                                    chat_id=dev_id, text=text, parse_mode="Markdown",
                                )
                            except Exception:  # noqa: BLE001
                                log.exception("auto-update notice DM to %s failed", dev_id)
                        await asyncio.sleep(2)
                        updater.restart_bot()
                    else:
                        log.error("auto-updater: pull failed: %s", output[:200])
                else:
                    # Notify devs once per detected commit (re-detection on
                    # next loop iteration is fine — just a duplicate notice
                    # at most once per `interval`).
                    text = (
                        f"🆙 Yangi versiya: `{short}`\n_{msg}_\n\n"
                        "`/update` bilan qo'lda yangilang, yoki `/version`'da avtomatikni yoqing."
                    )
                    for dev_id in (config.TELEGRAM_DEVELOPER_IDS or []):
                        try:
                            await app.bot.send_message(
                                chat_id=dev_id, text=text, parse_mode="Markdown",
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("update notice DM to %s failed", dev_id)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("auto-update loop iteration failed")
            await asyncio.sleep(600)


def build_app() -> Application:
    """Build the PTB Application with handlers wired up. Used by both CLI and GUI.

    `concurrent_updates=True` is critical: each analysis blocks on Claude CLI
    for 5-10 minutes. Without concurrency, updates from a second monitored
    group (or button clicks from the dev) would sit queued until the first
    analysis returns, making the bot look frozen.
    """
    validate_config()
    # SQLite — survive restart, audit log, settings store, projects model.
    db.init()
    # One-shot migrations.
    legacy_chats = config.ENV_FILE.parent / "chats.json"
    if legacy_chats.exists():
        try:
            n = db.import_chats_json_if_needed(legacy_chats)
            if n:
                bak = legacy_chats.with_suffix(".json.bak")
                legacy_chats.rename(bak)
                log.info("imported %d chat(s) from chats.json → renamed to %s", n, bak.name)
        except Exception:  # noqa: BLE001
            log.exception("chats.json migration failed; leaving file in place")
    # Bootstrap projects/repos/developers/settings from .env on first start.
    # Idempotent — re-running just refreshes missing settings, never clobbers
    # an existing project or already-set settings value.
    try:
        db.bootstrap_from_env(
            repo_path=str(config.REPO_PATH) if config.REPO_PATH else "",
            github_repo=config.GITHUB_REPO,
            stage_branch=config.STAGE_BRANCH,
            prod_branch=config.PROD_BRANCH,
            monitored_groups=config.MONITORED_GROUP_IDS,
            developer_ids=config.TELEGRAM_DEVELOPER_IDS,
            github_token=config.GITHUB_TOKEN or None,
            trigger_keywords=",".join(config.TRIGGER_KEYWORDS),
            dry_run=config.DRY_RUN,
            claude_cli=config.CLAUDE_CLI,
            claude_timeout=config.CLAUDE_TIMEOUT,
            max_parallel_claude=int(__import__("os").environ.get("MAX_PARALLEL_CLAUDE", "5") or "5"),
        )
        # Reload so DB-backed values take effect immediately for this run.
        config.reload()
    except Exception:  # noqa: BLE001
        log.exception("bootstrap_from_env failed; continuing on .env values")
    # Restore open issues from previous run so post-restart Accept/Retry/Skip
    # buttons still resolve to real Issue objects.
    _hydrate_issues_from_db()

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init_start_periodic_jobs)
        .build()
    )
    # Commands are PRIVATE-ONLY.
    #
    # The monitored group is a place where staff report problems — nothing
    # else. A bot that answers /menu, /status or /chatlist in there turns a
    # complaints channel into a console, and every reply is noise for the
    # people trying to describe a bug. The whole interactive surface
    # (menus, diagnosis cards, approve/skip/publish) belongs in the DM with
    # the developers, which is where the cards are sent anyway.
    private = filters.ChatType.PRIVATE
    for name, handler in (
        ("start", cmd_start),
        ("ping", cmd_ping),
        ("whereami", cmd_whereami),
        ("menu", cmd_menu),
        ("help", cmd_help),
        ("status", cmd_status),
        ("projects", cmd_projects),
        ("version", cmd_version),
        ("update", cmd_update),
        ("autoupdate", cmd_autoupdate),
        ("chat", cmd_chat),
        ("chatlist", cmd_chatlist),
        ("stop", cmd_stop),
        ("stopall", cmd_stopall),
        ("ask", cmd_ask),
        ("task", cmd_task),
        ("retry", cmd_retry),
        ("accept", cmd_accept),
        ("skip", cmd_skip),
        ("publish", cmd_publish),
        ("push", cmd_push),
        ("rollback", cmd_rollback),
    ):
        app.add_handler(CommandHandler(name, handler, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    # Accept text, photo, or image-type documents. Non-image files are ignored.
    media_filter = filters.TEXT | filters.PHOTO | filters.Document.IMAGE
    # Menu button taps must be handled BEFORE on_dm so they don't trigger the
    # intent classifier as free text.
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & _menu_button_filter(), on_menu_button),
    )
    # `~filters.COMMAND` matters: with commands now private-only, a "/menu"
    # typed in the group would otherwise fall through to this handler as plain
    # TEXT and get sent to the classifier — burning a Claude call on the word
    # "/menu". Commands in the group are ignored outright.
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & media_filter & ~filters.COMMAND, on_group_message),
    )
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & media_filter, on_dm))
    return app


def main():
    # Mirror the GUI's file logging when running as a CLI.
    try:
        from gui import _install_file_handler
        _install_file_handler(config.ENV_FILE)
    except Exception:  # noqa: BLE001
        log.exception("could not install file log handler")
    app = build_app()
    log.info("starting bot")
    log.info("\n%s", config.summarize())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
