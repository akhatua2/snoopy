"""Snoopy daemon — main orchestrator.

Initialises the database, starts all collector threads,
and runs the periodic buffer flush loop.

Supports SIGHUP for hot reload — stops old collectors, starts new ones,
keeping the DB and buffer intact so no data is lost.
"""

import logging
import os
import signal
import sys
import time

from snoopy.config import (
    DATA_DIR, LOG_PATH, PID_PATH,
    BUFFER_FLUSH_INTERVAL, HEALTH_HEARTBEAT_INTERVAL,
)
from snoopy.db import Database
from snoopy.buffer import EventBuffer

from snoopy.collectors.window import WindowCollector
from snoopy.collectors.location import LocationCollector
from snoopy.collectors.browser import BrowserCollector
from snoopy.collectors.shell import ShellCollector
from snoopy.collectors.media import MediaCollector
from snoopy.collectors.wifi import WifiCollector
from snoopy.collectors.clipboard import ClipboardCollector
from snoopy.collectors.network import NetworkCollector
from snoopy.collectors.filesystem import FilesystemCollector
from snoopy.collectors.notifications import NotificationCollector
from snoopy.collectors.audio import AudioCollector
from snoopy.collectors.messages import MessagesCollector
from snoopy.collectors.system import SystemCollector
from snoopy.collectors.applifecycle import AppLifecycleCollector
from snoopy.collectors.battery import BatteryCollector
from snoopy.collectors.calendar import CalendarCollector
from snoopy.collectors.oura import OuraCollector

log = logging.getLogger("snoopy")

ALL_COLLECTORS = [
    WindowCollector,
    LocationCollector,
    BrowserCollector,
    ShellCollector,
    MediaCollector,
    WifiCollector,
    ClipboardCollector,
    NetworkCollector,
    FilesystemCollector,
    NotificationCollector,
    AudioCollector,
    MessagesCollector,
    SystemCollector,
    AppLifecycleCollector,
    BatteryCollector,
    CalendarCollector,
    OuraCollector,
]


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(LOG_PATH)),
            logging.StreamHandler(sys.stderr),
        ],
    )


def _write_pid() -> None:
    PID_PATH.write_text(str(os.getpid()))


def _remove_pid() -> None:
    PID_PATH.unlink(missing_ok=True)


class Daemon:
    """Main daemon process that owns the DB, buffer, and all collectors."""

    def __init__(self):
        self.db = Database()
        self.buffer: EventBuffer | None = None
        self.collectors = []
        self._running = False

    def start(self) -> None:
        _setup_logging()
        _write_pid()
        log.info("snoopy daemon starting (pid=%d)", os.getpid())

        self.db.open()
        self.buffer = EventBuffer(self.db)
        self.db.log_health(time.time(), "startup", f"pid={os.getpid()}")

        self._start_collectors()

        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_reload)

        self._run_flush_loop()

    def stop(self) -> None:
        log.info("snoopy daemon shutting down")
        self._running = False
        self._stop_collectors()
        if self.buffer:
            self.buffer.flush()
        self.db.log_health(time.time(), "shutdown", "clean")
        self.db.close()
        _remove_pid()
        log.info("snoopy daemon stopped")

    def reload(self) -> None:
        """Hot reload: stop all collectors, re-apply schema, start fresh collectors.
        DB connection and buffer stay alive — no data lost.

        Safe for: adding new tables, adding new collectors, changing intervals.
        NOT safe for: dropping/renaming columns or tables, changing column types.
        For breaking schema changes, do a full restart with a migration instead.

        Trigger with: kill -HUP $(cat data/snoopy.pid)
        """
        log.info("snoopy daemon reloading")
        self._stop_collectors()
        if self.buffer:
            self.buffer.flush()
        # Re-apply schema in case new tables were added
        self.db._ensure_conn().executescript(Database._get_schema())
        self.db._ensure_conn().commit()
        self._start_collectors()
        self.db.log_health(time.time(), "reload")
        log.info("snoopy daemon reloaded — %d collectors running", len(self.collectors))

    def _start_collectors(self) -> None:
        for cls in ALL_COLLECTORS:
            collector = cls(self.buffer, self.db)
            self.collectors.append(collector)
            collector.start()
            log.info("collector %s started", collector.name)

    def _stop_collectors(self) -> None:
        for c in self.collectors:
            c.stop()
        self.collectors.clear()

    def _run_flush_loop(self) -> None:
        last_heartbeat = time.time()
        while self._running:
            time.sleep(BUFFER_FLUSH_INTERVAL)
            if self.buffer:
                self.buffer.flush()

            now = time.time()
            if now - last_heartbeat >= HEALTH_HEARTBEAT_INTERVAL:
                self.db.log_health(now, "heartbeat")
                last_heartbeat = now

    def _handle_signal(self, signum, frame) -> None:
        log.info("received signal %d", signum)
        self.stop()
        sys.exit(0)

    def _handle_reload(self, signum, frame) -> None:
        log.info("received SIGHUP — reloading")
        self.reload()


def main() -> None:
    daemon = Daemon()
    daemon.start()


if __name__ == "__main__":
    main()
