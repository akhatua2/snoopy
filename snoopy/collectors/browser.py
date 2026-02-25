"""Browser history collector — Chrome, Arc, Safari, Firefox.

Each browser stores history in a SQLite DB with different timestamp epochs:
- Chrome/Arc: microseconds since 1601-01-01
- Firefox: microseconds since Unix epoch
- Safari: seconds since 2001-01-01

We copy the DB before reading because Chrome/Arc hold a write lock.
A per-browser watermark (last_visit_id) avoids re-importing old visits.
"""

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)

# Chrome epoch offset: microseconds between 1601-01-01 and 1970-01-01
_CHROME_EPOCH_OFFSET = 11644473600 * 1_000_000
# Safari epoch offset: seconds between 2001-01-01 and 1970-01-01
_SAFARI_EPOCH_OFFSET = 978307200


class BrowserCollector(BaseCollector):
    name = "browser"
    interval = config.BROWSER_INTERVAL

    def setup(self) -> None:
        self._permission_warned: set[str] = set()

    def collect(self) -> None:
        self._collect_chromium("chrome", config.CHROME_HISTORY)
        self._collect_chromium("arc", config.ARC_HISTORY)
        self._collect_safari()
        self._collect_firefox()

    def _collect_chromium(self, browser: str, db_path: Path) -> None:
        """Collect from Chrome or Arc (same Chromium schema)."""
        if not db_path.exists():
            return

        watermark_key = f"{self.name}_{browser}"
        last_id = self.db.get_watermark(watermark_key)

        tmp = self._copy_db(db_path)
        if tmp is None:
            return

        try:
            conn = sqlite3.connect(tmp)

            # First run: skip historical data, just set watermark to current max
            if last_id is None:
                row = conn.execute("SELECT MAX(id) FROM visits").fetchone()
                max_id = row[0] or 0
                conn.close()
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info("[%s] first run — skipping %s history, tracking new visits only", self.name, browser)
                return

            cur = conn.execute(
                """SELECT v.id, u.url, u.title, v.visit_time, v.visit_duration
                   FROM visits v JOIN urls u ON v.url = u.id
                   WHERE v.id > ?
                   ORDER BY v.id""",
                (int(last_id),),
            )
            events = []
            max_id = int(last_id)
            for row in cur:
                visit_id, url, title, visit_time, duration = row
                ts = (visit_time - _CHROME_EPOCH_OFFSET) / 1_000_000
                dur_s = duration / 1_000_000 if duration else 0
                events.append(Event(
                    table="browser_events",
                    columns=["timestamp", "url", "title", "browser", "visit_duration_s"],
                    values=(ts, url, title, browser, dur_s),
                ))
                max_id = max(max_id, visit_id)
            conn.close()

            if events:
                self.buffer.push_many(events)
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info("[%s] collected %d visits from %s", self.name, len(events), browser)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _collect_safari(self) -> None:
        if not config.SAFARI_HISTORY.exists():
            return

        watermark_key = f"{self.name}_safari"
        last_ts_str = self.db.get_watermark(watermark_key)

        tmp = self._copy_db(config.SAFARI_HISTORY)
        if tmp is None:
            return

        try:
            conn = sqlite3.connect(tmp)

            # First run: skip historical data
            if last_ts_str is None:
                row = conn.execute("SELECT MAX(visit_time) FROM history_visits").fetchone()
                max_ts = row[0] or 0
                conn.close()
                self.db.set_watermark(watermark_key, str(max_ts), time.time())
                log.info("[%s] first run — skipping safari history, tracking new visits only", self.name)
                return

            last_ts = float(last_ts_str)
            cur = conn.execute(
                """SELECT hi.url, hv.title, hv.visit_time
                   FROM history_visits hv
                   JOIN history_items hi ON hv.history_item = hi.id
                   WHERE hv.visit_time > ?
                   ORDER BY hv.visit_time""",
                (last_ts,),
            )
            events = []
            max_ts = last_ts
            for url, title, visit_time in cur:
                ts = visit_time + _SAFARI_EPOCH_OFFSET
                events.append(Event(
                    table="browser_events",
                    columns=["timestamp", "url", "title", "browser", "visit_duration_s"],
                    values=(ts, url, title or "", "safari", 0),
                ))
                max_ts = max(max_ts, visit_time)
            conn.close()

            if events:
                self.buffer.push_many(events)
                self.db.set_watermark(watermark_key, str(max_ts), time.time())
                log.info("[%s] collected %d visits from safari", self.name, len(events))
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _collect_firefox(self) -> None:
        if not config.FIREFOX_PROFILES.exists():
            return

        profiles = sorted(config.FIREFOX_PROFILES.glob("*.default*"))
        if not profiles:
            return
        places_db = profiles[0] / "places.sqlite"
        if not places_db.exists():
            return

        watermark_key = f"{self.name}_firefox"
        last_id = self.db.get_watermark(watermark_key)

        tmp = self._copy_db(places_db)
        if tmp is None:
            return

        try:
            conn = sqlite3.connect(tmp)

            # First run: skip historical data
            if last_id is None:
                row = conn.execute("SELECT MAX(id) FROM moz_historyvisits").fetchone()
                max_id = row[0] or 0
                conn.close()
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info("[%s] first run — skipping firefox history, tracking new visits only", self.name)
                return

            cur = conn.execute(
                """SELECT v.id, p.url, p.title, v.visit_date
                   FROM moz_historyvisits v
                   JOIN moz_places p ON v.place_id = p.id
                   WHERE v.id > ?
                   ORDER BY v.id""",
                (int(last_id),),
            )
            events = []
            max_id = int(last_id)
            for visit_id, url, title, visit_date in cur:
                ts = visit_date / 1_000_000
                events.append(Event(
                    table="browser_events",
                    columns=["timestamp", "url", "title", "browser", "visit_duration_s"],
                    values=(ts, url, title or "", "firefox", 0),
                ))
                max_id = max(max_id, visit_id)
            conn.close()

            if events:
                self.buffer.push_many(events)
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info("[%s] collected %d visits from firefox", self.name, len(events))
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _copy_db(self, src: Path) -> str | None:
        """Copy a locked SQLite DB to a temp file for safe reading."""
        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(str(src), tmp)
            return tmp
        except PermissionError:
            key = str(src)
            if key not in self._permission_warned:
                log.warning("%s needs Full Disk Access — skipping until granted", src)
                self._permission_warned.add(key)
            return None
        except (OSError, shutil.Error):
            log.exception("failed to copy db %s", src)
            return None
