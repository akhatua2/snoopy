"""Base collector ABC — all collectors inherit from this."""

from __future__ import annotations

import abc
import logging
import threading
import time

from snoopy.buffer import EventBuffer
from snoopy.db import Database

log = logging.getLogger(__name__)


class BaseCollector(abc.ABC):
    """Abstract base for all data collectors.

    Subclasses must implement:
        name        — unique string identifier
        interval    — seconds between collection cycles (0 = push-based / manual)
        collect()   — gather data and push Event(s) to self.buffer
    """

    name: str = ""
    interval: float = 5.0

    def __init__(self, buffer: EventBuffer, db: Database):
        self.buffer = buffer
        self.db = db
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── abstract ────────────────────────────────────────────────────────
    @abc.abstractmethod
    def collect(self) -> None:
        """Run one collection cycle. Push results via self.buffer.push()."""

    # ── lifecycle ───────────────────────────────────────────────────────
    def setup(self) -> None:
        """Optional one-time init (override in subclass)."""

    def teardown(self) -> None:
        """Optional cleanup (override in subclass)."""

    def start(self) -> None:
        """Start the collector in a background daemon thread."""
        self.setup()
        if self.interval <= 0:
            # Push-based collectors manage their own loop
            log.info("[%s] push-based collector started", self.name)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name=f"collector-{self.name}", daemon=True
        )
        self._thread.start()
        log.info("[%s] started (interval=%.1fs)", self.name, self.interval)

    def stop(self) -> None:
        """Signal the collector to stop and wait for its thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.interval + 2)
        self.teardown()
        log.info("[%s] stopped", self.name)

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()

    # ── watermark helpers ───────────────────────────────────────────────
    def get_watermark(self) -> str | None:
        return self.db.get_watermark(self.name)

    def set_watermark(self, value: str) -> None:
        self.db.set_watermark(self.name, value, time.time())

    # ── internal ────────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.collect()
            except Exception:
                log.exception("[%s] collection error", self.name)
            self._stop_event.wait(timeout=self.interval)
