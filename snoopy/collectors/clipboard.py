"""Clipboard collector — monitors pasteboard for text changes.

Polls NSPasteboard.generalPasteboard().changeCount() to detect changes.
Skips content from password managers (configurable exclusion list).
Caps captured text at CLIPBOARD_MAX_LENGTH.
"""

import logging
import time

from AppKit import NSPasteboard, NSStringPboardType, NSWorkspace

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


class ClipboardCollector(BaseCollector):
    name = "clipboard"
    interval = config.CLIPBOARD_INTERVAL

    def setup(self) -> None:
        self._pasteboard = NSPasteboard.generalPasteboard()
        self._last_change_count = self._pasteboard.changeCount()

    def collect(self) -> None:
        current_count = self._pasteboard.changeCount()
        if current_count == self._last_change_count:
            return
        self._last_change_count = current_count

        # Check source app against exclusion list
        source_app = self._get_frontmost_app()
        if source_app in config.CLIPBOARD_EXCLUDED_APPS:
            log.debug("skipping clipboard from excluded app: %s", source_app)
            return

        text = self._pasteboard.stringForType_(NSStringPboardType)
        if not text:
            return

        # Truncate to max length
        if len(text) > config.CLIPBOARD_MAX_LENGTH:
            text = text[: config.CLIPBOARD_MAX_LENGTH]

        self.buffer.push(Event(
            table="clipboard_events",
            columns=["timestamp", "content_text", "content_type", "source_app"],
            values=(time.time(), text, "text/plain", source_app),
        ))

    @staticmethod
    def _get_frontmost_app() -> str:
        workspace = NSWorkspace.sharedWorkspace()
        active = workspace.activeApplication()
        if active:
            return active.get("NSApplicationName", "")
        return ""
