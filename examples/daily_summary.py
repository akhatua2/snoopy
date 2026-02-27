#!/usr/bin/env python3
"""Print a chronological timeline of today's activity.

Usage:
    python examples/daily_summary.py              # today
    python examples/daily_summary.py 2026-02-25   # specific date
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "snoopy.db"


def run(date_str: str | None = None):
    target = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    day_start = target.replace(hour=0, minute=0, second=0).timestamp()
    day_end = day_start + 86400

    conn = sqlite3.connect(str(DB))

    print(f"\n{'=' * 60}")
    print(f"  Daily Summary — {target.strftime('%A, %B %d %Y')}")
    print(f"{'=' * 60}")

    # App launches/quits
    apps = conn.execute(
        "SELECT timestamp, event_type, app_name FROM app_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()

    # Window activity
    windows = conn.execute(
        "SELECT timestamp, app_name, window_title FROM window_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()

    # Shell commands
    shells = conn.execute(
        "SELECT timestamp, command FROM shell_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()

    # Location
    locations = conn.execute(
        "SELECT timestamp, address, locality FROM location_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()

    # Calendar
    meetings = conn.execute(
        "SELECT start_time, end_time, title, location FROM calendar_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY start_time",
        (day_start, day_end),
    ).fetchall()

    # Build unified timeline
    events = []
    for ts, etype, app in apps:
        marker = "[+]" if etype == "launch" else "[-]"
        events.append((ts, f"{marker} {etype.upper():6s} {app}"))
    for ts, app, title in windows:
        label = f"{app}: {title}" if title else app
        events.append((ts, f"    > {label[:70]}"))

    seen_cmds = set()
    for ts, cmd in shells:
        if cmd and cmd not in seen_cmds:
            seen_cmds.add(cmd)
            events.append((ts, f"    $ {cmd[:65]}"))
    for ts, addr, loc in locations:
        place = addr or loc or "unknown"
        events.append((ts, f"    @ {place[:70]}"))

    events.sort(key=lambda x: x[0])

    if meetings:
        print(f"\n  MEETINGS")
        print(f"  {'─' * 56}")
        for start, end, title, loc in meetings:
            loc_str = f" @ {loc}" if loc else ""
            print(f"  {start} → {end}  {title}{loc_str}")

    print(f"\n  TIMELINE")
    print(f"  {'─' * 56}")

    last_hour = None
    for ts, label in events:
        t = datetime.fromtimestamp(ts)
        hour = t.strftime("%I %p")
        if hour != last_hour:
            print(f"\n  ── {hour} {'─' * 46}")
            last_hour = hour
        print(f"  {t.strftime('%H:%M:%S')}  {label}")

    # Summary counts
    print(f"\n  {'─' * 56}")
    print(f"  Totals: {len(windows)} window switches | "
          f"{len(shells)} commands | {len(apps)} app events | "
          f"{len(locations)} location pings")
    print()
    conn.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
