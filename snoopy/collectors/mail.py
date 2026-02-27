"""Apple Mail collector — tracks emails via Envelope Index.

Reads from ~/Library/Mail/V*/MailData/Envelope Index (requires Full Disk Access).
Copy-before-read to avoid locking issues with Mail.app.

First run: seeds with the last N days of emails (configurable via MAIL_SEED_DAYS).
Subsequent runs: incremental via ROWID watermark.
"""

import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_MAIL_BASE = Path("~/Library/Mail").expanduser()
_CONTENT_PREVIEW_LEN = 200

_QUERY_COLUMNS = """
    m.ROWID, m.date_received, m.read, m.deleted, m.flagged,
    m.mailbox, sub.subject, a.address
"""

_QUERY_JOINS = """
    FROM messages m
    LEFT JOIN subjects sub ON m.subject = sub.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
"""


def _find_envelope_index() -> Path | None:
    """Locate the Envelope Index DB under ~/Library/Mail."""
    if not _MAIL_BASE.exists():
        return None
    try:
        versions = [p for p in _MAIL_BASE.iterdir() if p.is_dir() and p.name.startswith("V")]
    except OSError:
        return None
    for v in sorted(versions, key=lambda p: p.name, reverse=True):
        maildata = v / "MailData"
        if not maildata.exists():
            continue
        for name in ("Envelope Index", "Envelope Index.db"):
            p = maildata / name
            if p.exists():
                return p
    return None


def _copy_mail_db(src: Path) -> str | None:
    """Copy Envelope Index + WAL/SHM to a temp file. Returns temp path or None."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        shutil.copy2(str(src), tmp)
        for suffix in ("-wal", "-shm"):
            wal = str(src) + suffix
            if os.path.exists(wal):
                shutil.copy2(wal, tmp + suffix)
        return tmp
    except (OSError, shutil.Error):
        Path(tmp).unlink(missing_ok=True)
        return None


def _mailbox_name_from_url(url: str | None) -> str:
    """Extract human-readable mailbox name from the mailbox url column.

    e.g. 'imap://user@imap.gmail.com/Sent%20Items' -> 'Sent Items'
    """
    if not url:
        return ""
    parts = url.rstrip("/").split("/")
    return unquote(parts[-1]) if parts else ""


def _is_sent(mailbox_name: str) -> int:
    """Heuristic: is this a sent-mail folder?"""
    lower = mailbox_name.lower()
    return 1 if ("sent" in lower) else 0


class MailCollector(BaseCollector):
    name = "mail"
    interval = config.MAIL_INTERVAL

    def setup(self) -> None:
        self._last_id: int | None = None
        saved = self.get_watermark()
        if saved is not None:
            self._last_id = int(saved)
        self._permission_warned = False

    def collect(self) -> None:
        idx_path = _find_envelope_index()
        if idx_path is None:
            log.debug("Envelope Index not found — Mail may not be set up")
            return

        tmp = _copy_mail_db(idx_path)
        if tmp is None:
            if not self._permission_warned:
                log.warning("Mail Envelope Index needs Full Disk Access — skipping until granted")
                self._permission_warned = True
            return

        try:
            conn = sqlite3.connect(tmp)

            # Build mailbox ROWID -> name map
            mailbox_map = {}
            try:
                for rowid, url in conn.execute("SELECT ROWID, url FROM mailboxes"):
                    mailbox_map[rowid] = _mailbox_name_from_url(url)
            except sqlite3.OperationalError:
                pass

            if self._last_id is None:
                self._first_run(conn, mailbox_map)
            else:
                self._incremental(conn, mailbox_map)

            conn.close()
        except sqlite3.OperationalError:
            log.warning("Mail DB query failed (schema may differ on this macOS version)")
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _first_run(self, conn: sqlite3.Connection, mailbox_map: dict[int, str]) -> None:
        """Seed with last MAIL_SEED_DAYS of emails, then set watermark to MAX(ROWID)."""
        cutoff = time.time() - (config.MAIL_SEED_DAYS * 86400)

        cur = conn.execute(
            f"SELECT {_QUERY_COLUMNS} {_QUERY_JOINS} WHERE m.date_received >= ? ORDER BY m.ROWID",
            (cutoff,),
        )

        events = self._rows_to_events(cur, mailbox_map)

        # Always set watermark to MAX(ROWID) — even if no recent messages
        row = conn.execute("SELECT MAX(ROWID) FROM messages").fetchone()
        max_id = row[0] or 0

        if events:
            self.buffer.push_many(events)

        self._last_id = max_id
        self.set_watermark(str(max_id))
        log.info(
            "[%s] first run — seeded %d messages from last %d day(s)",
            self.name, len(events), config.MAIL_SEED_DAYS,
        )

    def _incremental(self, conn: sqlite3.Connection, mailbox_map: dict[int, str]) -> None:
        """Fetch messages with ROWID > watermark."""
        cur = conn.execute(
            f"SELECT {_QUERY_COLUMNS} {_QUERY_JOINS} WHERE m.ROWID > ? ORDER BY m.ROWID",
            (self._last_id,),
        )

        events = []
        assert self._last_id is not None
        max_id = self._last_id
        for rowid, date_received, read, deleted, flagged, mailbox_id, subject, sender in cur:
            ts = date_received if date_received else time.time()
            mailbox_name = mailbox_map.get(mailbox_id, "")
            is_from_me = _is_sent(mailbox_name)
            content_preview = (subject or "")[:_CONTENT_PREVIEW_LEN]

            events.append(Event(
                table="mail_events",
                columns=["timestamp", "message_id", "mailbox", "sender", "subject",
                         "content_preview", "is_from_me", "read", "deleted", "flagged"],
                values=(ts, rowid, mailbox_name, sender or "", subject or "",
                        content_preview, is_from_me, read or 0, deleted or 0, flagged or 0),
            ))
            max_id = max(max_id, rowid)

        if events:
            self.buffer.push_many(events)
            self._last_id = max_id
            self.set_watermark(str(max_id))
            log.info("[%s] collected %d new messages", self.name, len(events))

    def _rows_to_events(self, cur: sqlite3.Cursor, mailbox_map: dict[int, str]) -> list[Event]:
        """Convert query rows to Event objects."""
        events = []
        for rowid, date_received, read, deleted, flagged, mailbox_id, subject, sender in cur:
            ts = date_received if date_received else time.time()
            mailbox_name = mailbox_map.get(mailbox_id, "")
            is_from_me = _is_sent(mailbox_name)
            content_preview = (subject or "")[:_CONTENT_PREVIEW_LEN]

            events.append(Event(
                table="mail_events",
                columns=["timestamp", "message_id", "mailbox", "sender", "subject",
                         "content_preview", "is_from_me", "read", "deleted", "flagged"],
                values=(ts, rowid, mailbox_name, sender or "", subject or "",
                        content_preview, is_from_me, read or 0, deleted or 0, flagged or 0),
            ))
        return events
