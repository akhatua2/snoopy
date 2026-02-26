# Mail Collector Implementation Plan

## Goal
Track Apple Mail: from first run, seed with last 1 day of emails; thereafter only new emails (incremental).

---

## 1. Existing Codebase Patterns

### Collectors (Messages, Notifications)
- **BaseCollector**: `name`, `interval`, `collect()`, `setup()`, `teardown()`
- **Watermark**: `get_watermark(collector_name)` / `set_watermark(name, value)` stored in `collector_state`
- **First run**: Messages/Notifications set watermark to MAX(id) and **skip** historical
- **Incremental**: `WHERE id > watermark`
- **DB copy**: Copy Envelope Index to temp before read (avoid locking)
- **Event**: `Event(table, columns, values)` pushed to `buffer.push_many()`

### Our difference
- **First run**: Seed with last 1 day (not skip)
- **Incremental**: Same as others

---

## 2. Schema Additions

### `mail_events` table
```sql
CREATE TABLE IF NOT EXISTS mail_events (
    id INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    message_id INTEGER NOT NULL,       -- Mail messages.ROWID (for dedup/correlation)
    mailbox TEXT NOT NULL,             -- Inbox, Sent Items, Trash, etc.
    sender TEXT,
    subject TEXT,
    content_preview TEXT,
    is_from_me INTEGER,                -- 1 if Sent, 0 if received
    read INTEGER,
    deleted INTEGER,
    flagged INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mail_ts ON mail_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_mail_message_id ON mail_events(message_id);
```
- Add `mail_events` to `_VALID_TABLES` in `db.py`

---

## 3. Config

### `config.py`
```python
MAIL_INTERVAL = 60              # Poll every 60s (similar to notifications)
MAIL_SEED_DAYS = 1              # On first run, seed with last N days
```
### Paths
```python
MAIL_BASE = Path("~/Library/Mail").expanduser()
# Envelope Index: MAIL_BASE / "V10" / "MailData" / "Envelope Index"
```

---

## 4. Collector Logic

### `MailCollector.collect()` flow

```
1. Find Envelope Index (V10/MailData/Envelope Index)
2. Copy to temp + WAL/SHM
3. Connect, run queries

4. FIRST RUN (watermark is None):
   a. cutoff = now - (MAIL_SEED_DAYS * 86400)
   b. SELECT messages WHERE date_received >= cutoff
   c. For each: build Event, push
   d. Set watermark = MAX(ROWID) of all messages in DB
   e. Log "first run — seeded N messages from last 1 day"
   f. Return

5. INCREMENTAL (watermark is set):
   a. SELECT messages WHERE ROWID > watermark
   b. For each: build Event, push
   c. Set watermark = max(watermark, max new ROWID)
   d. Log "collected N new messages"
```

### Query
**First run** (`date_received >= cutoff`):
```sql
SELECT m.ROWID, m.date_received, m.read, m.deleted, m.flagged,
       m.mailbox, a.address, sub.subject
FROM messages m
LEFT JOIN subjects sub ON m.subject = sub.ROWID
LEFT JOIN addresses a ON m.sender = a.ROWID
WHERE m.date_received >= ?
ORDER BY m.ROWID
```

**Incremental** (`ROWID > watermark`):
```sql
SELECT m.ROWID, m.date_received, ...
FROM messages m
...
WHERE m.ROWID > ?
ORDER BY m.ROWID
```

### Mailbox name resolution
- Join `mailboxes` on `m.mailbox = mailboxes.ROWID`
- Extract name from `url`: split on `/`, take last part, `urldecode` (e.g. `Sent%20Items` → `Sent Items`)

### `is_from_me`
- If mailbox URL contains `Sent` or `Sent%20Items` → 1, else 0

### Content preview
- From DB: `subjects.subject` only (no body on first pass)
- Optional Phase 2: resolve .emlx path, parse body (200 chars)

---

## 5. File Structure

### New file
` snoopy/collectors/mail.py`

### Shared helpers (from scripts)
- `_find_envelope_index()` — locate Envelope Index
- `_copy_mail_db()` — copy to temp
- `_mailbox_name_from_url(url)` — parse "Inbox", "Sent Items"
- Mailbox map: build once per collect from mailboxes table

---

## 6. Implementation Steps

| Step | Task |
|------|------|
| 1 | Add `mail_events` table to `db.py` and `_VALID_TABLES` |
| 2 | Add `MAIL_INTERVAL`, `MAIL_SEED_DAYS` to `config.py` |
| 3 | Create `snoopy/collectors/mail.py` with `MailCollector` |
| 4 | Implement first-run: `cutoff`, seed query, set watermark to MAX(ROWID) |
| 5 | Implement incremental: `WHERE ROWID > watermark` |
| 6 | Register `MailCollector` in `daemon.py` ALL_COLLECTORS |
| 7 | Add "Mail" to Full Disk Access note in `install.sh` |
| 8 | Test: first run seeds, second run only new |

---

## 7. Edge Cases

- **PermissionError**: Log once, skip (like Messages)
- **No Envelope Index**: Log debug, return
- **Schema change**: `sqlite3.OperationalError` → log warning, continue
- **Empty seed**: If no messages in last 1 day, still set watermark to MAX(ROWID)
- **date_received format**: Unix timestamp (confirmed from exploration)

---

## 8. Future (Phase 2)
- Body preview from .emlx (need ROWID/global_message_id → file path mapping)
- Read/deleted change detection (re-poll for `read`, `deleted` changes)
- Threading (In-Reply-To, References)
