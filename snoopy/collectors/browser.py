"""Browser history collector — Chrome, Arc, Safari, Firefox.

Each browser stores history in a SQLite DB with different timestamp epochs:
- Chrome/Arc: microseconds since 1601-01-01
- Firefox: microseconds since Unix epoch
- Safari: seconds since 2001-01-01

We copy the DB before reading because Chrome/Arc hold a write lock.
A per-browser watermark (last_visit_id) avoids re-importing old visits.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

# Strip leading notification count prefix from browser titles: "(1) " → ""
_NOTIF_COUNT_RE = re.compile(r"^\(\d+\)\s+")

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
        self._collect_chromium_searches("chrome", config.CHROME_HISTORY)
        self._collect_chromium_searches("arc", config.ARC_HISTORY)
        self._collect_chromium_downloads("chrome", config.CHROME_HISTORY)
        self._collect_chromium_downloads("arc", config.ARC_HISTORY)
        self._collect_bookmarks("chrome", config.CHROME_BOOKMARKS)
        self._collect_bookmarks("arc", config.ARC_BOOKMARKS)

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
                log.info(
                    "[%s] first run — skipping %s history, tracking new visits only",
                    self.name, browser,
                )
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
                # Strip notification count prefix: "(3) Gmail" → "Gmail"
                if title:
                    title = _NOTIF_COUNT_RE.sub("", title)
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

    def _collect_chromium_searches(self, browser: str, db_path: Path) -> None:
        """Collect search terms from Chrome/Arc keyword_search_terms table."""
        if not db_path.exists():
            return

        watermark_key = f"{self.name}_{browser}_search"
        last_url_id = self.db.get_watermark(watermark_key)

        tmp = self._copy_db(db_path)
        if tmp is None:
            return

        try:
            conn = sqlite3.connect(tmp)

            # Table may not exist in fresh profiles
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='keyword_search_terms'"
            ).fetchone()
            if not has_table:
                conn.close()
                return

            if last_url_id is None:
                row = conn.execute("SELECT MAX(url_id) FROM keyword_search_terms").fetchone()
                max_id = row[0] or 0
                conn.close()
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info(
                    "[%s] first run — skipping %s search terms, tracking new only",
                    self.name, browser,
                )
                return

            cur = conn.execute(
                """SELECT k.url_id, k.term, u.url, v.visit_time
                   FROM keyword_search_terms k
                   JOIN urls u ON k.url_id = u.id
                   LEFT JOIN visits v ON v.url = u.id
                   WHERE k.url_id > ?
                   GROUP BY k.url_id
                   ORDER BY k.url_id""",
                (int(last_url_id),),
            )
            events = []
            max_id = int(last_url_id)
            for url_id, term, url, visit_time in cur:
                if visit_time:
                    ts = (visit_time - _CHROME_EPOCH_OFFSET) / 1_000_000
                else:
                    ts = time.time()
                events.append(Event(
                    table="search_events",
                    columns=["timestamp", "term", "browser", "url"],
                    values=(ts, term, browser, url),
                ))
                max_id = max(max_id, url_id)
            conn.close()

            if events:
                self.buffer.push_many(events)
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info("[%s] collected %d search terms from %s", self.name, len(events), browser)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _collect_chromium_downloads(self, browser: str, db_path: Path) -> None:
        """Collect file downloads from Chrome/Arc downloads table."""
        if not db_path.exists():
            return

        watermark_key = f"{self.name}_{browser}_downloads"
        last_id = self.db.get_watermark(watermark_key)

        tmp = self._copy_db(db_path)
        if tmp is None:
            return

        try:
            conn = sqlite3.connect(tmp)

            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='downloads'"
            ).fetchone()
            if not has_table:
                conn.close()
                return

            if last_id is None:
                row = conn.execute("SELECT MAX(id) FROM downloads").fetchone()
                max_id = row[0] or 0
                conn.close()
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info(
                    "[%s] first run — skipping %s downloads, tracking new only",
                    self.name, browser,
                )
                return

            cur = conn.execute(
                """SELECT id, target_path, tab_url, total_bytes,
                          start_time, mime_type
                   FROM downloads
                   WHERE id > ?
                   ORDER BY id""",
                (int(last_id),),
            )
            events = []
            max_id = int(last_id)
            for row in cur:
                dl_id, target_path, tab_url, total_bytes, start_time, mime_type = row
                ts = (start_time - _CHROME_EPOCH_OFFSET) / 1_000_000
                events.append(Event(
                    table="download_events",
                    columns=["timestamp", "file_path", "source_url",
                             "total_bytes", "mime_type", "browser"],
                    values=(ts, target_path, tab_url, total_bytes,
                            mime_type, browser),
                ))
                max_id = max(max_id, dl_id)
            conn.close()

            if events:
                self.buffer.push_many(events)
                self.db.set_watermark(watermark_key, str(max_id), time.time())
                log.info("[%s] collected %d downloads from %s", self.name, len(events), browser)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _collect_bookmarks(self, browser: str, bookmarks_path: Path) -> None:
        """Collect new bookmarks from Chrome/Arc Bookmarks JSON."""
        if not bookmarks_path.exists():
            return

        watermark_key = f"{self.name}_{browser}_bookmarks"
        last_date_added = self.db.get_watermark(watermark_key)

        try:
            data = json.loads(bookmarks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        # Walk the tree and collect all URL bookmarks
        bookmarks: list[tuple[str, str, str, str]] = []  # (date_added, url, name, folder)

        def walk(node: dict, folder: str = "") -> None:
            if node.get("type") == "url":
                bookmarks.append((
                    node.get("date_added", "0"),
                    node.get("url", ""),
                    node.get("name", ""),
                    folder,
                ))
            for child in node.get("children", []):
                child_folder = node.get("name", folder) if node.get("type") == "folder" else folder
                walk(child, child_folder)

        for root in data.get("roots", {}).values():
            if isinstance(root, dict):
                walk(root)

        if last_date_added is None:
            # First run: set watermark to max date_added
            max_date = max((b[0] for b in bookmarks), default="0")
            self.db.set_watermark(watermark_key, max_date, time.time())
            log.info(
                "[%s] first run — indexed %d %s bookmarks, tracking new only",
                self.name, len(bookmarks), browser,
            )
            return

        events = []
        max_date = last_date_added
        for date_added, url, name, folder in bookmarks:
            if date_added <= last_date_added:
                continue
            ts = (int(date_added) - _CHROME_EPOCH_OFFSET) / 1_000_000
            events.append(Event(
                table="bookmark_events",
                columns=["timestamp", "url", "title", "folder", "browser"],
                values=(ts, url, name, folder, browser),
            ))
            if date_added > max_date:
                max_date = date_added

        if events:
            self.buffer.push_many(events)
            self.db.set_watermark(watermark_key, max_date, time.time())
            log.info("[%s] collected %d new bookmarks from %s", self.name, len(events), browser)

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
                log.info(
                    "[%s] first run — skipping safari history, tracking new visits only",
                    self.name,
                )
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
                log.info(
                    "[%s] first run — skipping firefox history, tracking new visits only",
                    self.name,
                )
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
