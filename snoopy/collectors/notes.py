"""Apple Notes collector — tracks new and modified notes via NoteStore.sqlite.

Reads from ~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite
(requires Full Disk Access). Copy-before-read to avoid locking issues.
Tracks modification timestamp watermark for incremental reads.

Full note content is extracted from ZICNOTEDATA.ZDATA which is a
gzip-compressed protobuf blob. We decompress and parse the wire format
to extract the plain text body.

macOS Notes date format: seconds since 2001-01-01 (Core Data / Apple epoch).
"""

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_NOTES_DB = Path(
    "~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
).expanduser()
_APPLE_EPOCH_OFFSET = 978307200  # seconds between 2001-01-01 and 1970-01-01

_QUERY = """
SELECT c1.ZIDENTIFIER,
       c1.ZTITLE1,
       c1.ZMODIFICATIONDATE1,
       c1.ZCREATIONDATE3,
       c2.ZTITLE2,
       c5.ZNAME,
       n.ZDATA
FROM ZICCLOUDSYNCINGOBJECT c1
LEFT JOIN ZICCLOUDSYNCINGOBJECT c2 ON c2.Z_PK = c1.ZFOLDER
LEFT JOIN ZICCLOUDSYNCINGOBJECT c5 ON c5.Z_PK = c1.ZACCOUNT2
LEFT JOIN ZICNOTEDATA n ON c1.ZNOTEDATA = n.Z_PK
WHERE c1.ZTITLE1 IS NOT NULL
  AND c1.ZMODIFICATIONDATE1 > ?
ORDER BY c1.ZMODIFICATIONDATE1
"""


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a protobuf varint at the given position."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            break
    return result, pos


def _extract_strings(data: bytes, results: list[str], depth: int = 0) -> None:
    """Recursively extract string fields from protobuf wire format."""
    if depth > 5:
        return
    pos = 0
    while pos < len(data):
        try:
            tag, new_pos = _decode_varint(data, pos)
        except (IndexError, ValueError):
            break
        wire_type = tag & 0x07
        field_num = tag >> 3
        if field_num == 0 or field_num > 10000:
            break
        pos = new_pos

        if wire_type == 0:  # varint
            try:
                _, pos = _decode_varint(data, pos)
            except (IndexError, ValueError):
                break
        elif wire_type == 2:  # length-delimited (string or nested message)
            try:
                length, pos = _decode_varint(data, pos)
            except (IndexError, ValueError):
                break
            if length < 0 or pos + length > len(data):
                break
            chunk = data[pos:pos + length]
            pos += length
            # Try as UTF-8 string
            try:
                text = chunk.decode("utf-8")
                if len(text) > 0 and all(
                    c.isprintable() or c in "\n\r\t" for c in text
                ):
                    results.append(text)
                    continue
            except UnicodeDecodeError:
                pass
            # Try as nested protobuf message
            _extract_strings(chunk, results, depth + 1)
        elif wire_type == 1:  # 64-bit fixed
            pos += 8
        elif wire_type == 5:  # 32-bit fixed
            pos += 4
        else:
            break


def _scan_raw_text(data: bytes) -> str:
    """Scan raw bytes for the longest run of printable UTF-8 text.

    Falls back to this when protobuf parsing fails to extract meaningful text,
    which happens when Apple Notes stores content in formats where raw text
    bytes get misinterpreted as protobuf field tags.
    """
    best = ""
    pos = 0
    while pos < len(data):
        # Find start of a printable text run
        run_start = pos
        chars: list[str] = []
        while pos < len(data):
            b = data[pos]
            # Determine UTF-8 byte length
            if b < 0x80:
                char_len = 1
            elif b < 0xC0:
                break  # continuation byte without start — end of run
            elif b < 0xE0:
                char_len = 2
            elif b < 0xF0:
                char_len = 3
            elif b < 0xF8:
                char_len = 4
            else:
                break
            if pos + char_len > len(data):
                break
            try:
                ch = data[pos:pos + char_len].decode("utf-8")
            except UnicodeDecodeError:
                break
            if not (ch.isprintable() or ch in "\n\r\t"):
                break
            chars.append(ch)
            pos += char_len
        text = "".join(chars)
        if len(text) > len(best):
            best = text
        pos = max(pos + 1, run_start + 1)
    return best.strip()


