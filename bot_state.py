"""Shared runtime state checked by handlers (e.g. pause flag)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BotState:
    paused: bool = False


state = BotState()
