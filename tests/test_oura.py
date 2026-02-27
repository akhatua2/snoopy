"""Tests for Oura collector — verifies API merging, upsert, and dedup."""

import pytest

from snoopy.buffer import EventBuffer
from snoopy.collectors.oura import OuraCollector, _merge_by_day
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


_SAMPLE_RAW = {
    "daily_sleep": [
        {"day": "2026-02-24", "score": 55},
    ],
    "sleep": [
        {
            "day": "2026-02-24",
            "type": "long_sleep",
            "total_sleep_duration": 18060,
            "deep_sleep_duration": 4410,
            "rem_sleep_duration": 3330,
            "light_sleep_duration": 10320,
            "awake_time": 1453,
            "bedtime_start": "2026-02-24T03:20:27.000-08:00",
            "bedtime_end": "2026-02-24T08:45:40.000-08:00",
            "average_heart_rate": 64.375,
            "average_hrv": 63,
            "lowest_heart_rate": 60,
            "average_breath": 13.75,
            "efficiency": 93,
        },
    ],
    "daily_readiness": [
        {
            "day": "2026-02-24",
            "score": 60,
            "temperature_deviation": 0.09,
        },
    ],
    "daily_activity": [
        {
            "day": "2026-02-24",
            "score": 72,
            "steps": 5432,
            "active_calories": 312,
        },
    ],
    "daily_stress": [
        {"day": "2026-02-24", "stress_high": 120, "recovery_high": 300},
    ],
    "daily_spo2": [
        {"day": "2026-02-24", "spo2_percentage": {"average": 97.5}},
    ],
}


class TestMergeByDay:
    def test_merges_all_endpoints_into_single_day(self):
        merged = _merge_by_day(_SAMPLE_RAW)
        assert "2026-02-24" in merged
        d = merged["2026-02-24"]
        assert d["sleep_score"] == 55
        assert d["readiness_score"] == 60
        assert d["activity_score"] == 72
        assert d["total_sleep_s"] == 18060
        assert d["deep_sleep_s"] == 4410
        assert d["avg_hrv"] == 63
        assert d["steps"] == 5432
        assert d["spo2_percentage"] == 97.5
        assert d["stress_high"] == 120

    def test_ignores_nap_sleep_periods(self):
        raw = {
            "daily_sleep": [], "daily_readiness": [], "daily_activity": [],
            "daily_stress": [], "daily_spo2": [],
            "sleep": [
                {"day": "2026-02-24", "type": "rest", "total_sleep_duration": 1200},
                {"day": "2026-02-24", "type": "long_sleep", "total_sleep_duration": 18060},
            ],
        }
        merged = _merge_by_day(raw)
        assert merged["2026-02-24"]["total_sleep_s"] == 18060

    def test_handles_spo2_as_scalar(self):
        raw = {
            "daily_sleep": [], "daily_readiness": [], "daily_activity": [],
            "daily_stress": [], "sleep": [],
            "daily_spo2": [{"day": "2026-02-24", "spo2_percentage": 98.0}],
        }
        merged = _merge_by_day(raw)
        assert merged["2026-02-24"]["spo2_percentage"] == 98.0


class TestOuraCollector:
    def test_inserts_new_day(self, buf, db, monkeypatch):
        """First collect should insert a new row."""
        monkeypatch.setattr(
            "snoopy.collectors.oura._fetch_all",
            lambda token, start, end: _SAMPLE_RAW,
        )
        monkeypatch.setattr("snoopy.collectors.oura.config.OURA_PAT", "fake-token")

        c = OuraCollector(buf, db)
        c.setup()
        c.collect()

        assert db.count("oura_daily") == 1
        conn = db._ensure_conn()
        row = conn.execute(
            "SELECT day, sleep_score, readiness_score, activity_score, "
            "total_sleep_s, steps, avg_hrv FROM oura_daily"
        ).fetchone()
        assert row[0] == "2026-02-24"
        assert row[1] == 55
        assert row[2] == 60
        assert row[3] == 72
        assert row[4] == 18060
        assert row[5] == 5432
        assert row[6] == 63

    def test_upserts_on_second_collect(self, buf, db, monkeypatch):
        """Second collect should update, not duplicate."""
        monkeypatch.setattr("snoopy.collectors.oura.config.OURA_PAT", "fake-token")

        calls = [0]
        def fake_fetch(token, start, end):
            calls[0] += 1
            if calls[0] == 1:
                return _SAMPLE_RAW
            # Second call: score updated
            updated = dict(_SAMPLE_RAW)
            updated["daily_sleep"] = [{"day": "2026-02-24", "score": 60}]
            return updated

        monkeypatch.setattr("snoopy.collectors.oura._fetch_all", fake_fetch)

        c = OuraCollector(buf, db)
        c.setup()
        c.collect()
        c.collect()

        assert db.count("oura_daily") == 1
        conn = db._ensure_conn()
        row = conn.execute(
            "SELECT sleep_score FROM oura_daily WHERE day='2026-02-24'"
        ).fetchone()
        assert row[0] == 60

    def test_skips_when_no_token(self, buf, db, monkeypatch):
        """Collector should do nothing if OURA_PAT is empty."""
        monkeypatch.setattr("snoopy.collectors.oura.config.OURA_PAT", "")

        c = OuraCollector(buf, db)
        c.setup()
        c.collect()

        assert db.count("oura_daily") == 0

    def test_has_first_seen_and_last_seen(self, buf, db, monkeypatch):
        """New rows should have first_seen and last_seen populated."""
        monkeypatch.setattr("snoopy.collectors.oura.config.OURA_PAT", "fake-token")
        monkeypatch.setattr(
            "snoopy.collectors.oura._fetch_all",
            lambda token, start, end: _SAMPLE_RAW,
        )

        c = OuraCollector(buf, db)
        c.setup()
        c.collect()

        conn = db._ensure_conn()
        row = conn.execute(
            "SELECT first_seen, last_seen FROM oura_daily"
        ).fetchone()
        assert row[0] is not None
        assert row[1] is not None
        assert row[0] == row[1]

    def test_updates_last_seen_on_upsert(self, buf, db, monkeypatch):
        """Upsert should update last_seen but keep first_seen."""
        monkeypatch.setattr("snoopy.collectors.oura.config.OURA_PAT", "fake-token")
        monkeypatch.setattr(
            "snoopy.collectors.oura._fetch_all",
            lambda token, start, end: _SAMPLE_RAW,
        )

        c = OuraCollector(buf, db)
        c.setup()
        c.collect()

        conn = db._ensure_conn()
        first = conn.execute(
            "SELECT first_seen, last_seen FROM oura_daily"
        ).fetchone()

        c.collect()
        second = conn.execute(
            "SELECT first_seen, last_seen FROM oura_daily"
        ).fetchone()

        assert second[0] == first[0]  # first_seen unchanged
        assert second[1] >= first[1]  # last_seen updated

    def test_multiple_days(self, buf, db, monkeypatch):
        """Should handle data for multiple days."""
        monkeypatch.setattr("snoopy.collectors.oura.config.OURA_PAT", "fake-token")

        multi_day = {
            "daily_sleep": [
                {"day": "2026-02-23", "score": 80},
                {"day": "2026-02-24", "score": 55},
            ],
            "sleep": [], "daily_readiness": [], "daily_activity": [],
            "daily_stress": [], "daily_spo2": [],
        }
        monkeypatch.setattr(
            "snoopy.collectors.oura._fetch_all",
            lambda token, start, end: multi_day,
        )

        c = OuraCollector(buf, db)
        c.setup()
        c.collect()

        assert db.count("oura_daily") == 2
