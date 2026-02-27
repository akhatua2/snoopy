"""SFT dataset builder for next-action prediction.

Converts a cleaned action timeline into (past_actions → next_action)
training examples in Qwen chat format for mlx-lm fine-tuning.
"""

import json
import logging
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from linus.clean import SESSION_BREAK, Action, build_timeline

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_BASE = """\
You predict the user's next computer action from their recent activity log.

Action types:
- [focus] App: Title — user switched to this app/window
- [browse] Page Title — user visited this webpage
- [cmd] command — user ran a shell command
- [claude:user] text — user sent a message to Claude Code
- [clipboard] text — user copied this text
- [edit/create/delete] path — user modified a file
- [message:sent] contact — user sent a message
- [launch/quit] App — user opened/closed an app
- [lock/unlock] — user locked/unlocked their computer
- [mail:sent] sender: subject — user sent an email

Stimuli (context only, not predicted):
- [mail:recv] — incoming email
- [message:recv] — incoming message
- [notify] — push notification

Timestamps are [HH:MM:SS] before each action."""


# ── Ambient context (calendar, health) ──────────────────────────────────


def _load_calendar_events(conn: sqlite3.Connection) -> list[tuple[float, float, str]]:
    """Load all calendar events as (start_ts, end_ts, title)."""
    rows = conn.execute(
        "SELECT start_time, end_time, title FROM calendar_events WHERE status = 'active'"
    ).fetchall()
    results = []
    for start_str, end_str, title in rows:
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str) if end_str else start_dt
            results.append((start_dt.timestamp(), end_dt.timestamp(), title or ""))
        except (ValueError, TypeError):
            continue
    return results


def _load_oura_scores(conn: sqlite3.Connection) -> dict[str, tuple[int, int, int]]:
    """Load oura daily scores as {day_str: (sleep, readiness, activity)}."""
    rows = conn.execute(
        "SELECT day, sleep_score, readiness_score, activity_score FROM oura_daily"
    ).fetchall()
    return {day: (sleep or 0, ready or 0, activity or 0) for day, sleep, ready, activity in rows}


def _get_ambient_context(
    ts: float,
    calendar_events: list[tuple[float, float, str]],
    oura_scores: dict[str, tuple[int, int, int]],
) -> str:
    """Build ambient context string for a given timestamp."""
    parts = []

    # Upcoming calendar events (within next 2 hours)
    upcoming = []
    for start_ts, end_ts, title in calendar_events:
        delta_min = (start_ts - ts) / 60
        if -30 <= delta_min <= 120 and title:  # ongoing or upcoming
            if delta_min < 0:
                upcoming.append(f'"{title}" (ongoing)')
            elif delta_min < 5:
                upcoming.append(f'"{title}" (starting now)')
            else:
                upcoming.append(f'"{title}" in {int(delta_min)} min')
    # Dedup by title (same meeting can appear twice in DB)
    seen_titles: set[str] = set()
    deduped = []
    for entry in upcoming:
        title = entry.split('"')[1] if '"' in entry else entry
        if title not in seen_titles:
            seen_titles.add(title)
            deduped.append(entry)
    if deduped:
        parts.append("Calendar: " + ", ".join(deduped[:2]))

    # Oura scores for the day
    day_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    if day_str in oura_scores:
        sleep, readiness, activity = oura_scores[day_str]
        parts.append(f"Sleep: {sleep}, Readiness: {readiness}")

    return ". ".join(parts)


@dataclass
class DatasetConfig:
    context_window: int = 15
    min_context: int = 3
    min_gap_s: float = 1.0
    max_gap_s: float = 1800.0
    max_consecutive_same: int = 2
    rare_action_boost: int = 2


