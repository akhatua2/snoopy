#!/usr/bin/env python3
"""One-off script to explore Apple Mail Envelope Index schema.
Run with Full Disk Access: System Settings > Privacy & Security > Full Disk Access > add Terminal.
Then: python scripts/explore_mail_schema.py
"""

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

MAIL_BASE = Path("~/Library/Mail").expanduser()


def find_envelope_index() -> Path | None:
    try:
        top_level = list(MAIL_BASE.iterdir())
    except OSError:
        print("Permission denied — grant Full Disk Access to Terminal")
        return None

    print("~/Library/Mail contents:", [p.name for p in sorted(top_level)])
    versions = [p for p in top_level if p.is_dir() and p.name.startswith("V")]
    if not versions:
        print("No V* version folders — Mail may use a different structure or not be set up")
        return None

    for v in sorted(versions, key=lambda p: p.name, reverse=True):
        maildata = v / "MailData"
        if not maildata.exists():
            continue
        for name in ("Envelope Index", "Envelope Index.db"):
            p = maildata / name
            if p.exists():
                return p

    # Fallback: search for SQLite files under MailData
    for v in sorted(versions, key=lambda p: p.name, reverse=True):
        maildata = v / "MailData"
        for f in maildata.iterdir() if maildata.exists() else []:
            if f.is_file() and (
                f.suffix in (".db", ".sqlite") or "Envelope" in f.name or "envelope" in f.name
            ):
                print(f"Found candidate: {f}")
                return f
    return None


