"""Tests for the Dock badge & status collector."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from snoopy.collectors.dock import DockCollector, _fetch_dock_items


def _make_collector():
    buf = MagicMock()
    db = MagicMock()
    c = DockCollector(buf, db)
    c.setup()
    return c, buf


def _sample_items():
    return [
        {"app": "Mail", "badge": "3", "running": True},
        {"app": "Slack", "badge": "•", "running": True},
        {"app": "Calendar", "badge": "", "running": True},
        {"app": "Notes", "badge": "", "running": False},
    ]


class TestFetchDockItems:
    def test_success(self):
        items = _sample_items()
        with patch("snoopy.collectors.dock.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(items)
            )
            result = _fetch_dock_items()
            assert result is not None
            assert len(result) == 4
            assert result[0]["app"] == "Mail"
            assert result[0]["badge"] == "3"

    def test_timeout(self):
        with patch("snoopy.collectors.dock.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("dock_helper", 5)
            assert _fetch_dock_items() is None

    def test_helper_not_found(self):
        with patch("snoopy.collectors.dock.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert _fetch_dock_items() is None

    def test_nonzero_exit(self):
        with patch("snoopy.collectors.dock.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _fetch_dock_items() is None

    def test_invalid_json(self):
        with patch("snoopy.collectors.dock.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json{")
            assert _fetch_dock_items() is None

    def test_empty_stdout(self):
        with patch("snoopy.collectors.dock.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _fetch_dock_items() is None


class TestDockCollector:
    def test_first_run_emits_nothing(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.dock._fetch_dock_items", return_value=_sample_items()):
            c.collect()
        assert buf.push.call_count == 0

    def test_badge_change_emits_event(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items()
        items2[0]["badge"] = "5"  # Mail: 3 → 5
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()  # first run — records state
            c.collect()  # second run — detects change
        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.table == "dock_events"
        assert event.values[1] == "badge_change"
        assert event.values[2] == "Mail"
        assert event.values[3] == "5"
        assert event.values[4] == "3"

    def test_badge_appear(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items()
        items2[2]["badge"] = "2"  # Calendar: "" → "2"
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "badge_change"
        assert event.values[2] == "Calendar"
        assert event.values[3] == "2"
        assert event.values[4] == ""

    def test_badge_clear(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items()
        items2[0]["badge"] = ""  # Mail: "3" → ""
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "badge_change"
        assert event.values[2] == "Mail"
        assert event.values[3] == ""
        assert event.values[4] == "3"

    def test_app_started_running(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items()
        items2[3]["running"] = True  # Notes: False → True
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "app_active"
        assert event.values[2] == "Notes"

    def test_app_stopped_running(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items()
        items2[0]["running"] = False  # Mail: True → False
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "app_inactive"
        assert event.values[2] == "Mail"

    def test_no_change_no_emit(self):
        c, buf = _make_collector()
        items = _sample_items()
        with patch("snoopy.collectors.dock._fetch_dock_items", return_value=items):
            c.collect()
            c.collect()
        assert buf.push.call_count == 0

    def test_multiple_changes_multiple_events(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items()
        items2[0]["badge"] = "5"  # Mail badge change
        items2[3]["running"] = True  # Notes started
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        assert buf.push.call_count == 2

    def test_fetch_failure_no_crash(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.dock._fetch_dock_items", return_value=None):
            c.collect()
        assert buf.push.call_count == 0

    def test_app_disappeared_from_dock(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = [i for i in _sample_items() if i["app"] != "Mail"]
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.values[1] == "app_inactive"
        assert event.values[2] == "Mail"

    def test_new_app_in_dock_running_with_badge(self):
        c, buf = _make_collector()
        items1 = _sample_items()
        items2 = _sample_items() + [{"app": "Discord", "badge": "5", "running": True}]
        with patch("snoopy.collectors.dock._fetch_dock_items", side_effect=[items1, items2]):
            c.collect()
            c.collect()
        # Should emit both app_active and badge_change
        assert buf.push.call_count == 2
        events = [buf.push.call_args_list[i][0][0] for i in range(2)]
        types = {e.values[1] for e in events}
        assert "app_active" in types
        assert "badge_change" in types
