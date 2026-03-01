"""Reminders collector — tracks Apple Reminders via CalendarHelper.app.

Uses the same CalendarHelper Swift helper as the calendar collector,
invoking its `reminders` command. Tracks reminder state and emits
events for new, completed, and modified reminders.
"""

import json
import logging
import subprocess
import tempfile
import time

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


def _fetch_reminders(helper_app: str) -> list[dict]:
    """Run CalendarHelper binary directly to fetch reminders JSON."""
    out_path = f"{tempfile.gettempdir()}/snoopy_reminders.json"
    binary = f"{helper_app}/Contents/MacOS/calendar_helper"

    try:
        result = subprocess.run(
            [binary, "reminders", out_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log.warning("[reminders] helper failed: %s", result.stderr.strip())
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("[reminders] helper binary not found or timed out")
        return []

    try:
        with open(out_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("[reminders] failed to parse output: %s", e)
        return []


class RemindersCollector(BaseCollector):
    name = "reminders"
    interval = config.REMINDERS_INTERVAL

    def setup(self) -> None:
        self._helper_app = str(config.CALENDAR_HELPER)
        self._known: dict[str, dict] = {}
        saved = self.get_watermark()
        if saved:
            try:
                self._known = json.loads(saved)
            except (json.JSONDecodeError, TypeError):
                pass
        self._initialized = bool(self._known)

    def collect(self) -> None:
        reminders = _fetch_reminders(self._helper_app)
        if not reminders:
            return

        now = time.time()

        if not self._initialized:
            # First run: record current state without emitting events
            for r in reminders:
                uid = r.get("uid", "")
                if uid:
                    self._known[uid] = {
                        "title": r.get("title", ""),
                        "completed": r.get("completed", False),
                        "list": r.get("list", ""),
                    }
            self.set_watermark(json.dumps(self._known))
            self._initialized = True
            log.info(
                "[%s] first run — indexed %d reminders, tracking changes only",
                self.name, len(self._known),
            )
            return

        events = []
        current_uids = set()

        for r in reminders:
            uid = r.get("uid", "")
            if not uid:
                continue
            current_uids.add(uid)
            title = r.get("title", "")
            completed = r.get("completed", False)
            list_name = r.get("list", "")
            due_date = r.get("due_date", "")

            prev = self._known.get(uid)
            if prev is None:
                # New reminder
                events.append(self._make_event(
                    now, uid, title, list_name, completed, due_date, "added",
                ))
            else:
                if completed and not prev.get("completed"):
                    events.append(self._make_event(
                        now, uid, title, list_name, completed, due_date, "completed",
                    ))
                elif title != prev.get("title") or list_name != prev.get("list"):
                    events.append(self._make_event(
                        now, uid, title, list_name, completed, due_date, "modified",
                    ))

            self._known[uid] = {
                "title": title, "completed": completed, "list": list_name,
            }

        # Detect removals
        for uid in list(self._known):
            if uid not in current_uids:
                prev = self._known.pop(uid)
                events.append(self._make_event(
                    now, uid, prev.get("title", ""), prev.get("list", ""),
                    prev.get("completed", False), "", "removed",
                ))

        if events:
            self.buffer.push_many(events)
            log.info("[%s] %d reminder changes", self.name, len(events))

        self.set_watermark(json.dumps(self._known))

    def _make_event(self, ts, uid, title, list_name, completed, due_date,
                    event_type) -> Event:
        return Event(
            table="reminder_events",
            columns=["timestamp", "reminder_uid", "title", "list_name",
                     "completed", "due_date", "event_type"],
            values=(ts, uid, title, list_name, int(completed),
                    due_date, event_type),
        )