def main() -> None:
    if not MAIL_BASE.exists():
        print("~/Library/Mail not found (Mail may not be set up)")
        return
    idx = find_envelope_index()
    if not idx:
        print("Envelope Index not found")
        return
    print(f"Found: {idx}\n")

    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        shutil.copy2(str(idx), tmp)
        for suffix in ("-wal", "-shm"):
            src = str(idx) + suffix
            if Path(src).exists():
                shutil.copy2(src, tmp + suffix)
    except PermissionError:
        print("Permission denied — grant Full Disk Access to Terminal")
        return
    except OSError as e:
        print(f"Copy failed: {e}")
        return

    conn = sqlite3.connect(tmp)
    cur = conn.cursor()

    print("=== TABLES ===")
    for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(f"  {row[0]}")

    print("\n=== SCHEMA (key tables) ===")
    for table in ("message", "mailbox", "message_data", "messages"):
        try:
            cur.execute(f"PRAGMA table_info({table})")
            rows = cur.fetchall()
            if rows:
                print(f"\n{table}:")
                for r in rows:
                    print(f"  {r[1]} {r[2]}")
        except sqlite3.OperationalError:
            pass

    # Try common table names across Mail versions
    for table in ("message", "messages", "envelope", "Message"):
        try:
            cur.execute(f"SELECT * FROM {table} LIMIT 1")
            cols = [d[0] for d in cur.description]
            print(f"\n=== SAMPLE: {table} columns ===")
            print("  ", cols)
            cur.execute(f"SELECT * FROM {table} LIMIT 1")
            row = cur.fetchone()
            if row:
                for k, v in zip(cols, row):
                    val = str(v)[:60] + "..." if v and len(str(v)) > 60 else v
                    print(f"    {k}: {val}")
            break
        except sqlite3.OperationalError:
            continue

    # Probe content tables: subjects, senders, searchable_messages, generated_summaries
    print("\n=== CONTENT: subjects ===")
    try:
        cur.execute("PRAGMA table_info(subjects)")
        print("  ", [r[1] for r in cur.fetchall()])
        cur.execute("SELECT * FROM subjects LIMIT 2")
        for row in cur.fetchall():
            print("  ", row[:3] if len(row) > 3 else row)
    except sqlite3.OperationalError:
        print("  (table not found)")

    print("\n=== CONTENT: senders (sample) ===")
    try:
        cur.execute("SELECT * FROM senders LIMIT 2")
        for row in cur.fetchall():
            print("  ", row)
    except sqlite3.OperationalError:
        print("  (table not found)")

    print("\n=== CONTENT: searchable_messages ===")
    try:
        cur.execute("PRAGMA table_info(searchable_messages)")
        cols = [r[1] for r in cur.fetchall()]
        print("  columns:", cols)
        cur.execute("SELECT * FROM searchable_messages LIMIT 1")
        row = cur.fetchone()
        if row:
            for k, v in zip(cols, row):
                if v and isinstance(v, (str, bytes)):
                    preview = str(v)[:200] + "..." if len(str(v)) > 200 else v
                    print(f"    {k}: {preview}")
                else:
                    print(f"    {k}: {v}")
    except sqlite3.OperationalError as e:
        print("  ", e)

    print("\n=== CONTENT: generated_summaries ===")
    try:
        cur.execute("PRAGMA table_info(generated_summaries)")
        print("  ", [r[1] for r in cur.fetchall()])
        cur.execute("SELECT * FROM generated_summaries LIMIT 1")
        row = cur.fetchone()
        if row:
            print("  sample:", str(row)[:300])
    except sqlite3.OperationalError:
        print("  (table not found)")

    print("\n=== FULL MESSAGE SAMPLE (subject + sender) ===")
    try:
        cur.execute("""
            SELECT m.ROWID, datetime(m.date_received, 'unixepoch'),
                   sub.subject, se.ROWID as sender_id
            FROM messages m
            LEFT JOIN subjects sub ON m.subject = sub.ROWID
            LEFT JOIN senders se ON m.sender = se.ROWID
            ORDER BY m.date_received DESC LIMIT 1
        """)
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        print("  cols:", cols, "\n  row:", row)
    except sqlite3.OperationalError as e:
        print("  ", e)

    print("\n=== ADDRESSES (sender email) ===")
    try:
        cur.execute("PRAGMA table_info(addresses)")
        print("  addresses:", [r[1] for r in cur.fetchall()])
        cur.execute("PRAGMA table_info(sender_addresses)")
        print("  sender_addresses:", [r[1] for r in cur.fetchall()])
        cur.execute("SELECT * FROM addresses LIMIT 2")
        print("  addresses sample:", cur.fetchall())
        cur.execute("SELECT * FROM sender_addresses LIMIT 2")
        print("  sender_addresses sample:", cur.fetchall())
    except sqlite3.OperationalError as e:
        print("  ", e)

    print("\n=== BODY: where is it? ===")
    try:
        cur.execute("PRAGMA table_info(message_metadata)")
        print("  message_metadata:", [r[1] for r in cur.fetchall()])
        cur.execute("SELECT * FROM message_metadata LIMIT 1")
        print("  sample:", cur.fetchone())
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%'")
        print("  FTS tables (full-text):", [r[0] for r in cur.fetchall()])
    except sqlite3.OperationalError:
        pass

    print("\n=== MAIL FOLDER STRUCTURE (sample) ===")
    try:
        paths = sorted(p for p in MAIL_BASE.rglob("*") if p.is_file())
        for p in paths[:50]:
            print(f"  {p.relative_to(MAIL_BASE)} ({p.stat().st_size} b)")
        if len(paths) > 50:
            print(f"  ... +{len(paths) - 50} more files")
    except Exception as e:
        print("  ", e)

    print("\n=== FILE TYPES (body candidates) ===")
    try:
        exts = {}
        for path in MAIL_BASE.rglob("*"):
            if path.is_file():
                ext = path.suffix or "(no ext)"
                exts[ext] = exts.get(ext, 0) + 1
        for ext, count in sorted(exts.items(), key=lambda x: -x[1]):
            print(f"  {ext}: {count}")
    except Exception as e:
        print("  ", e)

    print("\n=== document_id (points to body?) ===")
    try:
        cur.execute("SELECT ROWID, document_id FROM messages WHERE document_id IS NOT NULL LIMIT 5")
        for row in cur.fetchall():
            print("  ", row)
    except sqlite3.OperationalError:
        pass

    print("\n=== message_metadata with content ===")
    try:
        cur.execute(
            "SELECT message_id, length(json_values),"
            " substr(json_values,1,200)"
            " FROM message_metadata"
            " WHERE json_values IS NOT NULL LIMIT 3"
        )
        for row in cur.fetchall():
            print("  ", row)
    except sqlite3.OperationalError:
        pass

    print("\n=== SAMPLE: any file that looks like email body ===")
    try:
        for path in MAIL_BASE.rglob("*"):
            if not path.is_file() or path.stat().st_size < 100 or path.stat().st_size > 500_000:
                continue
            ext = path.suffix.lower()
            if (
                ext not in (".emlx", ".eml", ".partial", ".plist")
                and "eml" not in path.name.lower()
            ):
                continue
            preview = path.read_text(errors="replace")[:500]
            if "From:" in preview or "Subject:" in preview or "Content-Type:" in preview:
                print(f"  {path.relative_to(MAIL_BASE)}")
                print(f"    {preview[:300]}...")
                break
        else:
            print("  No obvious email-format files found in sampled paths")
    except Exception as e:
        print("  ", e)

    conn.close()
    Path(tmp).unlink(missing_ok=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
