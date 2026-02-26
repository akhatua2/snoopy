#!/usr/bin/env python3
"""Collect all available Mail data for last 2 days and report what we actually found.
Run with Full Disk Access (Terminal in Privacy settings).
"""
import email
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

MAIL_BASE = Path("~/Library/Mail").expanduser()
SECONDS_PER_DAY = 86400


def find_envelope_index() -> Path | None:
    try:
        versions = [p for p in MAIL_BASE.iterdir() if p.is_dir() and p.name.startswith("V")]
    except OSError:
        return None
    for v in sorted(versions, key=lambda p: p.name, reverse=True):
        for name in ("Envelope Index", "Envelope Index.db"):
            p = v / "MailData" / name
            if p.exists():
                return p
    return None


def copy_db(idx: Path) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy2(str(idx), tmp)
    for suffix in ("-wal", "-shm"):
        if Path(str(idx) + suffix).exists():
            shutil.copy2(str(idx) + suffix, tmp + suffix)
    return tmp


def parse_emlx_full(path: Path) -> dict | None:
    """Parse .emlx and extract all available headers and body."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    email_part = ""
    for marker in (b"\nFrom:", b"\nSubject:", b"\nContent-Type:", b"\nMessage-ID:"):
        idx = raw.find(marker)
        if idx >= 0 and idx < len(raw) - 50:
            start = raw.rfind(b"\n", 0, idx) + 1 if idx > 0 else 0
            decoded = raw[start:].decode("utf-8", errors="replace")
            if not decoded.lstrip().startswith("<") and ("From:" in decoded or "Content-Type:" in decoded):
                email_part = decoded
                break
    if not email_part:
        first_newline = raw.find(b"\n")
        if first_newline > 0:
            try:
                plist_len = int(raw[:first_newline].decode().strip())
                if 0 < plist_len < 100_000:
                    email_start = first_newline + 1 + plist_len
                    if email_start < len(raw):
                        candidate = raw[email_start:].decode("utf-8", errors="replace")
                        if "From:" in candidate or "Content-Type:" in candidate:
                            email_part = candidate
            except ValueError:
                pass
    if not email_part:
        plist_end = raw.find(b"</plist>")
        if plist_end >= 0:
            after = plist_end + len(b"</plist>")
            while after < len(raw) and raw[after : after + 1] in (b"\n", b"\r"):
                after += 1
            if after < len(raw):
                candidate = raw[after:].decode("utf-8", errors="replace")
                if "From:" in candidate or "Content-Type:" in candidate:
                    email_part = candidate

    if not email_part:
        return None

    try:
        msg = email.message_from_string(email_part)
    except Exception:
        return None

    result = {
        "From": msg.get("From", ""),
        "To": msg.get("To", ""),
        "Cc": msg.get("Cc", ""),
        "Bcc": msg.get("Bcc", ""),
        "Subject": msg.get("Subject", ""),
        "Date": msg.get("Date", ""),
        "Message-ID": msg.get("Message-ID", ""),
        "In-Reply-To": msg.get("In-Reply-To", ""),
        "References": msg.get("References", ""),
        "Reply-To": msg.get("Reply-To", ""),
    }

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")

    result["body_preview"] = re.sub(r"\s+", " ", body.strip())[:400]
    result["attachments"] = []
    for part in msg.walk():
        if part.get("Content-Disposition"):
            fn = part.get_filename()
            result["attachments"].append({"filename": fn, "content_type": part.get_content_type()})

    return result


def main() -> None:
    if not MAIL_BASE.exists():
        print("~/Library/Mail not found")
        return

    idx = find_envelope_index()
    if not idx:
        print("Envelope Index not found")
        return

    cutoff = time.time() - 2 * SECONDS_PER_DAY
    tmp = copy_db(idx)
    conn = sqlite3.connect(tmp)

    print("=" * 60)
    print("MAIL DATA REPORT — last 2 days")
    print("=" * 60)

    # 1. Messages from DB
    cur = conn.cursor()
    cur.execute("""
        SELECT m.ROWID, m.global_message_id, m.date_received, m.read, m.deleted, m.flagged,
               m.mailbox, m.sender, m.subject, m.summary,
               sub.subject as subject_text
        FROM messages m
        LEFT JOIN subjects sub ON m.subject = sub.ROWID
        WHERE m.date_received >= ?
        ORDER BY m.date_received DESC
    """, (cutoff,))
    db_rows = cur.fetchall()
    col_names = [d[0] for d in cur.description]

    print(f"\n[DB] Messages (date_received >= 2 days ago): {len(db_rows)}")

    # 2. Mailboxes - resolve ROWID to human name (Inbox, Sent, etc.)
    mailbox_map = {}
    try:
        cur.execute("PRAGMA table_info(mailboxes)")
        mb_cols = [r[1] for r in cur.fetchall()]
        for row in cur.execute("SELECT ROWID, * FROM mailboxes"):
            vals = dict(zip(["ROWID"] + mb_cols, [row[0]] + list(row[1:])))
            name = vals.get("display_name") or vals.get("name") or vals.get("path") or vals.get("url")
            if name and isinstance(name, str):
                if "/" in str(name):
                    name = str(name).split("/")[-1]
                name = str(name).replace(".mbox", "").strip() or None
            mailbox_map[row[0]] = name if name else f"mailbox_{row[0]}"
    except sqlite3.OperationalError:
        pass

    # 3. Sender addresses (messages.sender = addresses.ROWID in Apple Mail)
    sender_map = {}
    try:
        cur.execute("""
            SELECT m.ROWID, a.address
            FROM messages m
            LEFT JOIN addresses a ON m.sender = a.ROWID
            WHERE m.date_received >= ? AND m.sender IS NOT NULL
        """, (cutoff,))
        for r in cur.fetchall():
            if r[1]:
                sender_map[r[0]] = r[1]
    except sqlite3.OperationalError:
        pass

    # 4. Recipients
    recipients_found = []
    try:
        cur.execute("PRAGMA table_info(recipients)")
        rec_cols = [r[1] for r in cur.fetchall()]
        msg_col = "message_id" if "message_id" in rec_cols else "message"
        addr_col = "address" if "address" in rec_cols else "address_id"
        cur.execute(f"""
            SELECT r.{msg_col}, a.address
            FROM recipients r
            LEFT JOIN addresses a ON r.{addr_col} = a.ROWID
            WHERE r.{msg_col} IN (SELECT ROWID FROM messages WHERE date_received >= ?)
        """, (cutoff,))
        recipients_found = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # 5. Attachments
    attachments_found = []
    try:
        cur.execute("PRAGMA table_info(attachments)")
        att_cols = [r[1] for r in cur.fetchall()]
        msg_col = "message_id" if "message_id" in att_cols else "message"
        fn_col = "filename" if "filename" in att_cols else "name"
        cur.execute(f"""
            SELECT {msg_col}, {fn_col}
            FROM attachments
            WHERE {msg_col} IN (SELECT ROWID FROM messages WHERE date_received >= ?)
        """, (cutoff,))
        attachments_found = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # 6. Labels
    labels_found = []
    try:
        cur.execute("""
            SELECT m.ROWID, l.name
            FROM messages m
            JOIN local_message_actions lma ON m.ROWID = lma.message_id
            JOIN labels l ON lma.label_id = l.ROWID
            WHERE m.date_received >= ?
        """, (cutoff,))
        labels_found = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # 7. Generated summaries
    summaries_found = []
    try:
        cur.execute("""
            SELECT m.ROWID, gs.ROWID
            FROM messages m
            JOIN generated_summaries gs ON m.summary = gs.ROWID
            WHERE m.date_received >= ? AND gs.summary IS NOT NULL
        """, (cutoff,))
        summaries_found = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    conn.close()
    Path(tmp).unlink(missing_ok=True)

    # 8. .emlx files from last 2 days
    emlx_files = [p for p in MAIL_BASE.rglob("*.emlx") if p.stat().st_mtime >= cutoff]
    emlx_files += [p for p in MAIL_BASE.rglob("*.partial.emlx") if p.stat().st_mtime >= cutoff]
    emlx_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    print(f"[FILES] .emlx modified in last 2 days: {len(emlx_files)}")

    # Parse each emlx
    emlx_data = []
    for path in emlx_files:
        parsed = parse_emlx_full(path)
        if parsed:
            parsed["_path"] = str(path.relative_to(MAIL_BASE))
            parsed["_mailbox"] = "?"
            for part in path.parts:
                if part.endswith(".mbox"):
                    parsed["_mailbox"] = part.replace(".mbox", "")
                    break
            emlx_data.append(parsed)

    # Report
    print("\n" + "-" * 60)
    print("DETAILED FINDINGS")
    print("-" * 60)

    print("\n► FROM ENVELOPE INDEX (DB):")
    print(f"  • Subject (from subjects join): {sum(1 for r in db_rows if r[10])} / {len(db_rows)}")
    db_senders = len([v for v in sender_map.values() if v])
    sender_note = f"{db_senders} from DB" if db_senders >= len(db_rows) else f"{db_senders} from DB, rest from .emlx fallback"
    print(f"  • Sender address: {sender_note}")
    print(f"  • Mailbox names: {len(mailbox_map)} mailboxes in map")
    print(f"  • Recipients (recipients table): {len(recipients_found)} rows")
    print(f"  • Attachments (attachments table): {len(attachments_found)} rows")
    print(f"  • Labels: {len(labels_found)} rows")
    print(f"  • Generated summaries: {len(summaries_found)} rows")

    print("\n► FROM .emlx FILES:")
    if emlx_data:
        sample = emlx_data[0]
        print(f"  • Parsed: {len(emlx_data)} files")
        print(f"  • From: {sum(1 for e in emlx_data if e.get('From'))} / {len(emlx_data)}")
        print(f"  • To: {sum(1 for e in emlx_data if e.get('To'))} / {len(emlx_data)}")
        print(f"  • Cc: {sum(1 for e in emlx_data if e.get('Cc'))} / {len(emlx_data)}")
        print(f"  • Bcc: {sum(1 for e in emlx_data if e.get('Bcc'))} / {len(emlx_data)}")
        print(f"  • Subject: {sum(1 for e in emlx_data if e.get('Subject'))} / {len(emlx_data)}")
        print(f"  • Date: {sum(1 for e in emlx_data if e.get('Date'))} / {len(emlx_data)}")
        print(f"  • Message-ID: {sum(1 for e in emlx_data if e.get('Message-ID'))} / {len(emlx_data)}")
        print(f"  • In-Reply-To (threading): {sum(1 for e in emlx_data if e.get('In-Reply-To'))} / {len(emlx_data)}")
        print(f"  • References (threading): {sum(1 for e in emlx_data if e.get('References'))} / {len(emlx_data)}")
        print(f"  • Body preview: {sum(1 for e in emlx_data if e.get('body_preview'))} / {len(emlx_data)}")
        print(f"  • Attachments (from MIME): {sum(len(e.get('attachments', [])) for e in emlx_data)} total")
    else:
        print("  • No .emlx files parsed (all failed or none in date range)")

    # Build emlx lookup for sender fallback: (subject_normalized) -> From
    def _norm(s):
        return re.sub(r"\s+", " ", (s or "").strip())[:80]

    emlx_by_subject = {}
    for e in emlx_data:
        subj = _norm(e.get("Subject"))
        if subj:
            emlx_by_subject[subj] = e.get("From", "")

    print("\n► SAMPLE MESSAGES (first 5 from DB):")
    for i, row in enumerate(db_rows[:5]):
        rid, gid, ts, read, deleted, flagged, mb_id, sender_id, sub_id, summ_id, sub_text = row
        mb_name = mailbox_map.get(mb_id, f"mailbox_{mb_id}")
        sender = sender_map.get(rid, "")
        if not sender and sub_text:
            sender = emlx_by_subject.get(_norm(sub_text), "")
        print(f"\n  [{i+1}] ROWID={rid} mailbox={mb_name} read={read} deleted={deleted} flagged={flagged}")
        print(f"      subject: {sub_text or '(none)'}")
        print(f"      sender: {sender or '(none)'}")

    print("\n► SAMPLE .emlx (first 3 parsed):")
    for i, e in enumerate(emlx_data[:3]):
        print(f"\n  [{i+1}] {e.get('_mailbox', '?')} — {e.get('_path', '')[:60]}...")
        print(f"      From: {e.get('From', '')[:60]}")
        print(f"      To: {e.get('To', '')[:60] if e.get('To') else '(none)'}")
        print(f"      Subject: {(e.get('Subject') or '')[:50]}")
        print(f"      Message-ID: {(e.get('Message-ID') or '')[:50] or '(none)'}")
        print(f"      In-Reply-To: {(e.get('In-Reply-To') or '')[:40] or '(none)'}")
        print(f"      Attachments: {e.get('attachments', [])}")
        print(f"      Body: {(e.get('body_preview') or '')[:100]}...")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
