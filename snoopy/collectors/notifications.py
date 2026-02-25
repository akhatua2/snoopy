"""Notification collector — reads macOS notification history.

Reads from the notification database in ~/Library/Group Containers/.
This requires Full Disk Access. On macOS Sequoia+ the DB format may
have changed, so we handle errors gracefully.
"""

import logging
import os
import plistlib
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)


def _find_notification_db() -> Path | None:
    """Find the macOS notification center database."""
    base = Path("~/Library/Group Containers/group.com.apple.usernoted").expanduser()
    db_path = base / "db2" / "db"
    if db_path.exists():
        return db_path
    # Fallback for older macOS
    db_path = base / "db" / "db"
    if db_path.exists():
        return db_path
    return None


class NotificationCollector(BaseCollector):
    name = "notifications"
    interval = config.NOTIFICATION_INTERVAL

    def setup(self) -> None:
        saved = self.get_watermark()
        self._last_id = int(saved) if saved else None
        self._permission_warned = False

    def collect(self) -> None:
        db_path = _find_notification_db()
        if db_path is None:
            log.debug("notification DB not found")
            return

        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(str(db_path), tmp)
        except PermissionError:
            if not self._permission_warned:
                log.warning("notification DB needs Full Disk Access — skipping until granted")
                self._permission_warned = True
            return
        except (OSError, shutil.Error):
            log.exception("failed to copy notification db")
            return

        try:
            conn = sqlite3.connect(tmp)

            # First run: skip historical notifications, just set watermark to current max
            if self._last_id is None:
                row = conn.execute("SELECT MAX(rec_id) FROM record").fetchone()
                self._last_id = row[0] or 0
                conn.close()
                self.set_watermark(str(self._last_id))
                log.info("[%s] first run — skipping existing notifications, tracking new only", self.name)
                return

            cur = conn.execute(
                """SELECT rec_id, app_id, delivered_date, data
                   FROM record
                   WHERE rec_id > ?
                   ORDER BY rec_id""",
                (self._last_id,),
            )

            events = []
            max_id = self._last_id
            for rec_id, app_id, delivered_date, data in cur:
                content = ""
                if data:
                    try:
                        plist = plistlib.loads(data)
                        # Extract notification body from the plist
                        if isinstance(plist, dict):
                            req = plist.get("req", {})
                            if isinstance(req, dict):
                                content = str(req.get("body", ""))[:200]
                    except Exception:
                        pass

                events.append(Event(
                    table="notification_events",
                    columns=["timestamp", "app_name", "content_preview", "response_latency_s"],
                    values=(delivered_date or time.time(), app_id or "", content, 0),
                ))
                max_id = max(max_id, rec_id)

            conn.close()

            if events:
                self.buffer.push_many(events)
                self._last_id = max_id
                self.set_watermark(str(max_id))
                log.info("[%s] collected %d notifications", self.name, len(events))
        except sqlite3.OperationalError:
            log.warning("notification DB query failed (schema may have changed on this macOS version)")
        finally:
            Path(tmp).unlink(missing_ok=True)
