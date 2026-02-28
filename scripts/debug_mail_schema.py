#!/usr/bin/env python3
"""Debug Mail DB schema — inspect senders, mailboxes, and joins."""

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

CUTOFF = int(time.time()) - 2 * 86400

MAIL_BASE = Path("~/Library/Mail").expanduser()


def find_envelope_index() -> Path | None:
    try:
        versions = [p for p in MAIL_BASE.iterdir() if p.is_dir() and p.name.startswith("V")]
    except OSError:
        return None
    for v in sorted(versions, key=lambda p: p.name, reverse=True):
        p = v / "MailData" / "Envelope Index"
        if p.exists():
            return p
    return None


def main() -> None:
    idx = find_envelope_index()
    if not idx:
        print("Envelope Index not found")
        return

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy2(str(idx), tmp)
    for suffix in ("-wal", "-shm"):
        if Path(str(idx) + suffix).exists():
            shutil.copy2(str(idx) + suffix, tmp + suffix)

    conn = sqlite3.connect(tmp)
    cur = conn.cursor()

    print("=== MAILBOXES ===")
    cur.execute("PRAGMA table_info(mailboxes)")
    print("Columns:", [r[1] for r in cur.fetchall()])
    cur.execute(
        "SELECT ROWID, * FROM mailboxes"
        " WHERE ROWID IN (53, 1, 2, 3, 4, 5)"
        " OR url LIKE '%Inbox%' OR url LIKE '%Sent%'"
    )
    for row in cur.fetchall():
        print("  ", row)

    print("\n=== SENDERS ===")
    cur.execute("PRAGMA table_info(senders)")
    print("Columns:", [r[1] for r in cur.fetchall()])
    cur.execute("SELECT ROWID, * FROM senders LIMIT 3")
    for row in cur.fetchall():
        print("  ", row)

    print("\n=== SENDER_ADDRESSES ===")
    cur.execute("PRAGMA table_info(sender_addresses)")
    print("Columns:", [r[1] for r in cur.fetchall()])
    cur.execute("SELECT * FROM sender_addresses LIMIT 5")
    for row in cur.fetchall():
        print("  ", row)

    print("\n=== MESSAGES: sender population ===")
    cur.execute("SELECT COUNT(*), COUNT(sender) FROM messages WHERE date_received >= ?", (CUTOFF,))
    print("  total, with sender:", cur.fetchone())

    print("\n=== JOIN TEST: messages.sender -> senders? ===")
    cur.execute("SELECT DISTINCT sender FROM messages WHERE date_received >= ? LIMIT 5", (CUTOFF,))
    sender_ids = [r[0] for r in cur.fetchall() if r[0]]
    print("  Sample messages.sender values:", sender_ids)
    cur.execute("SELECT MAX(ROWID) FROM senders")
    print("  senders max ROWID:", cur.fetchone()[0])
    cur.execute(
        """
        SELECT m.ROWID, m.sender, s.ROWID as s_rid, a.address
        FROM messages m
        LEFT JOIN senders s ON m.sender = s.ROWID
        LEFT JOIN sender_addresses sa ON s.ROWID = sa.sender
        LEFT JOIN addresses a ON sa.address = a.ROWID
        WHERE m.date_received >= ? AND m.sender IS NOT NULL
        LIMIT 5
    """,
        (CUTOFF,),
    )
    for row in cur.fetchall():
        print("  ", row)
    print("\n=== TRY: messages.sender = addresses.ROWID? ===")
    cur.execute(
        """
        SELECT m.ROWID, m.sender, a.address
        FROM messages m
        LEFT JOIN addresses a ON m.sender = a.ROWID
        WHERE m.date_received >= ? AND m.sender IS NOT NULL
        LIMIT 5
    """,
        (CUTOFF,),
    )
    for row in cur.fetchall():
        print("  ", row)

    print("\n=== RECIPIENTS (message, type, address) ===")
    cur.execute("PRAGMA table_info(recipients)")
    cols = [r[1] for r in cur.fetchall()]
    print("recipients columns:", cols)
    msg_col = "message_id" if "message_id" in cols else "message"
    cur.execute(
        f"""
        SELECT r.{msg_col}, r.type, a.address
        FROM recipients r
        LEFT JOIN addresses a ON r.address = a.ROWID
        WHERE r.{msg_col} IN (SELECT ROWID FROM messages WHERE date_received >= ? LIMIT 3)
    """,
        (CUTOFF,),
    )
    for row in cur.fetchall():
        print("  ", row)

    conn.close()
    Path(tmp).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
