"""Tests for the WhatsApp message collector."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from snoopy.collectors.whatsapp import (
    WhatsAppCollector,
    _fetch_whatsapp,
    _whatsapp_is_frontmost,
)


def _make_collector():
    buf = MagicMock()
    db = MagicMock()
    c = WhatsAppCollector(buf, db)
    c.setup()
    return c, buf


def _sample_data():
    return {
        "chat_name": "Alice",
        "chat_members": "",
        "messages": [
            {
                "sender": "Alice",
                "text": "Hey, are you free tonight?",
                "timestamp": "3:42 PM",
            },
            {
                "sender": "You",
                "text": "Yeah, what's up?",
                "timestamp": "3:43 PM",
            },
        ],
        "chat_list": [
            {
                "name": "Alice",
                "last_message": "Yeah, what's up?",
                "timestamp": "3:43 PM",
            },
            {
                "name": "Family Group",
                "last_message": "Photo",
                "timestamp": "2:00 PM",
                "status": "Muted",
            },
        ],
    }


class TestWhatsAppIsFrontmost:
    def test_whatsapp_focused(self):
        mock_app = {"NSApplicationBundleIdentifier": "net.whatsapp.WhatsApp"}
        with patch("snoopy.collectors.whatsapp.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = mock_app
            assert _whatsapp_is_frontmost() is True

    def test_other_app_focused(self):
        mock_app = {"NSApplicationBundleIdentifier": "com.google.Chrome"}
        with patch("snoopy.collectors.whatsapp.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = mock_app
            assert _whatsapp_is_frontmost() is False

    def test_no_active_app(self):
        with patch("snoopy.collectors.whatsapp.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = None
            assert _whatsapp_is_frontmost() is False


class TestFetchWhatsApp:
    def test_success(self):
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(data)
            )
            result = _fetch_whatsapp()
            assert result is not None
            assert result["chat_name"] == "Alice"
            assert len(result["messages"]) == 2
            assert len(result["chat_list"]) == 2

    def test_timeout(self):
        with patch("snoopy.collectors.whatsapp.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("whatsapp_helper", 10)
            assert _fetch_whatsapp() is None

    def test_helper_not_found(self):
        with patch("snoopy.collectors.whatsapp.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert _fetch_whatsapp() is None

    def test_nonzero_exit(self):
        with patch("snoopy.collectors.whatsapp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _fetch_whatsapp() is None

    def test_invalid_json(self):
        with patch("snoopy.collectors.whatsapp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json{")
            assert _fetch_whatsapp() is None

    def test_empty_stdout(self):
        with patch("snoopy.collectors.whatsapp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _fetch_whatsapp() is None


class TestWhatsAppCollector:
    def test_emits_on_new_view(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.table == "whatsapp_events"
        assert event.values[1] == "Alice"
        messages = json.loads(event.values[3])
        assert len(messages) == 2
        assert messages[0]["sender"] == "Alice"
        assert messages[0]["text"] == "Hey, are you free tonight?"
        chat_list = json.loads(event.values[4])
        assert len(chat_list) == 2
        assert chat_list[0]["name"] == "Alice"

    def test_deduplicated_same_view(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 1

    def test_emits_on_chat_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = {
            "chat_name": "Bob",
            "chat_members": "",
            "messages": [
                {"sender": "Bob", "text": "lunch?", "timestamp": "4:00 PM"},
            ],
            "chat_list": data1["chat_list"],
        }
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 2

    def test_emits_on_new_message(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"] = data2["messages"] + [
            {"sender": "Alice", "text": "Let's grab dinner", "timestamp": "3:45 PM"},
        ]
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 2

    def test_no_collect_when_not_focused(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=False), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp") as mock_fetch:
            c.collect()

        assert buf.push.call_count == 0
        mock_fetch.assert_not_called()

    def test_no_emit_on_error(self):
        c, buf = _make_collector()
        error_data = {"error": "whatsapp_not_running"}
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=error_data):
            c.collect()

        assert buf.push.call_count == 0

    def test_no_emit_on_empty_data(self):
        c, buf = _make_collector()
        data = {"chat_name": "", "messages": [], "chat_list": []}
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            c.collect()

        assert buf.push.call_count == 0

    def test_no_emit_on_fetch_failure(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=None):
            c.collect()

        assert buf.push.call_count == 0

    def test_full_message_data_preserved(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        messages = json.loads(event.values[3])
        assert messages[1]["sender"] == "You"
        assert messages[1]["text"] == "Yeah, what's up?"
        assert messages[1]["timestamp"] == "3:43 PM"

    def test_chat_list_preserved(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        chat_list = json.loads(event.values[4])
        assert chat_list[1]["name"] == "Family Group"
        assert chat_list[1]["status"] == "Muted"

    def test_throttled_while_focused(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data) as mock_fetch:
            c.collect()
            c.collect()

        assert mock_fetch.call_count == 1
        assert buf.push.call_count == 1

    def test_focus_out_final_scrape(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"] = data2["messages"] + [
            {"sender": "Alice", "text": "sent right before switching", "timestamp": "3:45 PM"},
        ]
        with patch("snoopy.collectors.whatsapp._fetch_whatsapp", side_effect=[data1, data2]):
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=False):
                c.collect()

        assert buf.push.call_count == 2

    def test_focus_out_no_scrape_when_not_previously_focused(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=False), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp") as mock_fetch:
            c.collect()
            c.collect()

        mock_fetch.assert_not_called()
        assert buf.push.call_count == 0

    def test_focus_out_deduplicates(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=False):
                c.collect()

        assert buf.push.call_count == 1

    def test_immediate_on_refocus(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["messages"][0]["text"] = "edited message"
        with patch("snoopy.collectors.whatsapp._fetch_whatsapp", side_effect=[data1, data1, data2]):
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=False):
                c.collect()
            with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True):
                c.collect()

        assert buf.push.call_count == 2

    def test_group_chat_with_members(self):
        c, buf = _make_collector()
        data = {
            "chat_name": "Family Group",
            "chat_members": "Mom, Dad, Sister",
            "messages": [
                {"sender": "Mom", "text": "Dinner at 7?", "timestamp": "5:00 PM"},
            ],
            "chat_list": [],
        }
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        assert event.values[1] == "Family Group"
        assert event.values[2] == "Mom, Dad, Sister"

    def test_emits_on_chat_list_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["chat_list"] = data2["chat_list"] + [
            {"name": "Charlie", "last_message": "Hey!", "timestamp": "5:00 PM"},
        ]
        with patch("snoopy.collectors.whatsapp._whatsapp_is_frontmost", return_value=True), \
             patch("snoopy.collectors.whatsapp._fetch_whatsapp", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 2
