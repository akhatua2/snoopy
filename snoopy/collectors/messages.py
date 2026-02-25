"""iMessage collector — tracks sent and received messages via chat.db.

Reads from ~/Library/Messages/chat.db (requires Full Disk Access).
Copy-before-read to avoid locking issues with Messages.app.
Tracks ROWID watermark for incremental reads.

macOS Messages date format: nanoseconds since 2001-01-01.
On modern macOS, message text is often in attributedBody (NSArchiver blob)
rather than the plain text column.
"""

import logging
import os
import plistlib
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)

_MESSAGES_DB = Path("~/Library/Messages/chat.db").expanduser()
_APPLE_EPOCH_OFFSET = 978307200  # seconds between 2001-01-01 and 1970-01-01
_CONTENT_PREVIEW_LEN = 200


def _extract_text_from_attributed_body(blob: bytes) -> str:
    """Extract plain text from the NSArchiver attributedBody blob.

    The blob is a typedstream (NSArchiver) format. The message text appears
    after the NSString class marker, with a length byte prefix:
      ...NSString\x01\x94\x84\x01+\x05Rahul...
                                  ^len ^text
    The \x01+ is a type marker, then a length byte, then UTF-8 text.
    """
    if not blob:
        return ""
    try:
        marker = b"NSString"
        idx = blob.find(marker)
        if idx == -1:
            return ""
        # Skip past: NSString \x01 \x94 \x84 \x01 + <length_byte> <text>
        # Find the \x01+ sequence after NSString
        search_start = idx + len(marker)
        plus_idx = blob.find(b"\x01+", search_start)
        if plus_idx == -1:
            return ""
        length_offset = plus_idx + 2
        if length_offset >= len(blob):
            return ""
        text_len = blob[length_offset]
        text_start = length_offset + 1
        text_bytes = blob[text_start:text_start + text_len]
        return text_bytes.decode("utf-8", errors="replace")
    except Exception:
        return ""


class MessagesCollector(BaseCollector):
    name = "messages"
    interval = config.MESSAGES_INTERVAL

    def setup(self) -> None:
        self._last_id: int | None = None
        saved = self.get_watermark()
        if saved is not None:
            self._last_id = int(saved)
        self._permission_warned = False

    def collect(self) -> None:
        if not _MESSAGES_DB.exists():
            return

        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(str(_MESSAGES_DB), tmp)
            # Also copy WAL/SHM so we see recent uncheckpointed writes
            for suffix in ("-wal", "-shm"):
                src = str(_MESSAGES_DB) + suffix
                if os.path.exists(src):
                    shutil.copy2(src, tmp + suffix)
        except PermissionError:
            if not self._permission_warned:
                log.warning("Messages chat.db needs Full Disk Access — skipping until granted")
                self._permission_warned = True
            return
        except (OSError, shutil.Error):
            log.exception("failed to copy Messages chat.db")
            return

        try:
            conn = sqlite3.connect(tmp)

            # First run: skip historical messages
            if self._last_id is None:
                row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
                self._last_id = row[0] or 0
                conn.close()
                self.set_watermark(str(self._last_id))
                log.info("[%s] first run — skipping existing messages, tracking new only", self.name)
                return

            cur = conn.execute(
                """SELECT m.ROWID, m.text, m.is_from_me, m.date, m.service,
                          m.cache_has_attachments, h.id,
                          COALESCE(c.display_name, c.chat_identifier, h.id, ''),
                          m.attributedBody, m.destination_caller_id
                   FROM message m
                   LEFT JOIN handle h ON m.handle_id = h.ROWID
                   LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                   LEFT JOIN chat c ON cmj.chat_id = c.ROWID
                   WHERE m.ROWID > ?
                   ORDER BY m.ROWID""",
                (self._last_id,),
            )

            events = []
            max_id = self._last_id
            for rowid, text, is_from_me, date, service, has_attach, handle_id, chat_name, attr_body, dest_caller in cur:

                # Convert Apple nanosecond timestamp to Unix epoch
                ts = date / 1_000_000_000 + _APPLE_EPOCH_OFFSET if date else time.time()

                content = (text or "")[:_CONTENT_PREVIEW_LEN]
                if not content:
                    content = _extract_text_from_attributed_body(attr_body)[:_CONTENT_PREVIEW_LEN]
                if not content and has_attach:
                    content = "[attachment]"

                contact = handle_id or dest_caller or ""
                events.append(Event(
                    table="message_events",
                    columns=["timestamp", "contact", "is_from_me", "content_preview",
                             "has_attachment", "service", "chat_name"],
                    values=(ts, contact, is_from_me or 0, content,
                            has_attach or 0, service or "", chat_name or ""),
                ))
                max_id = max(max_id, rowid)

            conn.close()

            if events:
                self.buffer.push_many(events)
                self._last_id = max_id
                self.set_watermark(str(max_id))
                log.info("[%s] collected %d messages", self.name, len(events))
        except sqlite3.OperationalError:
            log.warning("Messages DB query failed (schema may differ on this macOS version)")
        finally:
            Path(tmp).unlink(missing_ok=True)
