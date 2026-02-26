"""Tests for location collector — verifies parsing and event creation."""

import subprocess

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors import location as loc_mod
from snoopy.collectors.location import LocationCollector


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


_GOOD_OUTPUT = (
    "37.4262||-122.1590||42.0||35||737 Campus Dr\n"
    "Stanford CA 94305\n"
    "United States||Stanford||CA||United States\n"
)


def _fake_run(stdout, returncode=0):
    """Return a callable that mimics subprocess.run with preset output."""
    def fake(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], returncode, stdout=stdout, stderr="")
    return fake


class TestLocationCollector:
    def test_successful_collect(self, buf, db, monkeypatch):
        """Valid CLI output should produce one event with address data."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run", _fake_run(_GOOD_OUTPUT))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 1
        row = db._ensure_conn().execute(
            "SELECT latitude, longitude, accuracy_m, altitude_m, "
            "address, locality, admin_area, country, source "
            "FROM location_events"
        ).fetchone()
        assert row[0] == pytest.approx(37.4262)
        assert row[1] == pytest.approx(-122.1590)
        assert row[2] == pytest.approx(35.0)
        assert row[3] == pytest.approx(42.0)
        assert "737 Campus Dr" in row[4]
        assert row[5] == "Stanford"
        assert row[6] == "CA"
        assert row[7] == "United States"
        assert row[8] == "corelocationcli"

    def test_address_newlines_collapsed(self, buf, db, monkeypatch):
        """Multiline address from CLI should be joined into one line."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run", _fake_run(_GOOD_OUTPUT))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        row = db._ensure_conn().execute(
            "SELECT address FROM location_events"
        ).fetchone()
        assert "\n" not in row[0]

    def test_no_cli_skips(self, buf, db, monkeypatch):
        """When CoreLocationCLI is not installed, collect does nothing."""
        monkeypatch.setattr(loc_mod, "_CLI", None)

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 0

    def test_nonzero_exit_skips(self, buf, db, monkeypatch):
        """Non-zero return code from CLI should produce no event."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 0

    def test_empty_stdout_skips(self, buf, db, monkeypatch):
        """Empty stdout should produce no event."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run", _fake_run("\n"))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 0

    def test_malformed_output_skips(self, buf, db, monkeypatch):
        """If CLI returns fewer than 8 fields, no event is created."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run",
                            _fake_run("37.4262||-122.1590\n"))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 0

    def test_non_numeric_coords_skips(self, buf, db, monkeypatch):
        """Non-numeric coordinates should be silently skipped."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run",
                            _fake_run("abc||def||ghi||jkl||addr||city||st||us\n"))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 0

    def test_timeout_skips(self, buf, db, monkeypatch):
        """If CoreLocationCLI hangs, the timeout should be caught gracefully."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")

        def fake_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=15)

        monkeypatch.setattr(subprocess, "run", fake_timeout)

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 0

    def test_multiple_collects(self, buf, db, monkeypatch):
        """Multiple successful collects should produce multiple events."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")

        outputs = [_GOOD_OUTPUT, _GOOD_OUTPUT.replace("37.4262", "37.4270")]
        calls = [0]

        def cycling_run(*args, **kwargs):
            out = outputs[calls[0]]
            calls[0] += 1
            return subprocess.CompletedProcess(args[0], 0, stdout=out, stderr="")

        monkeypatch.setattr(subprocess, "run", cycling_run)

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 2

    def test_empty_address_stored_as_null(self, buf, db, monkeypatch):
        """If geocoding returns empty strings, store as NULL not empty."""
        monkeypatch.setattr(loc_mod, "_CLI", "/usr/bin/fake")
        monkeypatch.setattr(subprocess, "run",
                            _fake_run("37.4262||-122.1590||0||35||||||||\n"))

        c = LocationCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("location_events") == 1
        row = db._ensure_conn().execute(
            "SELECT address, locality, admin_area, country FROM location_events"
        ).fetchone()
        assert row == (None, None, None, None)
