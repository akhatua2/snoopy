"""Dock badge & status collector — tracks notification badges and app running state.

Uses a compiled Swift helper that walks the Dock's AX tree to read
badge counts (AXStatusLabel) and running status for each pinned app.

Polls every 5 seconds, diffs against previous snapshot, and emits events
when badges change or apps start/stop running.
"""

import json
import logging
import subprocess
import time

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


def _fetch_dock_items() -> list[dict] | None:
    """Run the dock_helper Swift binary and parse its JSON output."""
    try:
        result = subprocess.run(
            [str(config.DOCK_HELPER)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


class DockCollector(BaseCollector):
    name = "dock"
    interval = config.DOCK_INTERVAL

    def setup(self) -> None:
        self._prev: dict[str, dict] = {}
        self._first_run = True

    def collect(self) -> None:
        items = _fetch_dock_items()
        if items is None:
            return

        current = {}
        for item in items:
            app = item.get("app", "")
            if not app:
                continue
            current[app] = {
                "badge": item.get("badge", ""),
                "running": item.get("running", False),
            }

        if self._first_run:
            self._prev = current
            self._first_run = False
            return

        now = time.time()

        for app, state in current.items():
            prev_state = self._prev.get(app)

            if prev_state is None:
                # New app appeared in dock — treat as if it just started
                if state["running"]:
                    self._emit(now, "app_active", app)
                if state["badge"]:
                    self._emit(now, "badge_change", app, badge=state["badge"], prev_badge="")
                continue

            # Badge changed
            if state["badge"] != prev_state["badge"]:
                self._emit(
                    now, "badge_change", app,
                    badge=state["badge"],
                    prev_badge=prev_state["badge"],
                )

            # Running state changed
            if state["running"] and not prev_state["running"]:
                self._emit(now, "app_active", app)
            elif not state["running"] and prev_state["running"]:
                self._emit(now, "app_inactive", app)

        # Apps that disappeared from dock
        for app, prev_state in self._prev.items():
            if app not in current and prev_state["running"]:
                self._emit(now, "app_inactive", app)

        self._prev = current

    def _emit(self, ts: float, event_type: str, app: str,
              badge: str = "", prev_badge: str = "") -> None:
        self.buffer.push(Event(
            table="dock_events",
            columns=["timestamp", "event_type", "app_name", "badge_value", "prev_badge_value"],
            values=(ts, event_type, app, badge, prev_badge),
        ))
