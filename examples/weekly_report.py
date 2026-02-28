#!/usr/bin/env python3
"""Generate a high-level weekly report across all collectors.

Aggregates event counts, top apps, top commands, location summary,
communication stats, and battery patterns for the past 7 days.

Usage:
    python examples/weekly_report.py
"""

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "snoopy.db"
MAX_GAP = 300


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def run():
    now = datetime.now()
    week_start = (now.replace(hour=0, minute=0, second=0) - timedelta(days=7)).timestamp()
    week_end = now.timestamp()
    rng = (week_start, week_end)

    conn = sqlite3.connect(str(DB))

    print(f"\n{'=' * 60}")
    print("  Weekly Report")
    print(f"  {datetime.fromtimestamp(week_start).strftime('%b %d')} → "
          f"{now.strftime('%b %d, %Y')}")
    print(f"{'=' * 60}")

    # ── Event volume ──
    tables = [
        ("window_events", "Window switches"),
        ("app_events", "App launches/quits"),
        ("shell_events", "Shell commands"),
        ("file_events", "File changes"),
        ("browser_events", "Browser visits"),
        ("clipboard_events", "Clipboard copies"),
        ("notification_events", "Notifications"),
        ("network_events", "Network connections"),
        ("location_events", "Location pings"),
        ("claude_events", "Claude interactions"),
        ("message_events", "Messages"),
        ("mail_events", "Emails"),
    ]

    print("\n  EVENT VOLUME")
    print(f"  {'─' * 56}")
    total_events = 0
    for table, label in tables:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE timestamp BETWEEN ? AND ?", rng
        ).fetchone()[0]
        total_events += count
        if count > 0:
            print(f"    {label:<30s} {count:>6,}")
    print(f"    {'':─<30s} {'':─>6s}")
    print(f"    {'Total':<30s} {total_events:>6,}")

    # ── Screen time by app ──
    windows = conn.execute(
        "SELECT timestamp, app_name FROM window_events "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp", rng
    ).fetchall()

    if windows:
        app_time: dict[str, float] = defaultdict(float)
        for i in range(len(windows) - 1):
            ts, app = windows[i]
            duration = min(windows[i + 1][0] - ts, MAX_GAP)
            if app:
                app_time[app] += duration

        top_apps = sorted(app_time.items(), key=lambda x: -x[1])[:10]
        print("\n  TOP APPS BY SCREEN TIME")
        print(f"  {'─' * 56}")
        for app, secs in top_apps:
            pct = secs / sum(app_time.values()) * 100
            bar = "█" * int(pct / 2)
            print(f"    {app:<22s} {fmt_duration(secs):>8s} {bar}")

    # ── Top shell commands ──
    cmds = conn.execute(
        "SELECT command, COUNT(*) as c FROM shell_events "
        "WHERE timestamp BETWEEN ? AND ? AND command IS NOT NULL "
        "GROUP BY command ORDER BY c DESC LIMIT 10", rng
    ).fetchall()

    if cmds:
        print("\n  TOP SHELL COMMANDS")
        print(f"  {'─' * 56}")
        for cmd, count in cmds:
            print(f"    {count:>4}x  {cmd[:50]}")

    # ── Locations visited ──
    locs = conn.execute(
        "SELECT locality, COUNT(*) as c FROM location_events "
        "WHERE timestamp BETWEEN ? AND ? AND locality IS NOT NULL "
        "GROUP BY locality ORDER BY c DESC", rng
    ).fetchall()

    if locs:
        print("\n  LOCATIONS")
        print(f"  {'─' * 56}")
        for loc, count in locs:
            print(f"    {loc:<30s} {count} pings")

    # ── Communication ──
    msg_count = conn.execute(
        "SELECT COUNT(*) FROM message_events WHERE timestamp BETWEEN ? AND ?", rng
    ).fetchone()[0]
    mail_count = conn.execute(
        "SELECT COUNT(*) FROM mail_events WHERE timestamp BETWEEN ? AND ?", rng
    ).fetchone()[0]
    notif_count = conn.execute(
        "SELECT COUNT(*) FROM notification_events WHERE timestamp BETWEEN ? AND ?", rng
    ).fetchone()[0]

    if msg_count or mail_count or notif_count:
        print("\n  COMMUNICATION")
        print(f"  {'─' * 56}")
        if msg_count:
            print(f"    Messages:       {msg_count}")
        if mail_count:
            print(f"    Emails:         {mail_count}")
        if notif_count:
            print(f"    Notifications:  {notif_count}")

    # ── Battery patterns ──
    batt = conn.execute(
        "SELECT MIN(percent), MAX(percent), "
        "       SUM(CASE WHEN is_charging THEN 1 ELSE 0 END), COUNT(*) "
        "FROM battery_events WHERE timestamp BETWEEN ? AND ?", rng
    ).fetchone()

    if batt and batt[3] > 0:
        print("\n  BATTERY")
        print(f"  {'─' * 56}")
        print(f"    Range:          {batt[0]}% → {batt[1]}%")
        print(f"    Charging events: {batt[2]} of {batt[3]} readings")

    # ── Daily breakdown ──
    daily = conn.execute(
        "SELECT DATE(timestamp, 'unixepoch', 'localtime') as day, COUNT(*) "
        "FROM window_events WHERE timestamp BETWEEN ? AND ? "
        "GROUP BY day ORDER BY day", rng
    ).fetchall()

    if daily:
        print("\n  DAILY ACTIVITY (window events)")
        print(f"  {'─' * 56}")
        max_count = max(c for _, c in daily)
        for day, count in daily:
            bar = "▓" * int(count / max_count * 35)
            print(f"    {day}  {bar:<35s} {count:>5}")

    print()
    conn.close()


if __name__ == "__main__":
    run()
