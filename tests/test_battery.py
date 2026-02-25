"""Tests for battery collector — verifies parsing and deduplication."""

import subprocess

import pytest

from snoopy.db import Database
from snoopy.buffer import EventBuffer
from snoopy.collectors.battery import BatteryCollector, _parse_pmset


@pytest.fixture
def db(tmp_path):
    d = Database(path=tmp_path / "test.db")
    d.open()
    yield d
    d.close()


@pytest.fixture
def buf(db):
    return EventBuffer(db)


_AC_CHARGED = """\
Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234567)	100%; charged; 0:00 remaining present: true
"""

_BATTERY_DISCHARGING = """\
Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234567)	72%; discharging; 3:45 remaining present: true
"""

_AC_CHARGING = """\
Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234567)	85%; charging; 0:30 remaining present: true
"""

_AC_FINISHING = """\
Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234567)	98%; finishing charge; 0:05 remaining present: true
"""


class TestParsePmset:
    def test_ac_charged(self):
        result = _parse_pmset(_AC_CHARGED)
        assert result == (100, False, "ac")

    def test_battery_discharging(self):
        result = _parse_pmset(_BATTERY_DISCHARGING)
        assert result == (72, False, "battery")

    def test_ac_charging(self):
        result = _parse_pmset(_AC_CHARGING)
        assert result == (85, True, "ac")

    def test_finishing_charge_counts_as_charging(self):
        result = _parse_pmset(_AC_FINISHING)
        assert result == (98, True, "ac")

    def test_returns_none_for_garbage(self):
        assert _parse_pmset("no battery here") is None


class TestBatteryCollector:
    def _make_fake_run(self, outputs):
        """Return a function that cycles through pmset output strings."""
        idx = [0]
        def fake_run(*args, **kwargs):
            out = outputs[idx[0]]
            idx[0] += 1
            return subprocess.CompletedProcess(args[0], 0, stdout=out, stderr="")
        return fake_run

    def test_logs_initial_state(self, buf, db, monkeypatch):
        """First collection should always log the current battery state."""
        monkeypatch.setattr(subprocess, "run", self._make_fake_run([_AC_CHARGED]))

        c = BatteryCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        assert db.count("battery_events") == 1

    def test_deduplicates_same_state(self, buf, db, monkeypatch):
        """Two identical readings should produce only one event."""
        monkeypatch.setattr(subprocess, "run", self._make_fake_run([
            _AC_CHARGED, _AC_CHARGED,
        ]))

        c = BatteryCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()
        buf.flush()

        assert db.count("battery_events") == 1

    def test_logs_on_state_change(self, buf, db, monkeypatch):
        """Unplugging from AC should produce a second event."""
        monkeypatch.setattr(subprocess, "run", self._make_fake_run([
            _AC_CHARGED, _BATTERY_DISCHARGING,
        ]))

        c = BatteryCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()
        buf.flush()

        assert db.count("battery_events") == 2

    def test_stores_correct_values(self, buf, db, monkeypatch):
        """Verify the actual values written to the database."""
        monkeypatch.setattr(subprocess, "run", self._make_fake_run([_AC_CHARGING]))

        c = BatteryCollector(buf, db)
        c.setup()
        c.collect()
        buf.flush()

        cur = db._ensure_conn().execute(
            "SELECT percent, is_charging, power_source FROM battery_events"
        )
        row = cur.fetchone()
        assert row == (85, 1, "ac")

    def test_percent_change_triggers_log(self, buf, db, monkeypatch):
        """Even a 1% drop should trigger a new event."""
        output_71 = _BATTERY_DISCHARGING.replace("72%", "71%")
        monkeypatch.setattr(subprocess, "run", self._make_fake_run([
            _BATTERY_DISCHARGING, output_71,
        ]))

        c = BatteryCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()
        buf.flush()

        assert db.count("battery_events") == 2
