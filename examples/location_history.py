#!/usr/bin/env python3
"""Show places visited with time spent at each.

Groups consecutive location pings at the same address into "visits"
and estimates dwell time from first-to-last ping at each place.

Usage:
    python examples/location_history.py              # today
    python examples/location_history.py 2026-02-25   # specific date
    python examples/location_history.py --week        # last 7 days
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "snoopy.db"


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def run(date_str: str | None = None, week: bool = False):
    now = datetime.now()
    if week:
        day_start = (now.replace(hour=0, minute=0, second=0) -
                     timedelta(days=7)).timestamp()
        day_end = now.timestamp()
        label = "Last 7 Days"
    else:
        target = datetime.strptime(date_str, "%Y-%m-%d") if date_str else now
        day_start = target.replace(hour=0, minute=0, second=0).timestamp()
        day_end = day_start + 86400
        label = target.strftime("%A, %B %d %Y")

    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT timestamp, latitude, longitude, accuracy_m, "
        "       address, locality, admin_area, country "
        "FROM location_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()
    conn.close()

    if not rows:
        print("No location events found for this period.")
        return

    # Group consecutive pings at the same locality into visits
    visits = []
    current = {
        "address": rows[0][4], "locality": rows[0][5],
        "admin_area": rows[0][6], "country": rows[0][7],
        "lat": rows[0][1], "lng": rows[0][2],
        "start": rows[0][0], "end": rows[0][0], "pings": 1,
    }

    for ts, lat, lng, acc, addr, loc, admin, country in rows[1:]:
        if loc == current["locality"] and addr == current["address"]:
            current["end"] = ts
            current["pings"] += 1
        else:
            visits.append(current)
            current = {
                "address": addr, "locality": loc,
                "admin_area": admin, "country": country,
                "lat": lat, "lng": lng,
                "start": ts, "end": ts, "pings": 1,
            }
    visits.append(current)

    print(f"\n{'=' * 60}")
    print(f"  Location History — {label}")
    print(f"{'=' * 60}")
    print(f"  {len(rows)} pings → {len(visits)} places\n")

    for i, v in enumerate(visits, 1):
        start = datetime.fromtimestamp(v["start"]).strftime("%H:%M")
        end = datetime.fromtimestamp(v["end"]).strftime("%H:%M")
        dwell = v["end"] - v["start"]

        place = v["address"] or v["locality"] or f"{v['lat']:.4f}, {v['lng']:.4f}"
        region = ", ".join(filter(None, [v["locality"], v["admin_area"]]))

        print(f"  {i}. {place}")
        if region and region not in place:
            print(f"     {region}")
        print(f"     {start} → {end}  ({fmt_duration(dwell)}, {v['pings']} pings)")
        print()

    # Summary: time per locality
    locality_time: dict[str, float] = {}
    for v in visits:
        key = v["locality"] or "Unknown"
        locality_time[key] = locality_time.get(key, 0) + (v["end"] - v["start"])

    if len(locality_time) > 1:
        print(f"  {'─' * 56}")
        print(f"  Time per area:")
        for loc, secs in sorted(locality_time.items(), key=lambda x: -x[1]):
            print(f"    {loc:<30s} {fmt_duration(secs)}")
        print()


if __name__ == "__main__":
    if "--week" in sys.argv:
        run(week=True)
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else None)
