"""Tests for Apple Notes collector — verifies NoteStore.sqlite parsing + content extraction."""

import gzip
import sqlite3
import time

import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.notes import NotesCollector, _APPLE_EPOCH_OFFSET, extract_note_text, _scan_raw_text
from snoopy.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


def _make_note_protobuf(text: str) -> bytes:
    """Create a minimal gzip-compressed protobuf containing note text.

    Wire format: field 2 (Note message) containing field 2 (text string).
    """
    # Encode the text as a protobuf string field (field 2, wire type 2)
    text_bytes = text.encode("utf-8")
    inner = _encode_field(2, text_bytes)
    # Wrap in outer field 2 (the Note message)
    outer = _encode_field(2, inner)
    return gzip.compress(outer)


def _encode_varint(value: int) -> bytes:
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _encode_field(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2  # wire type 2 = length-delimited
    return _encode_varint(tag) + _encode_varint(len(data)) + data


def _create_fake_notes_db(path, notes: list[dict]) -> None:
    """Create a minimal NoteStore.sqlite with ZICCLOUDSYNCINGOBJECT + ZICNOTEDATA."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ZICCLOUDSYNCINGOBJECT ("
        "  Z_PK INTEGER PRIMARY KEY,"
        "  ZIDENTIFIER TEXT,"
        "  ZTITLE1 TEXT,"
        "  ZTITLE2 TEXT,"
        "  ZMODIFICATIONDATE1 REAL,"
        "  ZCREATIONDATE3 REAL,"
        "  ZFOLDER INTEGER,"
        "  ZACCOUNT2 INTEGER,"
        "  ZNAME TEXT,"
        "  ZNOTEDATA INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE ZICNOTEDATA ("
        "  Z_PK INTEGER PRIMARY KEY,"
        "  ZNOTE INTEGER,"
        "  ZDATA BLOB"
        ")"
    )
    for note in notes:
        note_data_pk = note.get("data_pk")
        conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT "
            "(Z_PK, ZIDENTIFIER, ZTITLE1, ZTITLE2, "
            " ZMODIFICATIONDATE1, ZCREATIONDATE3, ZFOLDER, ZACCOUNT2, ZNAME, ZNOTEDATA) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note["pk"], note.get("identifier", f"note-{note['pk']}"),
                note.get("title"), note.get("title2"),
                note.get("mod_date"), note.get("create_date"),
                note.get("folder"), note.get("account"),
                note.get("name"), note_data_pk,
            ),
        )
        if note_data_pk and note.get("content"):
            zdata = _make_note_protobuf(note["content"])
            conn.execute(
                "INSERT INTO ZICNOTEDATA (Z_PK, ZNOTE, ZDATA) VALUES (?, ?, ?)",
                (note_data_pk, note["pk"], zdata),
            )
    conn.commit()
    conn.close()


class TestExtractNoteText:
    def test_extracts_text_from_protobuf(self):
        zdata = _make_note_protobuf("Hello, this is my full note content!")
        assert extract_note_text(zdata) == "Hello, this is my full note content!"

    def test_empty_data(self):
        assert extract_note_text(b"") == ""
        assert extract_note_text(None) == ""

    def test_invalid_gzip(self):
        assert extract_note_text(b"not gzip data") == ""

    def test_raw_text_fallback_for_nonstandard_protobuf(self):
        """When text bytes are not properly protobuf-delimited, raw scan finds them."""
        # Simulate Apple Notes format where raw text gets misinterpreted as
        # protobuf tags: protobuf framing + raw text bytes without proper
        # length-delimited string encoding.
        long_text = "This is a long note with lots of content that should be extracted fully."
        # Wrap in minimal protobuf framing (field 1 varint + field 2 bytes)
        # but put raw text bytes directly inside without proper string field encoding
        inner = b"\x08\x00\x10\x00" + long_text.encode("utf-8") + b"\x00\x00"
        outer = _encode_field(2, _encode_field(3, inner))
        zdata = gzip.compress(outer)
        result = extract_note_text(zdata)
        assert long_text in result

    def test_scan_raw_text(self):
        """_scan_raw_text finds the longest printable UTF-8 run in raw bytes."""
        data = b"\x00\x01\x02Hello, World!\x00\x03Short\x00"
        assert _scan_raw_text(data) == "Hello, World!"

    def test_scan_raw_text_with_unicode(self):
        text = "✅ SUCCESS! MiniMax-M2.5 is RUNNING!"
        data = b"\x08\x00" + text.encode("utf-8") + b"\x00\xff"
        assert _scan_raw_text(data) == text


class TestNotesCollector:
    def test_first_run_seeds_recent_with_full_content(self, buf, db, tmp_path, monkeypatch):
        """First run seeds recent notes with full content from ZDATA."""
        fake_db = tmp_path / "NoteStore.sqlite"
        now_apple = time.time() - _APPLE_EPOCH_OFFSET
        old_apple = now_apple - 86400 * 30

        _create_fake_notes_db(fake_db, [
            {"pk": 1, "title": "Recent Note", "data_pk": 1,
             "content": "This is the full body of my note with all the details.",
             "mod_date": now_apple - 3600, "create_date": now_apple - 3600},
            {"pk": 2, "title": "Old Note", "data_pk": 2,
             "content": "Old content",
             "mod_date": old_apple, "create_date": old_apple},
            {"pk": 100, "title": None, "title2": "My Folder",
             "mod_date": now_apple, "create_date": now_apple},
        ])

        monkeypatch.setattr("snoopy.collectors.notes._NOTES_DB", fake_db)

        c = NotesCollector(buf, db)
        c.setup()

        # First run: should seed with recent note only, with full content
        c.collect()
        buf.flush()
        assert db.count("note_events") == 1
        row = db._conn.execute(
            "SELECT title, content, event_type FROM note_events"
        ).fetchone()
        assert row[0] == "Recent Note"
        assert row[1] == "This is the full body of my note with all the details."
        assert row[2] == "created"

    def test_modification_detected_with_updated_content(self, buf, db, tmp_path, monkeypatch):
        """Modifying a note should emit event with updated full content."""
        fake_db = tmp_path / "NoteStore.sqlite"
        now_apple = time.time() - _APPLE_EPOCH_OFFSET

        _create_fake_notes_db(fake_db, [
            {"pk": 1, "title": "My Note", "data_pk": 1,
             "content": "Original content",
             "mod_date": now_apple - 3600, "create_date": now_apple - 3600},
        ])

        monkeypatch.setattr("snoopy.collectors.notes._NOTES_DB", fake_db)

        c = NotesCollector(buf, db)
        c.setup()
        c.collect()  # first run
        buf.flush()

        # Update note content
        new_zdata = _make_note_protobuf("Updated content with more text here")
        conn = sqlite3.connect(str(fake_db))
        conn.execute(
            "UPDATE ZICCLOUDSYNCINGOBJECT SET ZTITLE1='My Note Updated', "
            "ZMODIFICATIONDATE1=? WHERE Z_PK=1",
            (now_apple,),
        )
        conn.execute(
            "UPDATE ZICNOTEDATA SET ZDATA=? WHERE Z_PK=1",
            (new_zdata,),
        )
        conn.commit()
        conn.close()

        c.collect()
        buf.flush()
        assert db.count("note_events") == 2
        row = db._conn.execute(
            "SELECT title, content, event_type FROM note_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "My Note Updated"
        assert row[1] == "Updated content with more text here"
        assert row[2] == "modified"
