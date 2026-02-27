#!/usr/bin/env python3
"""Per-app screen time breakdown with estimated durations.

Estimates time spent in each app by computing the gap between
consecutive window events (capped at 5 min to exclude idle gaps).

Usage:
    python examples/screen_time.py              # today
    python examples/screen_time.py 2026-02-25   # specific date
    python examples/screen_time.py --week        # last 7 days
"""

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "snoopy.db"
MAX_GAP = 300  # cap per-event duration at 5 min


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def run(date_str: str | None = None, week: bool = False):
    now = datetime.now()
    if week:
        day_start = (now.replace(hour=0, minute=0, second=0) -
                     __import__("datetime").timedelta(days=7)).timestamp()
        day_end = now.timestamp()
        label = "Last 7 Days"
    else:
        target = datetime.strptime(date_str, "%Y-%m-%d") if date_str else now
        day_start = target.replace(hour=0, minute=0, second=0).timestamp()
        day_end = day_start + 86400
        label = target.strftime("%A, %B %d %Y")

    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT timestamp, app_name, window_title FROM window_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()
    conn.close()

    if not rows:
        print("No window events found for this period.")
        return

    app_time: dict[str, float] = defaultdict(float)
    app_titles: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for i in range(len(rows) - 1):
        ts, app, title = rows[i]
        next_ts = rows[i + 1][0]
        duration = min(next_ts - ts, MAX_GAP)
        if app:
            app_time[app] += duration
            if title:
                app_titles[app][title] += duration

    total = sum(app_time.values())
    ranked = sorted(app_time.items(), key=lambda x: -x[1])

    print(f"\n{'=' * 60}")
    print(f"  Screen Time — {label}")
    print(f"{'=' * 60}")
    print(f"\n  Total active time: {fmt_duration(total)}")
    print(f"  {'─' * 56}\n")

    bar_width = 30
    max_time = ranked[0][1] if ranked else 1

    for app, seconds in ranked:
        pct = seconds / total * 100
        bar_len = int(seconds / max_time * bar_width)
        bar = "█" * bar_len + "░" * (bar_width - bar_len)
        print(f"  {app:<22s} {bar} {fmt_duration(seconds):>8s} ({pct:4.1f}%)")

        top_titles = sorted(app_titles[app].items(), key=lambda x: -x[1])[:3]
        for title, t_sec in top_titles:
            print(f"    └─ {title[:50]:<50s} {fmt_duration(t_sec):>8s}")

    print()


if __name__ == "__main__":
    if "--week" in sys.argv:
        run(week=True)
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else None)
