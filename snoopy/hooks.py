"""Snoopy hook handlers — invoked by Claude Code via ~/.claude/settings.json.

Entry points:
  snoopy-hook session-start  → returns system message
  snoopy-hook                → parses transcript and logs to snoopy DB

Claude Code passes JSON on stdin with at least:
  {"transcript_path": "/path/to/session.jsonl", ...}
"""

import json
import sys
import time
from pathlib import Path

from snoopy.buffer import Event, EventBuffer
from snoopy.collectors.claude import parse_transcript
from snoopy.db import Database


def _read_hook_input() -> dict:
    """Read and parse JSON input from stdin (provided by Claude Code)."""
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}


def _log_transcript() -> int:
    """Parse the transcript from hook input and store events in snoopy DB."""
    hook_input = _read_hook_input()
    transcript_path = hook_input.get("transcript_path")

    if not transcript_path:
        print("WARNING: No transcript_path in hook input", file=sys.stderr)
        return 1

    path = Path(transcript_path)
    if not path.exists():
        print(f"WARNING: transcript not found: {path}", file=sys.stderr)
        return 1

    try:
        db = Database()
        db.open()
        buf = EventBuffer(db)

        # Get last offset from watermark
        watermark_key = f"hook_claude_{path.stem}"
        last_offset = int(db.get_watermark(watermark_key) or "0")

        parsed, new_offset = parse_transcript(path, since_offset=last_offset)

        for ev in parsed:
            buf.push(Event(
                table="claude_events",
                columns=[
                    "timestamp", "session_id", "message_type",
                    "content_preview", "project_path",
                ],
                values=(
                    ev["timestamp"], ev["session_id"],
                    ev["message_type"], ev["content_preview"],
                    ev["project_path"],
                ),
            ))

        buf.flush()
        db.set_watermark(watermark_key, str(new_offset), time.time())
        db.close()

        if parsed:
            print(f"snoopy: logged {len(parsed)} claude events", file=sys.stderr)
        return 0

    except Exception as e:
        print(f"WARNING: snoopy hook failed: {e}", file=sys.stderr)
        return 1


def main() -> int:
    """Entry point invoked by Claude Code hooks."""
    if len(sys.argv) > 1 and sys.argv[1] == "session-start":
        print('{"systemMessage": "[snoopy] Session logging active."}')
        return 0
    return _log_transcript()


if __name__ == "__main__":
    sys.exit(main())
