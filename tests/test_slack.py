"""Tests for the Slack message collector."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from snoopy.collectors.slack import (
    SlackCollector,
    _fetch_messages,
    _slack_is_frontmost,
)


def _make_collector():
    buf = MagicMock()
    db = MagicMock()
    c = SlackCollector(buf, db)
    c.setup()
    return c, buf


def _sample_data():
    return {
        "workspace": "TestCo",
        "channel_name": "#general",
        "messages": [
            {
                "sender": "Alice",
                "text": "shipped the fix",
                "timestamp": "Today at 3:42:15 PM",
                "reactions": [],
                "is_edited": False,
                "thread_replies": "",
            },
            {
                "sender": "Bob",
                "text": "nice work!",
                "timestamp": "Today at 3:43:00 PM",
                "reactions": [{"emoji": "+1 emoji", "count": "2"}],
                "is_edited": False,
                "thread_replies": "",
            },
        ],
    }


class TestSlackIsFrontmost:
    def test_slack_focused(self):
        mock_app = {"NSApplicationBundleIdentifier": "com.tinyspeck.slackmacgap"}
        with patch("snoopy.collectors.slack.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = mock_app
            assert _slack_is_frontmost() is True

    def test_other_app_focused(self):
        mock_app = {"NSApplicationBundleIdentifier": "com.google.Chrome"}
        with patch("snoopy.collectors.slack.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = mock_app
            assert _slack_is_frontmost() is False

    def test_no_active_app(self):
        with patch("snoopy.collectors.slack.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = None
            assert _slack_is_frontmost() is False


class TestFetchMessages:
    def test_success(self):
        data = _sample_data()
        with patch("snoopy.collectors.slack.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(data)
            )
            result = _fetch_messages()
            assert result is not None
            assert result["workspace"] == "TestCo"
            assert len(result["messages"]) == 2

    def test_timeout(self):
        with patch("snoopy.collectors.slack.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("slack_helper", 10)
            assert _fetch_messages() is None

    def test_helper_not_found(self):
        with patch("snoopy.collectors.slack.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert _fetch_messages() is None

    def test_nonzero_exit(self):
        with patch("snoopy.collectors.slack.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _fetch_messages() is None

    def test_invalid_json(self):
        with patch("snoopy.collectors.slack.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json{")
            assert _fetch_messages() is None

    def test_empty_stdout(self):
        with patch("snoopy.collectors.slack.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _fetch_messages() is None


class TestSlackCollector:
    def test_emits_on_new_view(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=data):
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.table == "slack_events"
        assert event.values[1] == "TestCo"
        assert event.values[2] == "#general"
        messages = json.loads(event.values[3])
        assert len(messages) == 2
        assert messages[0]["sender"] == "Alice"
        assert messages[0]["text"] == "shipped the fix"

    def test_deduplicated_same_view(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=data):
            c.collect()
            c._last_fetch_ts = 0  # simulate 10s elapsed
            c.collect()

        assert buf.push.call_count == 1

    def test_emits_on_channel_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = {
            "workspace": "TestCo",
            "channel_name": "#random",
            "messages": [
                {
                    "sender": "Charlie",
                    "text": "hey!",
                    "timestamp": "Today at 4:00:00 PM",
                    "reactions": [],
                    "is_edited": False,
                    "thread_replies": "",
                },
            ],
        }
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0  # simulate 10s elapsed
            c.collect()

        assert buf.push.call_count == 2

    def test_emits_on_new_message(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"] = data2["messages"] + [
            {
                "sender": "Arpan",
                "text": "thanks!",
                "timestamp": "Today at 3:44:00 PM",
                "reactions": [],
                "is_edited": False,
                "thread_replies": "",
            },
        ]
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0  # simulate 10s elapsed
            c.collect()

        assert buf.push.call_count == 2

    def test_emits_on_reaction_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"][0]["reactions"] = [{"emoji": "+1 emoji", "count": "1"}]
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0  # simulate 10s elapsed
            c.collect()

        assert buf.push.call_count == 2

    def test_no_collect_when_not_focused(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=False), \
             patch("snoopy.collectors.slack._fetch_messages") as mock_fetch:
            c.collect()

        assert buf.push.call_count == 0
        mock_fetch.assert_not_called()

    def test_no_emit_on_error(self):
        c, buf = _make_collector()
        error_data = {"error": "slack_not_running"}
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=error_data):
            c.collect()

        assert buf.push.call_count == 0

    def test_no_emit_on_empty_messages(self):
        c, buf = _make_collector()
        data = {"workspace": "TestCo", "channel_name": "#general", "messages": []}
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=data):
            c.collect()

        assert buf.push.call_count == 0

    def test_no_emit_on_fetch_failure(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=None):
            c.collect()

        assert buf.push.call_count == 0

    def test_full_message_data_preserved(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        messages = json.loads(event.values[3])
        bob = messages[1]
        assert bob["sender"] == "Bob"
        assert bob["text"] == "nice work!"
        assert bob["timestamp"] == "Today at 3:43:00 PM"
        assert bob["reactions"] == [{"emoji": "+1 emoji", "count": "2"}]
        assert bob["is_edited"] is False
        assert bob["thread_replies"] == ""

    def test_unread_notifications_captured(self):
        c, buf = _make_collector()
        data = _sample_data()
        data["unread"] = [
            {
                "name": "Kevin Xiang Li",
                "description": "@Kevin Xiang Li (active) (has 1 notification)",
                "unread_count": 1,
            },
        ]
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        unread = json.loads(event.values[4])
        assert len(unread) == 1
        assert unread[0]["name"] == "Kevin Xiang Li"
        assert unread[0]["unread_count"] == 1

    def test_emits_on_unread_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["unread"] = [
            {
                "name": "Kevin Xiang Li",
                "description": "@Kevin Xiang Li (active) (has 1 notification)",
                "unread_count": 1,
            },
        ]
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0  # simulate 10s elapsed
            c.collect()

        assert buf.push.call_count == 2

    def test_throttled_while_focused(self):
        """Second collect within 10s is throttled (no fetch)."""
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True), \
             patch("snoopy.collectors.slack._fetch_messages", return_value=data) as mock_fetch:
            c.collect()
            c.collect()  # within 10s — should be throttled

        assert mock_fetch.call_count == 1
        assert buf.push.call_count == 1

    def test_focus_out_final_scrape(self):
        """When Slack loses focus, one final scrape captures last-second activity."""
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"] = data2["messages"] + [
            {
                "sender": "Arpan",
                "text": "sent right before switching",
                "timestamp": "Today at 3:45:00 PM",
                "reactions": [],
                "is_edited": False,
                "thread_replies": "",
            },
        ]
        with patch("snoopy.collectors.slack._fetch_messages", side_effect=[data1, data2]):
            # First collect: Slack is focused
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True):
                c.collect()
            # Second collect: Slack just lost focus — triggers final scrape
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=False):
                c.collect()

        assert buf.push.call_count == 2

    def test_focus_out_no_scrape_when_not_previously_focused(self):
        """No final scrape if Slack wasn't focused on the previous tick."""
        c, buf = _make_collector()
        with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=False), \
             patch("snoopy.collectors.slack._fetch_messages") as mock_fetch:
            c.collect()
            c.collect()

        mock_fetch.assert_not_called()
        assert buf.push.call_count == 0

    def test_focus_out_deduplicates(self):
        """Focus-out scrape doesn't emit if data hasn't changed."""
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.slack._fetch_messages", return_value=data):
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=False):
                c.collect()

        # Same data both times — only 1 emit
        assert buf.push.call_count == 1

    def test_immediate_on_refocus(self):
        """After switching away and back, first scrape is immediate."""
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"][0]["text"] = "edited message"
        with patch("snoopy.collectors.slack._fetch_messages", side_effect=[data1, data1, data2]):
            # Focus Slack
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True):
                c.collect()
            # Leave Slack (final scrape, same data = no emit)
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=False):
                c.collect()
            # Re-focus Slack — should fetch immediately (no throttle)
            with patch("snoopy.collectors.slack._slack_is_frontmost", return_value=True):
                c.collect()

        assert buf.push.call_count == 2  # first focus + refocus with changed data
