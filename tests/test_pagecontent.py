"""Tests for the Chrome page content collector."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from snoopy.collectors.pagecontent import (
    PageContentCollector,
    _chrome_is_frontmost,
    _extract_domain,
    _fetch_page_content,
)


def _make_collector():
    buf = MagicMock()
    db = MagicMock()
    c = PageContentCollector(buf, db)
    c.setup()
    return c, buf


def _sample_data():
    return {
        "url": "https://www.instagram.com/direct/inbox/",
        "title": "Instagram",
        "content": [
            {"type": "heading", "text": "Messages"},
            {"type": "text", "text": "Alice sent you a message"},
            {"type": "link", "text": "Alice"},
            {"type": "text", "text": "Hey, check this out!"},
            {"type": "image", "text": "Profile photo"},
        ],
    }


class TestChromeIsFrontmost:
    def test_chrome_focused(self):
        mock_app = {"NSApplicationBundleIdentifier": "com.google.Chrome"}
        with patch("snoopy.collectors.pagecontent.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = mock_app
            assert _chrome_is_frontmost() is True

    def test_other_app_focused(self):
        mock_app = {"NSApplicationBundleIdentifier": "com.tinyspeck.slackmacgap"}
        with patch("snoopy.collectors.pagecontent.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = mock_app
            assert _chrome_is_frontmost() is False

    def test_no_active_app(self):
        with patch("snoopy.collectors.pagecontent.NSWorkspace") as mock_ws:
            mock_ws.sharedWorkspace().activeApplication.return_value = None
            assert _chrome_is_frontmost() is False


class TestFetchPageContent:
    def test_success(self):
        data = _sample_data()
        with patch("snoopy.collectors.pagecontent.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(data)
            )
            result = _fetch_page_content()
            assert result is not None
            assert result["url"] == "https://www.instagram.com/direct/inbox/"
            assert result["title"] == "Instagram"
            assert len(result["content"]) == 5

    def test_timeout(self):
        with patch("snoopy.collectors.pagecontent.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("chrome_helper", 10)
            assert _fetch_page_content() is None

    def test_helper_not_found(self):
        with patch("snoopy.collectors.pagecontent.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            assert _fetch_page_content() is None

    def test_nonzero_exit(self):
        with patch("snoopy.collectors.pagecontent.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _fetch_page_content() is None

    def test_invalid_json(self):
        with patch("snoopy.collectors.pagecontent.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json{")
            assert _fetch_page_content() is None

    def test_empty_stdout(self):
        with patch("snoopy.collectors.pagecontent.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _fetch_page_content() is None


class TestExtractDomain:
    def test_standard_url(self):
        assert _extract_domain("https://www.instagram.com/direct/inbox/") == "www.instagram.com"

    def test_subdomain(self):
        assert _extract_domain("https://chat.openai.com/c/abc123") == "chat.openai.com"

    def test_no_scheme(self):
        assert _extract_domain("instagram.com/direct") == ""

    def test_empty_url(self):
        assert _extract_domain("") == ""


class TestPageContentCollector:
    def test_emits_on_new_page(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            c.collect()

        assert buf.push.call_count == 1
        event = buf.push.call_args[0][0]
        assert event.table == "page_content_events"
        assert event.values[1] == "https://www.instagram.com/direct/inbox/"
        assert event.values[2] == "www.instagram.com"
        assert event.values[3] == "Instagram"
        content = json.loads(event.values[4])
        assert len(content) == 5
        assert content[0] == {"type": "heading", "text": "Messages"}

    def test_deduplicated_same_page(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 1

    def test_emits_on_url_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = {
            "url": "https://chatgpt.com/c/abc",
            "title": "ChatGPT",
            "content": [
                {"type": "text", "text": "Hello! How can I help?"},
            ],
        }
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 2
        event2 = buf.push.call_args[0][0]
        assert event2.values[1] == "https://chatgpt.com/c/abc"
        assert event2.values[2] == "chatgpt.com"

    def test_emits_on_content_change(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["content"] = data2["content"] + [
            {"type": "text", "text": "New message just appeared"},
        ]
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", side_effect=[data1, data2]):
            c.collect()
            c._last_fetch_ts = 0
            c.collect()

        assert buf.push.call_count == 2

    def test_no_collect_when_not_focused(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=False), \
             patch("snoopy.collectors.pagecontent._fetch_page_content") as mock_fetch:
            c.collect()

        assert buf.push.call_count == 0
        mock_fetch.assert_not_called()

    def test_no_emit_on_error(self):
        c, buf = _make_collector()
        error_data = {"error": "chrome_not_running"}
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=error_data):
            c.collect()

        assert buf.push.call_count == 0

    def test_no_emit_on_empty_content(self):
        c, buf = _make_collector()
        data = {"url": "", "title": "", "content": []}
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            c.collect()

        assert buf.push.call_count == 0

    def test_no_emit_on_fetch_failure(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=None):
            c.collect()

        assert buf.push.call_count == 0

    def test_full_content_data_preserved(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        content = json.loads(event.values[4])
        assert content[1] == {"type": "text", "text": "Alice sent you a message"}
        assert content[2] == {"type": "link", "text": "Alice"}
        assert content[4] == {"type": "image", "text": "Profile photo"}

    def test_throttled_while_focused(self):
        c, buf = _make_collector()
        data = _sample_data()
        fetch = "snoopy.collectors.pagecontent._fetch_page_content"
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch(fetch, return_value=data) as mock_fetch:
            c.collect()
            c.collect()

        assert mock_fetch.call_count == 1
        assert buf.push.call_count == 1

    def test_focus_out_final_scrape(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = _sample_data()
        data2["content"] = data2["content"] + [
            {"type": "text", "text": "Last second update"},
        ]
        with patch("snoopy.collectors.pagecontent._fetch_page_content", side_effect=[data1, data2]):
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=False):
                c.collect()

        assert buf.push.call_count == 2

    def test_focus_out_no_scrape_when_not_previously_focused(self):
        c, buf = _make_collector()
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=False), \
             patch("snoopy.collectors.pagecontent._fetch_page_content") as mock_fetch:
            c.collect()
            c.collect()

        mock_fetch.assert_not_called()
        assert buf.push.call_count == 0

    def test_focus_out_deduplicates(self):
        c, buf = _make_collector()
        data = _sample_data()
        with patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=False):
                c.collect()

        assert buf.push.call_count == 1

    def test_immediate_on_refocus(self):
        c, buf = _make_collector()
        data1 = _sample_data()
        data2 = {
            "url": "https://chatgpt.com/",
            "title": "ChatGPT",
            "content": [{"type": "text", "text": "new page"}],
        }
        fetch = "snoopy.collectors.pagecontent._fetch_page_content"
        with patch(fetch, side_effect=[data1, data1, data2]):
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True):
                c.collect()
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=False):
                c.collect()
            with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True):
                c.collect()

        assert buf.push.call_count == 2

    def test_emits_with_url_only(self):
        c, buf = _make_collector()
        data = {
            "url": "https://example.com",
            "title": "",
            "content": [],
        }
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            c.collect()

        assert buf.push.call_count == 1

    def test_domain_extraction(self):
        c, buf = _make_collector()
        data = {
            "url": "https://wandb.ai/team/project/runs/abc123",
            "title": "W&B Dashboard",
            "content": [{"type": "text", "text": "Training loss: 0.42"}],
        }
        with patch("snoopy.collectors.pagecontent._chrome_is_frontmost", return_value=True), \
             patch("snoopy.collectors.pagecontent._fetch_page_content", return_value=data):
            c.collect()

        event = buf.push.call_args[0][0]
        assert event.values[2] == "wandb.ai"