def _time_features(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%a %H:%M")


def _cap_consecutive(actions: list[str], max_n: int) -> list[str]:
    if not actions:
        return actions
    result = []
    count = 0
    prev_type = None
    for a in actions:
        atype = a.split("]", 1)[0] + "]" if "]" in a else a
        if atype == prev_type:
            count += 1
            if count > max_n:
                continue
        else:
            count = 1
            prev_type = atype
        result.append(a)
    return result


def _build_system_prompt(ambient: str) -> str:
    prompt = _SYSTEM_PROMPT_BASE
    if ambient:
        prompt += f"\n\nCurrent context:\n{ambient}"
    return prompt


def _format_context(
    actions: list[Action],
    target_ts: float,
    cfg: DatasetConfig,
) -> str:
    time_str = _time_features(target_ts)

    # Dedup on content (ignoring timestamps), then format with timestamps
    deduped: list[Action] = []
    for a in actions:
        if deduped and a.format() == deduped[-1].format():
            continue
        deduped.append(a)

    action_strs = [a.format(show_time=True) for a in deduped]
    action_strs = _cap_consecutive(action_strs, cfg.max_consecutive_same)

    lines = [f"Time: {time_str}. Recent actions:"]
    for i, a in enumerate(action_strs, 1):
        lines.append(f"{i}. {a}")

    lines.append("")
    lines.append("Predict the next action:")
    return "\n".join(lines)


_NON_USER_ACTIONS = frozenset(
    {
        SESSION_BREAK,
        # Stimuli — things that happen TO the user (useful context, not targets)
        "mail:recv",
        "message:recv",
        "notify",
        "mic:on",
        "mic:off",
        # Claude's actions — not the user's (user action is claude:user)
        "claude:Bash",
        "claude:Edit",
        "claude:Read",
        "claude:Write",
        "claude:Grep",
        "claude:Glob",
    }
)


def _is_predictable(action: Action) -> bool:
    """Whether this is a user-initiated action (not a stimulus or Claude's action)."""
    return action.action_type not in _NON_USER_ACTIONS


def _build_examples(
    timeline: list[Action],
    cfg: DatasetConfig,
    calendar_events: list[tuple[float, float, str]] | None = None,
    oura_scores: dict[str, tuple[int, int, int]] | None = None,
) -> list[dict]:
    calendar_events = calendar_events or []
    oura_scores = oura_scores or {}

    # Split at SESSION_BREAK markers
    sessions: list[list[Action]] = []
    current: list[Action] = []
    for a in timeline:
        if a.action_type == SESSION_BREAK:
            if current:
                sessions.append(current)
            current = []
        else:
            current.append(a)
    if current:
        sessions.append(current)

    examples = []
    for session in sessions:
        # All events go in the timeline (stimuli are visible context)
        all_events = [a for a in session if a.action_type != SESSION_BREAK]
        # But we only predict user-initiated actions
        target_indices = [i for i, a in enumerate(all_events) if _is_predictable(a)]

        for ti in target_indices:
            target = all_events[ti]
            # Context: the preceding events (all types, including stimuli)
            start = max(0, ti - cfg.context_window)
            context = all_events[start:ti]

            if len(context) < cfg.min_context:
                continue

            gap = target.timestamp - context[-1].timestamp
            if gap < cfg.min_gap_s or gap > cfg.max_gap_s:
                continue

            ambient = _get_ambient_context(target.timestamp, calendar_events, oura_scores)
            system_prompt = _build_system_prompt(ambient)
            prompt = _format_context(context, target.timestamp, cfg)
            target_text = target.format()

            examples.append(
                {
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": target_text},
                    ],
                    "_ts": target.timestamp,
                }
            )

    return examples


def _balance_examples(examples: list[dict], boost: int = 2) -> list[dict]:
    type_counts: Counter[str] = Counter()
    for ex in examples:
        target = ex["messages"][-1]["content"]
        atype = target.split("]", 1)[0] + "]" if "]" in target else target
        type_counts[atype] += 1

    if not type_counts:
        return examples

    median_count = sorted(type_counts.values())[len(type_counts) // 2]

    result = list(examples)
    for ex in examples:
        target = ex["messages"][-1]["content"]
        atype = target.split("]", 1)[0] + "]" if "]" in target else target
        count = type_counts[atype]
        if count < median_count:
            for _ in range(boost - 1):
                result.append(ex)

    return result


def _compute_stats(examples: list[dict], time_range: tuple[float, float]) -> dict:
    type_counts: Counter[str] = Counter()
    for ex in examples:
        target = ex["messages"][-1]["content"]
        atype = target.split("]", 1)[0] + "]" if "]" in target else target
        type_counts[atype] += 1

    return {
        "total_examples": len(examples),
        "action_distribution": dict(type_counts.most_common()),
        "time_range_start": time_range[0],
        "time_range_end": time_range[1],
        "built_at": time.time(),
    }


def build_dataset(
    db_path: str,
    output_dir: str | Path,
    since_ts: float = 0,
    until_ts: float | None = None,
    cfg: DatasetConfig | None = None,
) -> dict:
    if cfg is None:
        cfg = DatasetConfig()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("Building timeline from %s", db_path)
    timeline = build_timeline(db_path, since_ts, until_ts)
    log.info("Timeline: %d actions", len(timeline))

    if not timeline:
        log.warning("No actions found — empty dataset")
        return {"total_examples": 0, "action_distribution": {}}

    # Load ambient context data
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    try:
        calendar_events = _load_calendar_events(conn)
        oura_scores = _load_oura_scores(conn)
    finally:
        conn.close()
    log.info(
        "Ambient context: %d calendar events, %d oura days", len(calendar_events), len(oura_scores)
    )

    examples = _build_examples(timeline, cfg, calendar_events, oura_scores)
    log.info("Raw examples: %d", len(examples))

    # Sort by target timestamp
    examples.sort(key=lambda ex: ex["_ts"])

    # Time-based split: 90/10
    n = len(examples)
    train_end = int(n * 0.9)

    train = examples[:train_end]
    val = examples[train_end:]

    train = _balance_examples(train, cfg.rare_action_boost)

    def _strip(exs: list[dict]) -> list[dict]:
        return [{k: v for k, v in ex.items() if k != "_ts"} for ex in exs]

    for name, data in [("sft_train", _strip(train)), ("sft_val", _strip(val))]:
        path = output_dir / f"{name}.jsonl"
        with open(path, "w") as f:
            for ex in data:
                f.write(json.dumps(ex) + "\n")
        log.info("Wrote %s: %d examples", path, len(data))

    time_range = (timeline[0].timestamp, timeline[-1].timestamp)
    stats = _compute_stats(examples, time_range)
    stats["train_examples"] = len(train)
    stats["val_examples"] = len(val)

    stats_path = output_dir / "action_stats.json"
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2)
    log.info("Stats: %s", stats)

    return stats
