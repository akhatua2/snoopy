#!/usr/bin/env python3
"""Read the latest few emails from Apple Mail .emlx files.
Run with Full Disk Access (Terminal in Privacy settings).
"""

import email
import re
from pathlib import Path

MAIL_BASE = Path("~/Library/Mail").expanduser()
NUM_EMAILS = 5


def parse_emlx(path: Path) -> dict | None:
    """Parse .emlx or .partial.emlx. Returns dict with subject, from, date, body_preview."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    email_part = ""
    # Strategy 1: Find where RFC822 starts (must be after any plist; not inside XML)
    for marker in (b"\nFrom:", b"\nSubject:", b"\nContent-Type:", b"\nMessage-ID:"):
        idx = raw.find(marker)
        if idx >= 0 and idx < len(raw) - 50:
            start = raw.rfind(b"\n", 0, idx) + 1 if idx > 0 else 0
            decoded = raw[start:].decode("utf-8", errors="replace")
            if not decoded.lstrip().startswith("<") and (
                "From:" in decoded or "Content-Type:" in decoded
            ):
                email_part = decoded
                break
    # Strategy 2: Classic .emlx - first line = plist byte length, then plist, then email
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
    # Strategy 3: Plist first, email after </plist>
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

    subject = msg.get("Subject", "") or ""
    from_ = msg.get("From", "") or ""
    date = msg.get("Date", "") or ""
    if not subject and not from_:
        return None

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")

    # Strip to first ~300 chars of body
    body = re.sub(r"\s+", " ", body.strip())[:300]
    if len(body) >= 300:
        body = body + "..."

    return {"subject": subject, "from": from_, "date": date, "body_preview": body}


def main() -> None:
    if not MAIL_BASE.exists():
        print("~/Library/Mail not found")
        return

    emlx_files = []
    for ext in (".emlx", ".partial.emlx"):
        emlx_files.extend(MAIL_BASE.rglob(f"*{ext}"))
    emlx_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    print(f"Latest {NUM_EMAILS} emails (by file mtime):\n")
    shown = 0
    for path in emlx_files:
        if shown >= NUM_EMAILS:
            break
        parsed = parse_emlx(path)
        if not parsed:
            continue
        shown += 1
        print(f"--- [{shown}] {path.relative_to(MAIL_BASE)} ---")
        print(f"From: {parsed['from']}")
        print(f"Subject: {parsed['subject']}")
        print(f"Date: {parsed['date']}")
        preview = parsed["body_preview"]
        print(f"Preview: {preview}")
        print()


if __name__ == "__main__":
    main()
