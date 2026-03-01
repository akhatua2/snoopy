"""Zoom collector — tracks meeting sessions, participants, and meeting state.

Uses two data sources:
- NSWorkspace to check if Zoom is the active/focused app (free, no subprocess)
- osascript to scrape participant names + audio status from Accessibility
  (only runs when Zoom is focused AND in a meeting)
- Quartz CGWindowList for background-safe meeting detection and state
  (screen sharing, transcript, breakout rooms — works without focus)

Emits events on:
- Meeting start/end
- Participant list changes (joins/leaves/audio status changes)
"""

import json
import logging
import subprocess
import time

import Quartz
from AppKit import NSWorkspace

import snoopy.config as config
from snoopy.buffer import Event
from snoopy.collectors.base import BaseCollector

log = logging.getLogger(__name__)

_ZOOM_BUNDLE = "us.zoom.xos"

_PARTICIPANT_SCRIPT = '''
tell application "System Events"
    tell process "zoom.us"
        set winCount to count of windows
        set results to {}
        repeat with i from 1 to winCount
            set uiElements to every UI element of window i
            repeat with elem in uiElements
                try
                    set elemRole to role of elem
                    if elemRole is "AXTabGroup" then
                        set elemDesc to description of elem
                        set end of results to elemDesc
                    end if
                end try
            end repeat
        end repeat
        return results
    end tell
end tell
'''


def _zoom_is_frontmost() -> bool:
    """Check if Zoom is the active (focused) app via NSWorkspace."""
    active = NSWorkspace.sharedWorkspace().activeApplication()
    if not active:
        return False
    return active.get("NSApplicationBundleIdentifier", "") == _ZOOM_BUNDLE


def _get_zoom_windows() -> dict:
    """Get Zoom meeting state from Quartz window list (works in background)."""
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll,
        Quartz.kCGNullWindowID,
    )
    if not windows:
        return {}

    state = {
        "in_meeting": False,
        "meeting_topic": "",
        "screen_sharing": False,
        "transcript": False,
        "breakout_rooms": "",
    }

    for w in windows:
        if w.get("kCGWindowOwnerName", "") != "zoom.us":
            continue
        title = w.get("kCGWindowName", "") or ""
        bounds = w.get("kCGWindowBounds", {})
        width = bounds.get("Width", 0) if bounds else 0
        height = bounds.get("Height", 0) if bounds else 0
        if width == 0 or height == 0:
            continue

        if title == "Zoom Meeting" or (
            title and title not in (
                "Zoom Workplace", "Login", "Settings",
                "Zoom Client Healthcheck",
            )
            and "zoom share" not in title
            and "zoom floating" not in title
            and title != "Menu Window"
            and title != "Reactions"
            and title != "Select a window or an application that you want to share"
            and w.get("kCGWindowLayer", -1) == 0
            and width > 400
            and height > 300
        ):
            state["in_meeting"] = True
            if title != "Zoom Meeting":
                state["meeting_topic"] = title
        if "zoom share toolbar" in title:
            state["screen_sharing"] = True
        if title == "Transcript":
            state["transcript"] = True
        if title.startswith("Breakout rooms"):
            state["breakout_rooms"] = title

    return state


def _scrape_participants() -> list[dict]:
    """Scrape participant names and audio status via osascript."""
    try:
        result = subprocess.run(
            ["osascript", "-e", _PARTICIPANT_SCRIPT],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    raw = result.stdout.strip()
    parts = [p.strip() for p in raw.split(", ")]

    participants = []
    i = 0
    while i < len(parts):
        name = parts[i]
        if i + 1 < len(parts) and "audio" in parts[i + 1].lower():
            participants.append({
                "name": name,
                "audio_status": parts[i + 1],
            })
            i += 2
        else:
            participants.append({"name": name, "audio_status": ""})
            i += 1

    return participants


class ZoomCollector(BaseCollector):
    name = "zoom"
    interval = config.ZOOM_INTERVAL

    def setup(self) -> None:
        self._in_meeting = False
        self._last_participants_key: str | None = None
        self._meeting_start: float = 0.0
        self._meeting_topic: str = ""

    def collect(self) -> None:
        state = _get_zoom_windows()
        if not state:
            if self._in_meeting:
                self._end_meeting()
            return

        in_meeting = state.get("in_meeting", False)

        if not in_meeting:
            if self._in_meeting:
                self._end_meeting()
            return

        now = time.time()
        topic = state.get("meeting_topic", "") or "Zoom Meeting"

        # Meeting just started
        if not self._in_meeting:
            self._in_meeting = True
            self._meeting_start = now
            self._meeting_topic = topic
            self._last_participants_key = None
            self.buffer.push(Event(
                table="zoom_events",
                columns=[
                    "timestamp", "event_type", "meeting_topic",
                    "participants", "screen_sharing", "transcript_active",
                ],
                values=(
                    now, "meeting_start", topic,
                    "", state.get("screen_sharing", False),
                    state.get("transcript", False),
                ),
            ))

        # Only scrape participants when Zoom is focused
        if not _zoom_is_frontmost():
            return

        participants = _scrape_participants()
        if not participants:
            return

        # Deduplicate: only emit when participant list changes
        key = json.dumps(participants, sort_keys=True)
        if key == self._last_participants_key:
            return
        self._last_participants_key = key

        self.buffer.push(Event(
            table="zoom_events",
            columns=[
                "timestamp", "event_type", "meeting_topic",
                "participants", "screen_sharing", "transcript_active",
            ],
            values=(
                now, "participants", topic,
                json.dumps(participants),
                state.get("screen_sharing", False),
                state.get("transcript", False),
            ),
        ))

    def _end_meeting(self) -> None:
        now = time.time()
        duration = now - self._meeting_start if self._meeting_start else 0.0
        self.buffer.push(Event(
            table="zoom_events",
            columns=[
                "timestamp", "event_type", "meeting_topic",
                "participants", "screen_sharing", "transcript_active",
            ],
            values=(
                now, "meeting_end", self._meeting_topic,
                json.dumps({"duration_s": round(duration, 1)}),
                False, False,
            ),
        ))
        self._in_meeting = False
        self._last_participants_key = None
        self._meeting_start = 0.0
        self._meeting_topic = ""
