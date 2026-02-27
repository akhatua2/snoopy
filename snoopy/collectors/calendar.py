"""Calendar collector — reads upcoming events via CalendarHelper.app.

Uses a compiled Swift helper app that has EventKit TCC permissions.
The helper is launched via `open -a` to inherit its app bundle's
Calendar access, writes JSON to a temp file, and we parse it.

Maintains a "living calendar" in calendar_events (always reflects current state)
and logs all changes to calendar_changes:
  - added: new event first seen
  - modified: field changed (title, time, location, etc.)
  - removed: event disappeared from fetch within its time window
"""

import json
import logging
import os
import subprocess
import tempfile
import time

import snoopy.config as config
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_TRACKED_FIELDS = [
    "title", "calendar_name", "end_time", "location",
    "attendees", "is_all_day", "is_recurring",
]


def _fetch_events(helper_app: str) -> list[dict]:
    """Launch CalendarHelper.app and read events JSON from temp file."""
    out_path = os.path.join(tempfile.gettempdir(), "snoopy_calendar.json")

    # Remove stale output
    try:
        os.unlink(out_path)
    except FileNotFoundError:
        pass

    result = subprocess.run(
        ["open", "-a", str(helper_app), "--args", "events", out_path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.warning("[calendar] helper launch failed: %s", result.stderr.strip())
        return []

    # Wait for the helper to write the file (it runs async via `open`)
    for _ in range(40):
        time.sleep(0.5)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 2:
            break
    else:
        log.warning("[calendar] helper timed out — no output file")
        return []

    try:
        with open(out_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[calendar] failed to parse output: %s", e)
        return []


class CalendarCollector(BaseCollector):
    name = "calendar"
    interval = config.CALENDAR_INTERVAL

    def setup(self) -> None:
        self._helper_app = str(config.CALENDAR_HELPER)

    def collect(self) -> None:
        events = _fetch_events(self._helper_app)
        if not events:
            return

        now = time.time()
        conn = self.db._ensure_conn()

        with self.db._lock:
            self._sync_events(conn, events, now)
            conn.commit()

    def _sync_events(self, conn, events: list[dict], now: float) -> None:
        # Load all active events from DB
        cur = conn.execute(
            "SELECT id, event_uid, start_time, title, calendar_name, "
            "end_time, location, attendees, is_all_day, is_recurring "
            "FROM calendar_events WHERE status = 'active'"
        )
        db_events = {}
        for row in cur.fetchall():
            key = (row[1], row[2])  # (event_uid, start_time)
            db_events[key] = {
                "id": row[0], "title": row[3], "calendar_name": row[4],
                "end_time": row[5], "location": row[6], "attendees": row[7],
                "is_all_day": row[8], "is_recurring": row[9],
            }

        seen_keys = set()
        added = 0
        modified = 0

        for ev in events:
            uid = ev.get("uid", "")
            start = ev.get("start", "")
            if not uid or not start:
                continue

            key = (uid, start)
            seen_keys.add(key)
            attendees = ", ".join(ev.get("attendees", []))

            new_vals = {
                "title": ev.get("title", ""),
                "calendar_name": ev.get("calendar", ""),
                "end_time": ev.get("end", ""),
                "location": ev.get("location", ""),
                "attendees": attendees,
                "is_all_day": int(ev.get("all_day", False)),
                "is_recurring": int(ev.get("recurring", False)),
            }

            if key not in db_events:
                conn.execute(
                    "INSERT OR IGNORE INTO calendar_events "
                    "(timestamp, event_uid, title, calendar_name, start_time, "
                    "end_time, location, attendees, is_all_day, is_recurring, "
                    "first_seen, last_seen, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                    (now, uid, new_vals["title"], new_vals["calendar_name"],
                     start, new_vals["end_time"], new_vals["location"],
                     new_vals["attendees"], new_vals["is_all_day"],
                     new_vals["is_recurring"], now, now),
                )
                conn.execute(
                    "INSERT INTO calendar_changes "
                    "(timestamp, event_uid, title, change_type) "
                    "VALUES (?, ?, ?, 'added')",
                    (now, uid, new_vals["title"]),
                )
                added += 1
            else:
                existing = db_events[key]
                changes = self._diff_fields(existing, new_vals)

                if changes:
                    conn.execute(
                        "UPDATE calendar_events SET title=?, calendar_name=?, "
                        "end_time=?, location=?, attendees=?, is_all_day=?, "
                        "is_recurring=?, last_seen=? WHERE id=?",
                        (new_vals["title"], new_vals["calendar_name"],
                         new_vals["end_time"], new_vals["location"],
                         new_vals["attendees"], new_vals["is_all_day"],
                         new_vals["is_recurring"], now, existing["id"]),
                    )
                    for field, old_v, new_v in changes:
                        conn.execute(
                            "INSERT INTO calendar_changes "
                            "(timestamp, event_uid, title, change_type, "
                            "field_name, old_value, new_value) "
                            "VALUES (?, ?, ?, 'modified', ?, ?, ?)",
                            (now, uid, new_vals["title"], field, old_v, new_v),
                        )
                    modified += 1
                else:
                    conn.execute(
                        "UPDATE calendar_events SET last_seen=? WHERE id=?",
                        (now, existing["id"]),
                    )

        # Detect removals — only within the fetch window
        removed = self._mark_removals(conn, db_events, seen_keys, now)

        if added or modified or removed:
            log.info(
                "[%s] +%d added, ~%d modified, -%d removed (%d total fetched)",
                self.name, added, modified, removed, len(events),
            )

    def _diff_fields(self, existing: dict, new_vals: dict) -> list[tuple]:
        """Compare tracked fields, return list of (field, old_str, new_str)."""
        changes = []
        for field in _TRACKED_FIELDS:
            old_val = existing.get(field)
            new_val = new_vals[field]
            if old_val is None:
                old_val = "" if isinstance(new_val, str) else 0
            if old_val != new_val:
                changes.append((field, str(old_val), str(new_val)))
        return changes

    def _mark_removals(self, conn, db_events: dict, seen_keys: set,
                       now: float) -> int:
        """Mark active events as removed if they're within the helper's
        fetch window (-1 day to +7 days) but weren't returned."""
        # Helper fetches from now-1day to now+7days; use ISO strings for comparison
        from datetime import datetime, timedelta, timezone
        window_start = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(days=1)
        window_end = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(days=7)
        window_min = window_start.strftime("%Y-%m-%dT%H:%M:%S")
        window_max = window_end.strftime("%Y-%m-%dT%H:%M:%S")

        removed = 0
        for key, existing in db_events.items():
            if key in seen_keys:
                continue
            # Only remove if start_time is within the fetch window
            if window_min <= key[1] <= window_max:
                conn.execute(
                    "UPDATE calendar_events SET status='removed', last_seen=? "
                    "WHERE id=?",
                    (now, existing["id"]),
                )
                conn.execute(
                    "INSERT INTO calendar_changes "
                    "(timestamp, event_uid, title, change_type) "
                    "VALUES (?, ?, ?, 'removed')",
                    (now, key[0], existing["title"]),
                )
                removed += 1
        return removed
