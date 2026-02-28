#!/usr/bin/env python3
"""Build a next-action prediction dataset from today's snoopy data.

Format: p(next_action | past_5min_actions, current_stimuli)

Each row:
  - timestamp: when the action occurred
  - past_5min: summary of actions in the preceding 5 minutes
  - stimuli: incoming events (notifications, messages received) in the window
  - action: what the user actually did (the prediction target)
"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "snoopy.db"
WINDOW_S = 300  # 5 minutes lookback
TODAY_HOURS = 14  # how far back "today" goes


def load_events(conn: sqlite3.Connection, cutoff: float) -> list[dict]:
    """Load all events from today into a unified timeline."""
    events = []

    for row in conn.execute(
        """
        SELECT timestamp, 'focus' as type,
               app_name || CASE WHEN window_title != ''
                   THEN ': ' || window_title ELSE '' END as detail
        FROM window_events WHERE timestamp >= ? AND duration_s >= 10
        ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp, 'browse' as type, title as detail
        FROM browser_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp, 'shell' as type, substr(command, 1, 100) as detail
        FROM shell_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp,
               CASE WHEN is_from_me = 1 THEN 'msg_sent' ELSE 'msg_recv' END as type,
               substr(content_preview, 1, 100) as detail
        FROM message_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp, 'claude_' || message_type as type,
               substr(content_preview, 1, 100) as detail
        FROM claude_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp, 'clipboard' as type, substr(content_text, 1, 80) as detail
        FROM clipboard_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp, 'app_' || event_type as type, app_name as detail
        FROM app_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    for row in conn.execute(
        """
        SELECT timestamp, 'notification' as type,
               app_name || ': ' || substr(content_preview, 1, 80) as detail
        FROM notification_events WHERE timestamp >= ? ORDER BY timestamp
    """,
        (cutoff,),
    ):
        events.append({"ts": row[0], "type": row[1], "detail": row[2]})

    events.sort(key=lambda e: e["ts"])
    return events


def classify_event(ev: dict) -> str:
    """Classify an event as 'action' (user-initiated) or 'stimulus' (incoming)."""
    stimuli_types = {"msg_recv", "notification", "app_launch"}
    if ev["type"] in stimuli_types:
        return "stimulus"
    return "action"


def format_event(ev: dict) -> str:
    """Single-line representation of an event."""
    return f"[{ev['type']}] {ev['detail'] or ''}"


def deduplicate_consecutive(events: list[dict]) -> list[dict]:
    """Remove consecutive duplicate events (same type + detail)."""
    if not events:
        return []
    result = [events[0]]
    for ev in events[1:]:
        prev = result[-1]
        if ev["type"] == prev["type"] and ev["detail"] == prev["detail"]:
            continue
        result.append(ev)
    return result


def build_dataset(events: list[dict]) -> list[dict]:
    """Build prediction dataset: for each action, context = past 5 min."""
    events = deduplicate_consecutive(events)
    dataset = []

    for i, ev in enumerate(events):
        if classify_event(ev) != "action":
            continue

        window_start = ev["ts"] - WINDOW_S
        past_actions = []
        stimuli = []
        for prev in events[:i]:
            if prev["ts"] < window_start:
                continue
            if classify_event(prev) == "stimulus":
                stimuli.append(format_event(prev))
            else:
                past_actions.append(format_event(prev))

        dataset.append(
            {
                "timestamp": ev["ts"],
                "time": time.strftime("%H:%M:%S", time.localtime(ev["ts"])),
                "past_5min": past_actions[-20:],
                "stimuli": stimuli[-10:],
                "action": format_event(ev),
            }
        )

    return dataset


def main() -> None:
    cutoff = time.time() - TODAY_HOURS * 3600
    conn = sqlite3.connect(str(DB_PATH))
    events = load_events(conn, cutoff)
    conn.close()

    dataset = build_dataset(events)

    out_path = DB_PATH.parent / "next_action_dataset.jsonl"
    with open(out_path, "w") as f:
        for row in dataset:
            f.write(json.dumps(row) + "\n")

    print(f"Dataset: {len(dataset)} rows")
    print(f"Saved to: {out_path}")
    print("\nSample rows:\n")
    for row in dataset[:3]:
        print(f"--- {row['time']} ---")
        print(f"  Past 5min ({len(row['past_5min'])} events):")
        for a in row["past_5min"][-5:]:
            print(f"    {a}")
        if row["stimuli"]:
            print(f"  Stimuli ({len(row['stimuli'])}):")
            for s in row["stimuli"]:
                print(f"    {s}")
        print(f"  → ACTION: {row['action']}")
        print()

    print("--- last 3 ---")
    for row in dataset[-3:]:
        print(f"\n--- {row['time']} ---")
        print(f"  Past 5min ({len(row['past_5min'])} events):")
        for a in row["past_5min"][-5:]:
            print(f"    {a}")
        if row["stimuli"]:
            print(f"  Stimuli ({len(row['stimuli'])}):")
            for s in row["stimuli"]:
                print(f"    {s}")
        print(f"  → ACTION: {row['action']}")


if __name__ == "__main__":
    main()
