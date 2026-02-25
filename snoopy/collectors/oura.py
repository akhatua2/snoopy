"""Oura Ring collector — pulls daily sleep, readiness, and activity from the Oura API v2.

Polls once per day. Fetches the last 2 days to handle timezone edges
and late syncs. Upserts into oura_daily keyed on day.
"""

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from snoopy.collectors.base import BaseCollector
import snoopy.config as config

log = logging.getLogger(__name__)

_BASE_URL = "https://api.ouraring.com/v2/usercollection"

_ENDPOINTS = [
    "daily_sleep",
    "sleep",
    "daily_readiness",
    "daily_activity",
    "daily_stress",
    "daily_spo2",
]

_UPSERT_COLS = [
    "timestamp", "day", "sleep_score", "readiness_score", "activity_score",
    "total_sleep_s", "deep_sleep_s", "rem_sleep_s", "light_sleep_s",
    "awake_time_s", "bedtime_start", "bedtime_end",
    "avg_heart_rate", "avg_hrv", "lowest_heart_rate", "avg_breath",
    "sleep_efficiency", "temperature_deviation",
    "steps", "active_calories", "spo2_percentage",
    "stress_high", "recovery_high", "first_seen", "last_seen",
]


def _api_get(endpoint: str, params: dict, token: str) -> dict:
    """GET request to Oura API v2. Returns parsed JSON."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_BASE_URL}/{endpoint}?{query}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _fetch_all(token: str, start_date: str, end_date: str) -> dict[str, list]:
    """Fetch all endpoints for a date range. Returns {endpoint: data_list}."""
    results = {}
    for ep in _ENDPOINTS:
        try:
            resp = _api_get(ep, {
                "start_date": start_date, "end_date": end_date,
            }, token)
            results[ep] = resp.get("data", [])
        except Exception:
            log.exception("[oura] failed to fetch %s", ep)
            results[ep] = []
    return results


def _merge_by_day(raw: dict[str, list]) -> dict[str, dict]:
    """Merge data from all endpoints into per-day dicts."""
    days = {}

    for item in raw.get("daily_sleep", []):
        day = item.get("day")
        if not day:
            continue
        days.setdefault(day, {})["sleep_score"] = item.get("score")

    # Detailed sleep — use the "long_sleep" period (main sleep, not naps)
    for item in raw.get("sleep", []):
        day = item.get("day")
        if not day or item.get("type") != "long_sleep":
            continue
        d = days.setdefault(day, {})
        d["total_sleep_s"] = item.get("total_sleep_duration")
        d["deep_sleep_s"] = item.get("deep_sleep_duration")
        d["rem_sleep_s"] = item.get("rem_sleep_duration")
        d["light_sleep_s"] = item.get("light_sleep_duration")
        d["awake_time_s"] = item.get("awake_time")
        d["bedtime_start"] = item.get("bedtime_start")
        d["bedtime_end"] = item.get("bedtime_end")
        d["avg_heart_rate"] = item.get("average_heart_rate")
        d["avg_hrv"] = item.get("average_hrv")
        d["lowest_heart_rate"] = item.get("lowest_heart_rate")
        d["avg_breath"] = item.get("average_breath")
        d["sleep_efficiency"] = item.get("efficiency")

    for item in raw.get("daily_readiness", []):
        day = item.get("day")
        if not day:
            continue
        d = days.setdefault(day, {})
        d["readiness_score"] = item.get("score")
        d["temperature_deviation"] = item.get("temperature_deviation")

    for item in raw.get("daily_activity", []):
        day = item.get("day")
        if not day:
            continue
        d = days.setdefault(day, {})
        d["activity_score"] = item.get("score")
        d["steps"] = item.get("steps")
        d["active_calories"] = item.get("active_calories")

    for item in raw.get("daily_stress", []):
        day = item.get("day")
        if not day:
            continue
        d = days.setdefault(day, {})
        d["stress_high"] = item.get("stress_high")
        d["recovery_high"] = item.get("recovery_high")

    for item in raw.get("daily_spo2", []):
        day = item.get("day")
        if not day:
            continue
        d = days.setdefault(day, {})
        spo2 = item.get("spo2_percentage")
        if isinstance(spo2, dict):
            d["spo2_percentage"] = spo2.get("average")
        else:
            d["spo2_percentage"] = spo2

    return days


class OuraCollector(BaseCollector):
    name = "oura"
    interval = config.OURA_INTERVAL

    def setup(self) -> None:
        self._token = config.OURA_PAT
        if not self._token:
            log.warning("[oura] OURA_PAT not set — collector will be inactive")

    def collect(self) -> None:
        if not self._token:
            return

        now = time.time()
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=2)).isoformat()
        end = today.isoformat()

        raw = _fetch_all(self._token, start, end)
        merged = _merge_by_day(raw)

        if not merged:
            return

        conn = self.db._ensure_conn()
        upserted = 0

        with self.db._lock:
            for day, data in merged.items():
                vals = tuple(
                    data.get(col) for col in _UPSERT_COLS
                    if col not in ("timestamp", "day", "first_seen", "last_seen")
                )
                # Check if row exists
                existing = conn.execute(
                    "SELECT id FROM oura_daily WHERE day = ?", (day,)
                ).fetchone()

                if existing:
                    set_clause = ", ".join(
                        f"{col} = ?" for col in _UPSERT_COLS
                        if col not in ("timestamp", "day", "first_seen", "last_seen")
                    )
                    conn.execute(
                        f"UPDATE oura_daily SET {set_clause}, last_seen = ? "
                        f"WHERE day = ?",
                        vals + (now, day),
                    )
                else:
                    placeholders = ", ".join("?" for _ in _UPSERT_COLS)
                    col_names = ", ".join(_UPSERT_COLS)
                    conn.execute(
                        f"INSERT INTO oura_daily ({col_names}) VALUES ({placeholders})",
                        (now, day, *vals, now, now),
                    )
                upserted += 1

            conn.commit()

        if upserted:
            log.info("[%s] upserted %d day(s)", self.name, upserted)
