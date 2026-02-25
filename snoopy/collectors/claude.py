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
import time
from pathlib import Path

from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)


def parse_transcript(transcript_path: Path, since_offset: int = 0) -> list[dict]:
    """Parse a Claude Code JSONL transcript into structured events.

    Returns a list of dicts with keys: timestamp, session_id, message_type,
    content_preview, project_path, tool_name, tool_input_preview.
    """
    events = []
    session_id = transcript_path.stem
    project_path = str(transcript_path.parent)

    with open(transcript_path, "r", errors="replace") as f:
        f.seek(since_offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = entry.get("type", "")
            ts_str = entry.get("timestamp", "")
            ts = _parse_iso_ts(ts_str) if ts_str else time.time()

            if event_type == "user":
                content = _extract_content(entry.get("message", {}))
                events.append({
                    "timestamp": ts,
                    "session_id": session_id,
                    "message_type": "user",
                    "content_preview": content[:config.CLAUDE_CONTENT_PREVIEW_LEN],
                    "project_path": project_path,
                })

            elif event_type == "assistant":
                msg = entry.get("message", {})
                content_blocks = msg.get("content", [])
                if not isinstance(content_blocks, list):
                    continue

                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type", "")

                    if block_type == "text":
                        events.append({
                            "timestamp": ts,
                            "session_id": session_id,
                            "message_type": "assistant_text",
                            "content_preview": block.get("text", "")[:config.CLAUDE_CONTENT_PREVIEW_LEN],
                            "project_path": project_path,
                        })

                    elif block_type == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        # Build a compact preview of the tool input
                        preview = _tool_input_preview(tool_name, tool_input)
                        events.append({
                            "timestamp": ts,
                            "session_id": session_id,
                            "message_type": f"tool_use:{tool_name}",
                            "content_preview": preview[:config.CLAUDE_CONTENT_PREVIEW_LEN],
                            "project_path": project_path,
                        })

            elif event_type == "progress":
                data = entry.get("data", {})
                subtype = data.get("type", "")
                if subtype == "tool_result":
                    events.append({
                        "timestamp": ts,
                        "session_id": session_id,
                        "message_type": f"tool_result:{data.get('tool_name', '')}",
                        "content_preview": str(data.get("output", ""))[:config.CLAUDE_CONTENT_PREVIEW_LEN],
                        "project_path": project_path,
                    })

        final_offset = f.tell()

    return events, final_offset


def _extract_content(msg: dict) -> str:
    """Extract text content from a user or assistant message."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(texts)
    return ""


def _tool_input_preview(tool_name: str, tool_input: dict) -> str:
    """Build a readable preview of a tool call."""
    if tool_name == "Bash":
        return tool_input.get("command", "")
    if tool_name in ("Read", "Glob"):
        return tool_input.get("file_path", "") or tool_input.get("pattern", "")
    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        size = len(tool_input.get("content", ""))
        return f"{path} ({size} chars)"
    if tool_name == "Edit":
        return tool_input.get("file_path", "")
    if tool_name == "Grep":
        return f"/{tool_input.get('pattern', '')}/ in {tool_input.get('path', '.')}"
    if tool_name == "Task":
        return tool_input.get("description", "")
    # Generic fallback
    return json.dumps(tool_input, ensure_ascii=False)[:200]


def _parse_iso_ts(ts_str: str) -> float:
    """Parse ISO 8601 timestamp to epoch float."""
    from datetime import datetime, timezone
    try:
        # Handle "2026-02-25T08:16:18.720Z"
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()


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
            log.info("[%s] first run — indexed %d transcript files, tracking new events only", self.name, len(self._file_state))
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
                all_events.append(Event(
                    table="claude_events",
                    columns=["timestamp", "session_id", "message_type", "content_preview", "project_path"],
                    values=(ev["timestamp"], ev["session_id"], ev["message_type"],
                            ev["content_preview"], ev["project_path"]),
                ))

        if all_events:
            self.buffer.push_many(all_events)
            self.set_watermark(json.dumps(self._file_state))
            log.info("[%s] collected %d events", self.name, len(all_events))
