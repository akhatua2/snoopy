"""Original pure-Python parser implementations, kept for benchmarking against Rust."""

import json
import re
import time
from pathlib import Path


_LSOF_RE = re.compile(
    r"^(\S+)\s+\d+\s+\S+\s+\S+\s+IPv[46]\s+\S+\s+\S+\s+TCP\s+"
    r"\S+->(\d+\.\d+\.\d+\.\d+):(\d+)\s+\(ESTABLISHED\)"
)


def extract_attributed_body_text(blob: bytes) -> str:
    """Extract plain text from the NSArchiver attributedBody blob."""
    if not blob:
        return ""
    try:
        marker = b"NSString"
        idx = blob.find(marker)
        if idx == -1:
            return ""
        search_start = idx + len(marker)
        plus_idx = blob.find(b"\x01+", search_start)
        if plus_idx == -1:
            return ""
        length_offset = plus_idx + 2
        if length_offset >= len(blob):
            return ""
        text_len = blob[length_offset]
        text_start = length_offset + 1
        text_bytes = blob[text_start:text_start + text_len]
        return text_bytes.decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_lsof_output(output: str) -> set[tuple[str, str, int]]:
    """Parse lsof -i -P -n output into a set of (process, ip, port) tuples."""
    current: set[tuple[str, str, int]] = set()
    for line in output.split("\n"):
        m = _LSOF_RE.match(line)
        if m:
            current.add((m.group(1), m.group(2), int(m.group(3))))
    return current


def parse_transcript(
    transcript_path: Path, since_offset: int = 0, preview_len: int = 500
) -> tuple[list[dict], int]:
    """Parse a Claude Code JSONL transcript into structured events."""
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
                if not content.strip():
                    continue
                events.append({
                    "timestamp": ts,
                    "session_id": session_id,
                    "message_type": "user",
                    "content_preview": content[:preview_len],
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
                            "content_preview": block.get("text", "")[:preview_len],
                            "project_path": project_path,
                        })

                    elif block_type == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        preview = _tool_input_preview(tool_name, tool_input)
                        events.append({
                            "timestamp": ts,
                            "session_id": session_id,
                            "message_type": f"tool_use:{tool_name}",
                            "content_preview": preview[:preview_len],
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
                        "content_preview": str(data.get("output", ""))[:preview_len],
                        "project_path": project_path,
                    })

        final_offset = f.tell()

    return events, final_offset


def _extract_content(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(texts)
    return ""


def _tool_input_preview(tool_name: str, tool_input: dict) -> str:
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
    return json.dumps(tool_input, ensure_ascii=False)[:200]


def _parse_iso_ts(ts_str: str) -> float:
    from datetime import datetime
    try:
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time()
