"""Tests for shell collector — verifies incremental zsh_history parsing."""

import time

import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.shell import ShellCollector
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


class TestShellCollector:
    def test_first_run_skips_then_collects_new(self, buf, db, tmp_path, monkeypatch):
        """First run skips existing history. New commands after that are collected."""
        hist = tmp_path / ".zsh_history"
        now = int(time.time())

        # Existing history (should be skipped on first run)
        with open(hist, "w") as f:
            f.write(f": {now - 60}:0;ls -la\n")
            f.write(f": {now - 30}:2;git status\n")

        monkeypatch.setattr("snoopy.config.ZSH_HISTORY", hist)

        c = ShellCollector(buf, db)
        c.setup()

        # First run: skips to end of file
        c.collect()
        buf.flush()
        assert db.count("shell_events") == 0

        # Append a new command
        with open(hist, "a") as f:
            f.write(f": {now}:1;python main.py\n")

        # Second run: only the new command is collected
        c.collect()
        buf.flush()
        assert db.count("shell_events") == 1
