"""Thread-safe event buffer that flushes to SQLite in batches."""

import logging
import threading
from dataclasses import dataclass

import snoopy.config as config
from snoopy.db import Database

log = logging.getLogger(__name__)


@dataclass
class Event:
    """A single collected event destined for a DB table."""
    table: str
    columns: list[str]
    values: tuple


class EventBuffer:
    """Accumulates events from collector threads and flushes them to the DB."""

    def __init__(self, db: Database):
        self._db = db
        self._lock = threading.Lock()
        self._events: list[Event] = []

    def push(self, event: Event):
        with self._lock:
            self._events.append(event)
            if len(self._events) >= config.BUFFER_MAX_SIZE:
                self._flush_locked()

    def push_many(self, events: list[Event]):
        with self._lock:
            self._events.extend(events)
            if len(self._events) >= config.BUFFER_MAX_SIZE:
                self._flush_locked()

    def flush(self):
        """Flush all pending events to the database."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        """Must be called while holding self._lock."""
        if not self._events:
            return
        # Group by table for batch inserts
        by_table: dict[str, tuple[list[str], list[tuple]]] = {}
        for ev in self._events:
            key = ev.table
            if key not in by_table:
                by_table[key] = (ev.columns, [])
            by_table[key][1].append(ev.values)

        count = len(self._events)
        self._events.clear()

        for table, (columns, rows) in by_table.items():
            try:
                self._db.batch_insert(table, columns, rows)
            except Exception:
                log.exception("flush failed for table %s (%d rows)", table, len(rows))

        log.debug("flushed %d events across %d tables", count, len(by_table))
