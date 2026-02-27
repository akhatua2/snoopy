"""Tests for Claude collector — verifies JSONL transcript parsing + hook flow."""

import json
import time

import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.claude import ClaudeCollector, parse_transcript
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


def _write_transcript(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestParseTranscript:
    def test_parses_user_and_assistant_messages(self, tmp_path):
        """Parse a transcript with a user message and an assistant response
        containing text + tool_use blocks. Should produce 3 events:
        1 user, 1 assistant_text, 1 tool_use."""

        transcript = tmp_path / "session-abc.jsonl"
        _write_transcript(transcript, [
            {
                "type": "user",
                "timestamp": "2026-02-25T10:00:00.000Z",
                "message": {"role": "user", "content": "list files in /tmp"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-02-25T10:00:01.000Z",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Let me list the files."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls /tmp"}},
                ]},
            },
        ])

        events, offset = parse_transcript(transcript)

        assert len(events) == 3
        assert events[0]["message_type"] == "user"
        assert events[1]["message_type"] == "assistant_text"
        assert events[2]["message_type"] == "tool_use:Bash"
        assert events[2]["content_preview"] == "ls /tmp"
        assert offset > 0

    def test_incremental_parsing_with_offset(self, tmp_path):
        """Write 2 entries, parse to get offset. Append 1 more, parse from offset.
        Should only return the new entry."""

        transcript = tmp_path / "session-inc.jsonl"
        _write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-02-25T10:00:00Z", "message": {"content": "hello"}},
            {"type": "user", "timestamp": "2026-02-25T10:00:01Z", "message": {"content": "world"}},
        ])

        events1, offset1 = parse_transcript(transcript)
        assert len(events1) == 2

        with open(transcript, "a") as f:
            entry = {"type": "user", "timestamp": "2026-02-25T10:00:02Z",
                     "message": {"content": "new"}}
            f.write(json.dumps(entry) + "\n")

        events2, offset2 = parse_transcript(transcript, since_offset=offset1)
        assert len(events2) == 1
        assert events2[0]["content_preview"] == "new"


class TestClaudeCollector:
    def test_first_run_skips_then_collects_new(self, buf, db, tmp_path, monkeypatch):
        """First run indexes existing transcripts without importing.
        Appending a new entry after that should be collected on the next run."""

        projects_dir = tmp_path / "projects" / "my-project"
        projects_dir.mkdir(parents=True)
        monkeypatch.setattr("snoopy.config.CLAUDE_PROJECTS_DIR", tmp_path / "projects")

        transcript = projects_dir / "session-abc.jsonl"
        _write_transcript(transcript, [
            {"type": "user", "timestamp": "2026-02-25T10:00:00Z", "message": {"content": "first"}},
        ])

        c = ClaudeCollector(buf, db)
        c.setup()

        # First run: indexes files but doesn't import
        c.collect()
        buf.flush()
        assert db.count("claude_events") == 0

        # Append a new entry and collect again
        time.sleep(0.05)
        with open(transcript, "a") as f:
            entry = {"type": "user", "timestamp": "2026-02-25T10:00:01Z",
                     "message": {"content": "second"}}
            f.write(json.dumps(entry) + "\n")

        c.collect()
        buf.flush()
        assert db.count("claude_events") == 1
