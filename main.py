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
import git_ops
import github_ops
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
    return (
        f"Muammo {issue.id} — guruh: {issue.group_title}\n"
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
):
    """Run analyze() on `text` (+ optional image paths) and store an Issue.

    Shared by group + DM entry points. `ack_message` is the bot's "AI tahlil
    qilmoqda..." (group) or "Xabar tahlil qilinmoqda..." (DM) message that we
    edit in place with the final category-specific verdict once analysis
    completes.

    Group entry point only reaches this after Stage 1 classified OUR_PROBLEM,
    so the ack is meaningful; DMs always get an ack since the dev sends tasks
    intentionally.

    After analysis, attached images are deleted.
    """
    image_strs = [str(p) for p in (image_paths or [])]
    try:
        try:
            diagnosis = await run_capped(
                analyze, text, source_chat_title, image_strs or None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("analyze failed")
            await ack_message.edit_text(f"Tahlil bajarilmadi: {exc}")
            return

        if not diagnosis:
            await ack_message.edit_text(
                "Tahlil natija qaytarmadi — iltimos, aniqroq tushuntirib yoki screenshot qo'shib qayta yuboring."
            )
            return

        issue_id = uuid.uuid4().hex[:8]
        ISSUES[issue_id] = Issue(
            id=issue_id,
            group_id=source_chat_id,
            group_title=source_chat_title,
            message=text,
            user_message_id=source_message_id,
            diagnosis=diagnosis,
        )

        category = diagnosis.get("category") or "unclear"
        summary = diagnosis.get("summary") or ""
        eta = diagnosis.get("eta_minutes") or "?"

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
    is_problem, reason = await run_capped(
        classify_via_claude,
        text,
        [str(image_path)] if image_path else None,
    )
    if not is_problem:
        _cleanup_image(image_path)
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
    # ma'lumot kerak...", etc).
    group_title = msg.chat.title or str(msg.chat.id)
    log.info(
        "stage1 → our_problem (reason=%s); stage2 analysing msg in %s (image=%s): %s",
        reason, group_title, has_image, text[:80] if text else "(no caption)",
    )

    ack = await msg.reply_text("AI tahlil qilmoqda...")

    effective_text = text or "(Screenshot yuborildi — matn yo'q)"
    await _process_task(
        ctx, effective_text, msg.chat.id, group_title, msg.message_id, ack,
        image_paths=[image_path] if image_path else None,
    )


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
        stashed = _PENDING_TASKS.pop(payload, None)
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
        stashed = _PENDING_TASKS.pop(payload, None)
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
        ISSUES.pop(issue_id, None)
        await q.edit_message_text(f"{issue_id} o'tkazib yuborildi.")
        return

    if action == "rty":
        issue.awaiting_retry_prompt = True
        issue.retry_initiator = q.from_user.id
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
            ISSUES.pop(issue.id, None)
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
        pr_url, merged = await asyncio.to_thread(
            github_ops.create_and_merge_pr,
            branch,
            f"fix: bot issue {issue.id}",
            issue.diagnosis.get("technical_summary", ""),
        )
        issue.pr_url = pr_url
        issue.merged_to_stage = merged

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
        try:
            await ctx.bot.send_message(
                issue.group_id,
                "Tuzatildi. Xabaringiz uchun rahmat!",
                reply_to_message_id=issue.user_message_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("could not notify group %s", issue.group_id)
        await _dm_all_devs(
            ctx,
            f"{issue.id} {config.PROD_BRANCH} shoxobchasiga chiqarildi.",
        )
        ISSUES.pop(issue.id, None)
    else:
        await _dm_all_devs(
            ctx,
            f"{issue.id}: {config.PROD_BRANCH} shoxobchasiga birlashtirish bajarilmadi; loglarni tekshiring.",
        )


# =============================================================================
# DM chat mode — multi-session, parallel, intent-routed.
# =============================================================================

# Pending TASK confirmations: callback_data → (text, image_path_str_or_none).
# Created when the DM intent classifier says TASK and we ask the dev for
# confirmation. Cleared once a callback resolves or after ~30 min by natural
# restart. Keyed by a short hash to keep callback_data under Telegram's 64-byte
# cap.
_PENDING_TASKS: dict[str, tuple[str, str | None]] = {}


def _auto_chat_name(text: str) -> str:
    """Deterministic short name for an auto-created chat session."""
    t = (text or "").strip().replace("\n", " ")
    if not t:
        return "chat"
    short = t[:28].strip()
    return short or "chat"


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
        # around until the dev chooses — _PENDING_TASKS carries a path string.
        token = _pending_task_id(text + (str(image_path) if image_path else ""))
        _PENDING_TASKS[token] = (text, str(image_path) if image_path else None)
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
        "`/status` · `/menu` · `/help` · `/ping` · `/whereami`\n"
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
    ISSUES.pop(issue.id, None)
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
    _require("REPO_PATH", str(config.REPO_PATH))
    if not config.REPO_PATH.exists():
        raise SystemExit(f"REPO_PATH does not exist: {config.REPO_PATH}")


def build_app() -> Application:
    """Build the PTB Application with handlers wired up. Used by both CLI and GUI.

    `concurrent_updates=True` is critical: each analysis blocks on Claude CLI
    for 5-10 minutes. Without concurrency, updates from a second monitored
    group (or button clicks from the dev) would sit queued until the first
    analysis returns, making the bot look frozen.
    """
    validate_config()
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("ping",     cmd_ping))
    app.add_handler(CommandHandler("whereami", cmd_whereami))
    app.add_handler(CommandHandler("menu",     cmd_menu))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("chat",     cmd_chat))
    app.add_handler(CommandHandler("chatlist", cmd_chatlist))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("stopall",  cmd_stopall))
    app.add_handler(CommandHandler("ask",      cmd_ask))
    app.add_handler(CommandHandler("task",     cmd_task))
    app.add_handler(CommandHandler("retry",    cmd_retry))
    app.add_handler(CommandHandler("accept",   cmd_accept))
    app.add_handler(CommandHandler("skip",     cmd_skip))
    app.add_handler(CommandHandler("publish",  cmd_publish))
    app.add_handler(CommandHandler("push",     cmd_push))
    app.add_handler(CommandHandler("rollback", cmd_rollback))
    app.add_handler(CallbackQueryHandler(on_button))
    # Accept text, photo, or image-type documents. Non-image files are ignored.
    media_filter = filters.TEXT | filters.PHOTO | filters.Document.IMAGE
    # Menu button taps must be handled BEFORE on_dm so they don't trigger the
    # intent classifier as free text.
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & _menu_button_filter(), on_menu_button),
    )
    app.add_handler(MessageHandler(filters.ChatType.GROUPS  & media_filter, on_group_message))
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
