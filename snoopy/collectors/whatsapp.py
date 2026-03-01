"""WhatsApp collector — captures visible chats when WhatsApp is focused.

Uses a compiled Swift helper that walks WhatsApp's accessibility tree
to extract chat list, messages, and header info.

Only activates when WhatsApp is the frontmost app. Deduplicates snapshots
by content hash to avoid redundant storage.

Timing strategy:
- Base interval is 2s (cheap NSWorkspace frontmost check).
- On focus-in: immediate first scrape.
- While focused: re-scrape every 10s (AX walk is heavier).
- On focus-out: one final scrape to catch last-second activity.
"""

import json
import logging
import subprocess
import time

from AppKit import NSWorkspace

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_WHATSAPP_BUNDLE = "net.whatsapp.WhatsApp"
_REFETCH_S = 10


def _whatsapp_is_frontmost() -> bool:
    active = NSWorkspace.sharedWorkspace().activeApplication()
    if not active:
        return False
    return active.get("NSApplicationBundleIdentifier", "") == _WHATSAPP_BUNDLE


def _fetch_whatsapp() -> dict | None:
    try:
        result = subprocess.run(
            [str(config.WHATSAPP_HELPER), "messages"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


class WhatsAppCollector(BaseCollector):
    name = "whatsapp"
    interval = config.WHATSAPP_INTERVAL

    def setup(self) -> None:
        self._last_snapshot_key: str | None = None
        self._was_frontmost: bool = False
        self._last_fetch_ts: float = 0

    def _emit(self, data: dict) -> None:
        messages = data.get("messages", [])
        chat_list = data.get("chat_list", [])

        if not messages and not chat_list:
            return

        key = json.dumps({"m": messages, "cl": chat_list}, sort_keys=True)
        if key == self._last_snapshot_key:
            return
        self._last_snapshot_key = key

        self.buffer.push(Event(
            table="whatsapp_events",
            columns=[
                "timestamp", "chat_name", "chat_members",
                "messages", "chat_list",
            ],
            values=(
                time.time(),
                data.get("chat_name", ""),
                data.get("chat_members", ""),
                json.dumps(messages),
                json.dumps(chat_list),
            ),
        ))

    def collect(self) -> None:
        focused = _whatsapp_is_frontmost()

        if not focused:
            if self._was_frontmost:
                self._was_frontmost = False
                data = _fetch_whatsapp()
                if data and "error" not in data:
                    self._emit(data)
            return

        now = time.time()

        if self._was_frontmost and (now - self._last_fetch_ts) < _REFETCH_S:
            return

        self._was_frontmost = True
        self._last_fetch_ts = now

        data = _fetch_whatsapp()
        if not data or "error" in data:
            return

        self._emit(data)
