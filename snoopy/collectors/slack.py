"""Slack collector — captures visible messages when Slack is focused.

Uses a compiled Swift helper that sets AXEnhancedUserInterface on Slack's
Electron process and walks the accessibility tree to extract messages.

Only activates when Slack is the frontmost app. Deduplicates snapshots
by message content to avoid redundant storage.

Timing strategy:
- Base interval is 2s (cheap NSWorkspace frontmost check).
- On focus-in: immediate first scrape.
- While focused: re-scrape every 10s (AX walk is heavier).
- On focus-out: one final scrape to catch last-second sends/reactions.
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

_SLACK_BUNDLE = "com.tinyspeck.slackmacgap"
_REFETCH_S = 10  # seconds between AX scrapes while Slack stays focused


def _slack_is_frontmost() -> bool:
    """Check if Slack is the active (focused) app via NSWorkspace."""
    active = NSWorkspace.sharedWorkspace().activeApplication()
    if not active:
        return False
    return active.get("NSApplicationBundleIdentifier", "") == _SLACK_BUNDLE


def _fetch_messages() -> dict | None:
    """Run the slack_helper Swift binary and parse its JSON output."""
    try:
        result = subprocess.run(
            [str(config.SLACK_HELPER), "messages"],
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


class SlackCollector(BaseCollector):
    name = "slack"
    interval = config.SLACK_INTERVAL

    def setup(self) -> None:
        self._last_snapshot_key: str | None = None
        self._was_frontmost: bool = False
        self._last_fetch_ts: float = 0

    def _emit(self, data: dict) -> None:
        """Emit a slack_events row if the visible state changed."""
        messages = data.get("messages", [])
        if not messages:
            return

        unread = data.get("unread", [])

        key = json.dumps({"m": messages, "u": unread}, sort_keys=True)
        if key == self._last_snapshot_key:
            return
        self._last_snapshot_key = key

        self.buffer.push(Event(
            table="slack_events",
            columns=[
                "timestamp", "workspace", "channel_name",
                "messages", "unread",
            ],
            values=(
                time.time(),
                data.get("workspace", ""),
                data.get("channel_name", ""),
                json.dumps(messages),
                json.dumps(unread) if unread else None,
            ),
        ))

    def collect(self) -> None:
        focused = _slack_is_frontmost()

        if not focused:
            if self._was_frontmost:
                # Slack just lost focus — one final scrape to catch last-second activity
                self._was_frontmost = False
                data = _fetch_messages()
                if data and "error" not in data:
                    self._emit(data)
            return

        now = time.time()

        # If Slack was already focused, throttle fetches to every _REFETCH_S
        if self._was_frontmost and (now - self._last_fetch_ts) < _REFETCH_S:
            return

        self._was_frontmost = True
        self._last_fetch_ts = now

        data = _fetch_messages()
        if not data or "error" in data:
            return

        self._emit(data)