def extract_note_text(zdata: bytes | None) -> str:
    """Extract full plain text from Apple Notes ZDATA (gzip-compressed protobuf)."""
    if not zdata:
        return ""
    try:
        decompressed = gzip.decompress(zdata)
    except Exception:
        return ""

    # Try structured protobuf extraction first
    strings: list[str] = []
    _extract_strings(decompressed, strings)
    best_proto = ""
    for s in strings:
        if len(s) > len(best_proto):
            best_proto = s

    # If protobuf extraction got a decent ratio of text vs blob size, use it.
    # Otherwise fall back to raw byte scanning — some Apple Notes store text
    # in formats where raw text bytes get misinterpreted as protobuf field
    # tags, yielding only tiny fragments from large blobs.
    if len(best_proto) < len(decompressed) // 4:
        raw = _scan_raw_text(decompressed)
        if len(raw) > len(best_proto):
            return raw

    return best_proto


class NotesCollector(BaseCollector):
    name = "notes"
    interval = config.NOTES_INTERVAL

    def setup(self) -> None:
        self._last_mod: float | None = None
        saved = self.get_watermark()
        if saved is not None:
            self._last_mod = float(saved)
        self._permission_warned = False

    def collect(self) -> None:
        if not _NOTES_DB.exists():
            return

        try:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            shutil.copy2(str(_NOTES_DB), tmp)
            for suffix in ("-wal", "-shm"):
                src = str(_NOTES_DB) + suffix
                if os.path.exists(src):
                    shutil.copy2(src, tmp + suffix)
        except PermissionError:
            if not self._permission_warned:
                log.warning(
                    "Notes NoteStore.sqlite needs Full Disk Access — skipping"
                )
                self._permission_warned = True
            return
        except (OSError, shutil.Error):
            log.exception("failed to copy NoteStore.sqlite")
            return

        try:
            conn = sqlite3.connect(tmp)

            if self._last_mod is None:
                # First run: seed with notes from the last N days
                cutoff = time.time() - (config.NOTES_SEED_DAYS * 86400)
                cutoff_apple = cutoff - _APPLE_EPOCH_OFFSET
                cur = conn.execute(_QUERY, (cutoff_apple,))
            else:
                cur = conn.execute(_QUERY, (self._last_mod,))

            events = []
            max_mod = self._last_mod or 0.0
            for row in cur:
                note_id, title, mod_date, create_date, folder, account, zdata = row
                if not mod_date:
                    continue
                ts = mod_date + _APPLE_EPOCH_OFFSET
                content = extract_note_text(zdata) if zdata else ""
                event_type = "created" if self._last_mod is None else "modified"
                events.append(Event(
                    table="note_events",
                    columns=["timestamp", "note_id", "title", "content",
                             "folder", "account", "event_type"],
                    values=(ts, note_id or "", title or "", content,
                            folder or "", account or "", event_type),
                ))
                max_mod = max(max_mod, mod_date)

            conn.close()

            if events:
                self.buffer.push_many(events)
                log.info("[%s] collected %d note events", self.name, len(events))

            if max_mod > (self._last_mod or 0.0):
                self._last_mod = max_mod
                self.set_watermark(str(max_mod))
            elif self._last_mod is None:
                # No notes found but mark as initialized
                self._last_mod = time.time() - _APPLE_EPOCH_OFFSET
                self.set_watermark(str(self._last_mod))
                log.info("[%s] first run — no recent notes, tracking new only", self.name)
        except sqlite3.OperationalError:
            log.warning("Notes DB query failed (schema may differ on this macOS version)")
        finally:
            Path(tmp).unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(tmp + suffix).unlink(missing_ok=True)
