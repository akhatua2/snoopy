"""App lifecycle collector — tracks application launches and quits.

Uses `ps` to get running process info and diffs against previous snapshot.
New apps → launch event, missing apps → quit event.

We use subprocess instead of NSWorkspace.runningApplications() because
the latter returns stale data from daemon background threads.
"""

import logging
import re
import subprocess
import time

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

# Match lines from: ps -eo pid,comm
# Example: "  1234 /Applications/Safari.app/Contents/MacOS/Safari"
_APP_RE = re.compile(r"/Applications/(.+?)\.app/")
_SYSTEM_APP_RE = re.compile(r"/System/Applications/(.+?)\.app/")
_CORE_SERVICES_RE = re.compile(r"/CoreServices/(.+?)\.app/")


def _get_running_apps() -> set[str]:
    """Return set of running app names extracted from process paths.

    Uses `ps` which always returns fresh data regardless of thread context.
    """
    result = subprocess.run(
        ["ps", "-eo", "comm"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        return set()

    apps = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        for pattern in (_APP_RE, _SYSTEM_APP_RE, _CORE_SERVICES_RE):
            m = pattern.search(line)
            if m:
                apps.add(m.group(1))
                break
    return apps


class AppLifecycleCollector(BaseCollector):
    name = "applifecycle"
    interval = config.APPLIFECYCLE_INTERVAL

    def setup(self) -> None:
        self._previous_apps: set[str] | None = None

    def collect(self) -> None:
        current = _get_running_apps()

        if self._previous_apps is None:
            self._previous_apps = current
            log.info("[%s] first run — %d apps detected", self.name, len(current))
            return

        # Detect launches (in current but not in previous)
        for app_name in current - self._previous_apps:
            self._log_app_event("launch", app_name)

        # Detect quits (in previous but not in current)
        for app_name in self._previous_apps - current:
            self._log_app_event("quit", app_name)

        self._previous_apps = current

    def _log_app_event(self, event_type: str, app_name: str) -> None:
        if app_name in config.APP_EXCLUDED:
            return
        self.buffer.push(Event(
            table="app_events",
            columns=["timestamp", "event_type", "app_name", "bundle_id"],
            values=(time.time(), event_type, app_name, ""),
        ))
        log.info("[%s] %s %s", self.name, event_type, app_name)
