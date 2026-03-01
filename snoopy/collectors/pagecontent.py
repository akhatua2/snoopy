"""Chrome page content collector — captures visible text from any website.

Uses a compiled Swift helper that sets AXEnhancedUserInterface on Chrome
and walks the accessibility tree to extract all visible page content.

Captures Instagram DMs, ChatGPT conversations, W&B dashboards, email,
docs — anything visible in Chrome gets a full text snapshot.

Only activates when Chrome is the frontmost app. Deduplicates snapshots
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
from urllib.parse import urlparse

from AppKit import NSWorkspace

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_CHROME_BUNDLE = "com.google.Chrome"
_REFETCH_S = 10


def _chrome_is_frontmost() -> bool:
    active = NSWorkspace.sharedWorkspace().activeApplication()
    if not active:
        return False
    return active.get("NSApplicationBundleIdentifier", "") == _CHROME_BUNDLE


def _fetch_page_content() -> dict | None:
    try:
        result = subprocess.run(
            [str(config.CHROME_HELPER), "content"],
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


def _extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc or ""
    except Exception:
        return ""


class PageContentCollector(BaseCollector):
    name = "pagecontent"
    interval = config.PAGECONTENT_INTERVAL

    def setup(self) -> None:
        self._last_snapshot_key: str | None = None
        self._was_frontmost: bool = False
        self._last_fetch_ts: float = 0

    def _emit(self, data: dict) -> None:
        content = data.get("content", [])
        url = data.get("url", "")
        title = data.get("title", "")

        if not content and not url:
            return

        key = json.dumps({"u": url, "c": content}, sort_keys=True)
        if key == self._last_snapshot_key:
            return
        self._last_snapshot_key = key

        domain = _extract_domain(url)

        self.buffer.push(Event(
            table="page_content_events",
            columns=[
                "timestamp", "url", "domain", "title", "content",
            ],
            values=(
                time.time(),
                url,
                domain,
                title,
                json.dumps(content),
            ),
        ))

    def collect(self) -> None:
        focused = _chrome_is_frontmost()

        if not focused:
            if self._was_frontmost:
                self._was_frontmost = False
                data = _fetch_page_content()
                if data and "error" not in data:
                    self._emit(data)
            return

        now = time.time()

        if self._was_frontmost and (now - self._last_fetch_ts) < _REFETCH_S:
            return

        self._was_frontmost = True
        self._last_fetch_ts = now

        data = _fetch_page_content()
        if not data or "error" in data:
            return

        self._emit(data)
