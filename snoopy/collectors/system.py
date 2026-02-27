"""System state collector — sleep/wake and lock/unlock detection.

Polling-based approach:
- Lock/unlock: checks CGSessionCopyCurrentDictionary() for screen lock state
- Sleep/wake: detects large time gaps between polls (system was asleep)

Logs only on state transitions.
"""

import logging
import time

import Quartz

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

# If elapsed time between polls exceeds interval * this factor, system was sleeping
_SLEEP_GAP_FACTOR = 6


def _is_screen_locked() -> bool:
    """Check if the screen is currently locked via CGSession."""
    session = Quartz.CGSessionCopyCurrentDictionary()
    if session is None:
        return False
    return bool(session.get("CGSSessionScreenIsLocked", False))


class SystemCollector(BaseCollector):
    name = "system"
    interval = config.SYSTEM_INTERVAL

    def setup(self) -> None:
        self._last_locked: bool | None = None
        self._last_poll_ts: float = time.time()

    def collect(self) -> None:
        now = time.time()
        elapsed = now - self._last_poll_ts

        # Detect sleep/wake via time gap
        if self._last_poll_ts > 0 and elapsed > self.interval * _SLEEP_GAP_FACTOR:
            gap_minutes = elapsed / 60
            self._log_event("sleep", f"at={self._last_poll_ts:.0f}")
            self._log_event("wake", f"gap={gap_minutes:.1f}min")
            log.info("[%s] detected sleep/wake gap of %.1f min", self.name, gap_minutes)

        self._last_poll_ts = now

        # Check lock state
        locked = _is_screen_locked()
        if self._last_locked is None:
            # First poll — record initial state only if locked (interesting event)
            self._last_locked = locked
            if locked:
                self._log_event("lock")
            return

        if locked != self._last_locked:
            event_type = "lock" if locked else "unlock"
            self._log_event(event_type)
            self._last_locked = locked

    def _log_event(self, event_type: str, details: str = "") -> None:
        self.buffer.push(Event(
            table="system_events",
            columns=["timestamp", "event_type", "details"],
            values=(time.time(), event_type, details),
        ))
        log.info("[%s] %s", self.name, event_type)
