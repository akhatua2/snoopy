"""Tests for the Zoom meeting collector."""

import json
from unittest.mock import MagicMock, patch

from snoopy.collectors.zoom import (
    ZoomCollector,
    _get_zoom_windows,
    _scrape_participants,
)


def _make_collector():
    buf = MagicMock()
    db = MagicMock()
    c = ZoomCollector(buf, db)
    c.setup()
    return c, buf


def _quartz_window(owner, title, layer=0, width=800, height=600):
    return {
        "kCGWindowOwnerName": owner,
        "kCGWindowName": title,
        "kCGWindowLayer": layer,
        "kCGWindowBounds": {"Width": width, "Height": height, "X": 0, "Y": 0},
    }


class TestGetZoomWindows:
    def test_no_windows(self):
        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = []
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            assert _get_zoom_windows() == {}

    def test_meeting_detected(self):
        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
        ]
        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            state = _get_zoom_windows()
            assert state["in_meeting"] is True

    def test_screen_sharing_detected(self):
        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
            _quartz_window("zoom.us", "zoom share toolbar window", layer=97, width=700, height=50),
        ]
        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            state = _get_zoom_windows()
            assert state["screen_sharing"] is True

    def test_transcript_detected(self):
        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
            _quartz_window("zoom.us", "Transcript", layer=26, width=370, height=448),
        ]
        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            state = _get_zoom_windows()
            assert state["transcript"] is True

    def test_no_meeting_when_only_workplace(self):
        windows = [
            _quartz_window("zoom.us", "Zoom Workplace", layer=0, width=1512, height=863),
        ]
        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            state = _get_zoom_windows()
            assert state["in_meeting"] is False

    def test_non_zoom_windows_ignored(self):
        windows = [
            _quartz_window("Google Chrome", "Some Tab", layer=0),
        ]
        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            state = _get_zoom_windows()
            assert state["in_meeting"] is False


class TestScrapeParticipants:
    def test_parses_two_participants(self):
        stdout = "Alice Smith, Computer audio connected, Bob Jones, Computer audio muted\n"
        with patch("snoopy.collectors.zoom.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            result = _scrape_participants()
            assert len(result) == 2
            assert result[0]["name"] == "Alice Smith"
            assert result[0]["audio_status"] == "Computer audio connected"
            assert result[1]["name"] == "Bob Jones"
            assert result[1]["audio_status"] == "Computer audio muted"

    def test_empty_on_failure(self):
        with patch("snoopy.collectors.zoom.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _scrape_participants() == []

    def test_empty_on_timeout(self):
        import subprocess
        with patch("snoopy.collectors.zoom.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("osascript", 3)
            assert _scrape_participants() == []


class TestZoomCollector:
    def test_meeting_start_emitted(self):
        c, buf = _make_collector()
        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
        ]
        with patch("snoopy.collectors.zoom.Quartz") as mock_q, \
             patch("snoopy.collectors.zoom._scrape_participants", return_value=[]):
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.table == "zoom_events"
        assert event.values[1] == "meeting_start"

    def test_meeting_end_emitted(self):
        c, buf = _make_collector()
        c._in_meeting = True
        c._meeting_start = 1000.0
        c._meeting_topic = "Standup"

        with patch("snoopy.collectors.zoom.Quartz") as mock_q:
            mock_q.CGWindowListCopyWindowInfo.return_value = []
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "meeting_end"
        data = json.loads(event.values[3])
        assert "duration_s" in data

    def test_participants_emitted(self):
        c, buf = _make_collector()
        c._in_meeting = True
        c._meeting_start = 1000.0
        c._meeting_topic = "Standup"

        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
        ]
        stdout = "Alice, Computer audio muted\n"
        with patch("snoopy.collectors.zoom.Quartz") as mock_q, \
             patch("snoopy.collectors.zoom.subprocess.run") as mock_run:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "participants"
        participants = json.loads(event.values[3])
        assert participants[0]["name"] == "Alice"

    def test_participants_deduplicated(self):
        c, buf = _make_collector()
        c._in_meeting = True
        c._meeting_start = 1000.0
        c._meeting_topic = "Standup"

        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
        ]
        stdout = "Alice, Computer audio muted\n"
        with patch("snoopy.collectors.zoom.Quartz") as mock_q, \
             patch("snoopy.collectors.zoom.subprocess.run") as mock_run:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            c.collect()
            c.collect()

        # Only one event, second call deduplicated
        assert buf.push.call_count == 1

    def test_scrapes_participants_without_focus(self):
        """Participants are scraped even when Zoom is not the focused app."""
        c, buf = _make_collector()
        c._in_meeting = True
        c._meeting_start = 1000.0
        c._meeting_topic = "Standup"

        windows = [
            _quartz_window("zoom.us", "Zoom Meeting", layer=0, width=1600, height=900),
        ]
        stdout = "Bob, Computer audio connected\n"
        with patch("snoopy.collectors.zoom.Quartz") as mock_q, \
             patch("snoopy.collectors.zoom.subprocess.run") as mock_run:
            mock_q.CGWindowListCopyWindowInfo.return_value = windows
            mock_q.kCGWindowListOptionAll = 0
            mock_q.kCGNullWindowID = 0
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "participants"
