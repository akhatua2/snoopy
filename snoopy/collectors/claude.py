"""Claude session log collector — dual-mode: hook-based (real-time) + polling fallback.

Transcript JSONL format (from Claude Code):
  - type=user:      user messages
  - type=assistant:  assistant responses with content blocks (text, tool_use, thinking)
  - type=progress:   tool results, agent progress, hook progress
  - type=file-history-snapshot: file backup snapshots
  - type=queue-operation: internal queue ops

Each assistant message's content is a list of blocks:
  - {type: "text", text: "..."}
  - {type: "tool_use", name: "Bash", input: {command: "..."}}
  - {type: "thinking", thinking: "..."}
"""

import json
import logging
from pathlib import Path

import snoopy.config as config
from snoopy._native import parse_transcript as _parse_transcript_rs
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)


def parse_transcript(transcript_path: Path, since_offset: int = 0) -> tuple[list[dict], int]:
    """Parse a Claude Code JSONL transcript into structured events.

    Delegates to Rust for the heavy lifting (JSONL parsing, regex, etc.).
    """
    events, final_offset = _parse_transcript_rs(
        str(transcript_path),
        since_offset,
        config.CLAUDE_CONTENT_PREVIEW_LEN,
    )
    return events, final_offset


class ClaudeCollector(BaseCollector):
    """Polling-based fallback collector. The hook handles real-time capture.

    This collector incrementally reads JSONL transcripts from ~/.claude/projects/
    as a fallback for when the hook isn't installed or misses events.
    """
    name = "claude"
    interval = config.CLAUDE_INTERVAL

    def setup(self) -> None:
        self._file_state: dict[str, tuple[float, int]] = {}
        saved = self.get_watermark()
        if saved:
            try:
                self._file_state = json.loads(saved)
            except (json.JSONDecodeError, TypeError):
                pass
        self._initialized = bool(self._file_state)

    def collect(self) -> None:
        projects_dir = config.CLAUDE_PROJECTS_DIR
        if not projects_dir.exists():
            return

        # First run: record current file positions without importing history
        if not self._initialized:
            for jsonl_path in projects_dir.rglob("*.jsonl"):
                str_path = str(jsonl_path)
                stat = jsonl_path.stat()
                self._file_state[str_path] = (stat.st_mtime, stat.st_size)
            self.set_watermark(json.dumps(self._file_state))
            self._initialized = True
            log.info(
                "[%s] first run — indexed %d transcript files, tracking new events only",
                self.name, len(self._file_state),
            )
            return

        all_events = []
        for jsonl_path in projects_dir.rglob("*.jsonl"):
            str_path = str(jsonl_path)
            current_mtime = jsonl_path.stat().st_mtime
            prev_mtime, prev_offset = self._file_state.get(str_path, (0.0, 0))

            if current_mtime <= prev_mtime:
                continue

            parsed, new_offset = parse_transcript(jsonl_path, since_offset=prev_offset)
            self._file_state[str_path] = (current_mtime, new_offset)

            for ev in parsed:
                cols = [
                    "timestamp", "session_id", "message_type",
                    "content_preview", "project_path",
                ]
                vals = (
                    ev["timestamp"], ev["session_id"],
                    ev["message_type"], ev["content_preview"],
                    ev["project_path"],
                )
                all_events.append(Event(
                    table="claude_events", columns=cols, values=vals,
                ))

        if all_events:
            self.buffer.push_many(all_events)
            self.set_watermark(json.dumps(self._file_state))
            log.info("[%s] collected %d events", self.name, len(all_events))
