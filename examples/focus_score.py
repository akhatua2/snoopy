#!/usr/bin/env python3
"""Analyze focus patterns: context switches, deep work blocks, distractions.

Detects "deep work" blocks (20+ min in a single app without switching)
and measures context switch frequency per hour.

Usage:
    python examples/focus_score.py              # today
    python examples/focus_score.py 2026-02-25   # specific date
"""

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "snoopy.db"
MAX_GAP = 300
DEEP_WORK_THRESHOLD = 1200  # 20 min continuous in one app

COMMUNICATION_APPS = frozenset({
    "Slack", "Discord", "Messages", "Mail", "Telegram",
    "WhatsApp", "Microsoft Teams", "Zoom",
})

CODING_APPS = frozenset({
    "Cursor", "Code", "Visual Studio Code", "Terminal", "iTerm2",
    "Warp", "Xcode", "IntelliJ IDEA", "PyCharm", "Alacritty",
})


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def run(date_str: str | None = None):
    target = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    day_start = target.replace(hour=0, minute=0, second=0).timestamp()
    day_end = day_start + 86400

    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT timestamp, app_name FROM window_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (day_start, day_end),
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        print("Not enough window events for analysis.")
        return

    # Compute per-event durations and detect switches
    switches = 0
    hourly_switches: dict[int, int] = defaultdict(int)
    app_time: dict[str, float] = defaultdict(float)
    deep_blocks: list[tuple[str, float, float]] = []

    streak_app = rows[0][1]
    streak_start = rows[0][0]
    streak_time = 0.0

    for i in range(len(rows) - 1):
        ts, app = rows[i]
        next_ts, next_app = rows[i + 1]
        duration = min(next_ts - ts, MAX_GAP)

        if app:
            app_time[app] += duration

        if next_app != app:
            switches += 1
            hour = datetime.fromtimestamp(next_ts).hour
            hourly_switches[hour] += 1

            streak_time += duration
            if streak_time >= DEEP_WORK_THRESHOLD and streak_app:
                deep_blocks.append((streak_app, streak_start, streak_time))
            streak_app = next_app
            streak_start = next_ts
            streak_time = 0.0
        else:
            streak_time += duration

    # Final streak
    if streak_time >= DEEP_WORK_THRESHOLD and streak_app:
        deep_blocks.append((streak_app, streak_start, streak_time))

    total_time = sum(app_time.values())
    first_ts = rows[0][0]
    last_ts = rows[-1][0]
    active_hours = (last_ts - first_ts) / 3600

    coding_time = sum(t for a, t in app_time.items() if a in CODING_APPS)
    comms_time = sum(t for a, t in app_time.items() if a in COMMUNICATION_APPS)
    deep_total = sum(d for _, _, d in deep_blocks)

    print(f"\n{'=' * 60}")
    print(f"  Focus Analysis — {target.strftime('%A, %B %d %Y')}")
    print(f"{'=' * 60}")

    print(f"\n  Active window time:   {fmt_duration(total_time)}")
    print(f"  Context switches:     {switches}")
    if active_hours > 0:
        print(f"  Switches/hour:        {switches / active_hours:.1f}")
    print(f"  Deep work blocks:     {len(deep_blocks)} ({fmt_duration(deep_total)})")
    print(f"  Coding time:          {fmt_duration(coding_time)}")
    print(f"  Communication time:   {fmt_duration(comms_time)}")

    if total_time > 0:
        focus_ratio = (coding_time + deep_total) / total_time / 2
        score = min(100, int(focus_ratio * 100 + len(deep_blocks) * 5))
        bar_len = score // 2
        bar = "█" * bar_len + "░" * (50 - bar_len)
        print(f"\n  Focus score:  [{bar}] {score}/100")

    if deep_blocks:
        print(f"\n  DEEP WORK BLOCKS")
        print(f"  {'─' * 56}")
        for app, start, dur in sorted(deep_blocks, key=lambda x: -x[2]):
            t = datetime.fromtimestamp(start).strftime("%H:%M")
            print(f"    {t}  {app:<25s} {fmt_duration(dur)}")

    if hourly_switches:
        print(f"\n  SWITCHES BY HOUR")
        print(f"  {'─' * 56}")
        max_sw = max(hourly_switches.values())
        for h in range(min(hourly_switches), max(hourly_switches) + 1):
            count = hourly_switches.get(h, 0)
            bar = "▓" * int(count / max_sw * 30) if max_sw else ""
            print(f"    {h:02d}:00  {bar:<30s} {count}")

    print()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
