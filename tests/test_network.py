"""Tests for network collector — verifies lsof parsing and connection deduplication."""


import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.network import NetworkCollector
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


FAKE_LSOF = (
    "COMMAND   PID USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME\n"
    "Chrome   1234 user   42u  IPv4 0xabc  0t0  TCP "
    "192.168.1.5:54321->142.250.80.46:443 (ESTABLISHED)\n"
    "Spotify  5678 user   18u  IPv4 0xdef  0t0  TCP "
    "192.168.1.5:55555->35.186.224.25:4070 (ESTABLISHED)\n"
    "httpd    9012 root    4u  IPv4 0xghi  0t0  TCP *:80 (LISTEN)\n"
)


class TestNetworkCollector:
    def test_parses_and_deduplicates_connections(self, buf, db, monkeypatch):
        """Run lsof twice with the same output. First poll should log 2 connections
        (Chrome + Spotify, not the LISTEN). Second poll should log 0 (already seen).
        Then add a new connection on third poll — only the new one should appear."""

        import subprocess

        class FakeResult:
            returncode = 0
            stdout = FAKE_LSOF

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())

        c = NetworkCollector(buf, db)
        c.setup()

        c.collect()
        buf.flush()
        assert db.count("network_events") == 2

        # Same connections — no new rows
        c.collect()
        buf.flush()
        assert db.count("network_events") == 2

        # Add a new connection
        class FakeResult2:
            returncode = 0
            stdout = (
                FAKE_LSOF
                + "Slack  3456 user  22u  IPv4 0xjkl  0t0  TCP "
                "192.168.1.5:44444->54.187.168.6:443 (ESTABLISHED)\n"
            )

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult2())

        c.collect()
        buf.flush()
        assert db.count("network_events") == 3  # only 1 new
