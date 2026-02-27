"""Battery collector — tracks charge level, charging state, and power source.

Polls `pmset -g batt` at a slow interval. Only logs on state change
(percent, charging, or power source changed).
"""

import logging
import re
import subprocess
import time

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_BATT_RE = re.compile(r"(\d+)%;\s*(charging|discharging|charged|finishing charge)")


def _parse_pmset(output: str) -> tuple[int, bool, str] | None:
    """Parse pmset -g batt output into (percent, is_charging, power_source).

    Returns None if output can't be parsed (e.g. desktop Mac with no battery).
    """
    source = "unknown"
    if "'AC Power'" in output:
        source = "ac"
    elif "'Battery Power'" in output:
        source = "battery"

    match = _BATT_RE.search(output)
    if not match:
        return None

    percent = int(match.group(1))
    state = match.group(2)
    is_charging = state in ("charging", "finishing charge")

    return percent, is_charging, source


class BatteryCollector(BaseCollector):
    name = "battery"
    interval = config.BATTERY_INTERVAL

    def setup(self) -> None:
        self._last_percent: int | None = None
        self._last_charging: bool | None = None
        self._last_source: str | None = None

    def collect(self) -> None:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return

        parsed = _parse_pmset(result.stdout)
        if parsed is None:
            return

        percent, is_charging, source = parsed

        # Only log on change
        if (percent == self._last_percent
                and is_charging == self._last_charging
                and source == self._last_source):
            return

        self._last_percent = percent
        self._last_charging = is_charging
        self._last_source = source

        self.buffer.push(Event(
            table="battery_events",
            columns=["timestamp", "percent", "is_charging", "power_source"],
            values=(time.time(), percent, int(is_charging), source),
        ))
