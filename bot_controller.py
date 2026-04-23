"""Runs the Telegram bot in a background thread with start/stop/pause/resume.

PTB's Application has an asyncio lifecycle (initialize -> start -> updater.start_polling
-> updater.stop -> stop -> shutdown). We host that lifecycle in a dedicated thread
with its own event loop, so the Tk main loop stays responsive.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

from telegram import Update

log = logging.getLogger(__name__)


class BotController:
    STATES = ("stopped", "starting", "running", "paused", "stopping")

    def __init__(self, on_state_change: Optional[Callable[[str, str | None], None]] = None):
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = None
        self._state = "stopped"
        self._state_lock = threading.Lock()
        self._on_state_change = on_state_change

    # ---------- state ----------

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def _set_state(self, new: str, detail: str | None = None):
        with self._state_lock:
            if self._state == new and detail is None:
                return
            self._state = new
        log.info("bot state -> %s%s", new, f" ({detail})" if detail else "")
        if self._on_state_change:
            try:
                self._on_state_change(new, detail)
            except Exception:
                log.exception("state callback failed")

    # ---------- controls ----------

    def start(self) -> tuple[bool, str | None]:
        if self.state != "stopped":
            return False, f"already {self.state}"

        # Reload .env so freshly-saved settings take effect.
        import config
        config.reload()
        from bot_state import state as bs
        bs.paused = False

        self._set_state("starting")
        self._thread = threading.Thread(target=self._run_thread, name="bot-runner", daemon=True)
        self._thread.start()
        return True, None

    def stop(self, timeout: float = 15.0) -> tuple[bool, str | None]:
        if self.state == "stopped":
            return False, "already stopped"
        if not self._loop:
            self._set_state("stopped")
            return True, None

        self._set_state("stopping")

        async def _shutdown():
            try:
                if self._app and self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
            except Exception as exc:
                log.warning("updater.stop failed: %s", exc)
            try:
                if self._app and self._app.running:
                    await self._app.stop()
            except Exception as exc:
                log.warning("app.stop failed: %s", exc)
            try:
                if self._app:
                    await self._app.shutdown()
            except Exception as exc:
                log.warning("app.shutdown failed: %s", exc)

        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            fut.result(timeout=timeout)
        except Exception as exc:
            log.error("shutdown wait failed: %s", exc)

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

        if self._thread:
            self._thread.join(timeout=timeout)

        self._app = None
        self._loop = None
        self._thread = None
        self._set_state("stopped")
        return True, None

    def pause(self) -> tuple[bool, str | None]:
        if self.state != "running":
            return False, f"cannot pause while {self.state}"
        from bot_state import state as bs
        bs.paused = True
        self._set_state("paused")
        return True, None

    def resume(self) -> tuple[bool, str | None]:
        if self.state != "paused":
            return False, f"cannot resume while {self.state}"
        from bot_state import state as bs
        bs.paused = False
        self._set_state("running")
        return True, None

    # ---------- internal ----------

    def _run_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _startup():
            from main import build_app
            self._app = build_app()
            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        try:
            loop.run_until_complete(_startup())
        except SystemExit as exc:
            self._set_state("stopped", str(exc))
            loop.close()
            self._loop = None
            return
        except Exception as exc:
            log.exception("bot failed to start")
            self._set_state("stopped", f"start failed: {exc}")
            loop.close()
            self._loop = None
            return

        self._set_state("running")
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass
