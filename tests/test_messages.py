"""Tests for iMessage attributedBody blob parser (Rust native + Python fallback)."""

import pytest

from snoopy._native import extract_attributed_body_text as rs_extract
from snoopy._python_parsers import extract_attributed_body_text as py_extract


def _make_blob(text: str) -> bytes:
    """Build a minimal NSArchiver-style blob embedding the given text."""
    text_bytes = text.encode("utf-8")
    return (
        b"\x00" * 10
        + b"NSString\x01\x94\x84\x01+"
        + bytes([len(text_bytes)])
        + text_bytes
        + b"\x00" * 10
    )


@pytest.mark.parametrize("extract", [rs_extract, py_extract], ids=["rust", "python"])
class TestExtractAttributedBodyText:
    def test_basic_extraction(self, extract):
        blob = _make_blob("Hello world")
        assert extract(blob) == "Hello world"

    def test_empty_blob(self, extract):
        assert extract(b"") == ""

    def test_no_nsstring_marker(self, extract):
        assert extract(b"\x00\x01\x02\x03random data") == ""

    def test_no_plus_marker(self, extract):
        blob = b"\x00NSString\x00\x00\x00end"
        assert extract(blob) == ""

    def test_unicode_text(self, extract):
        blob = _make_blob("caf\u00e9")
        assert extract(blob) == "caf\u00e9"

    def test_length_byte_at_end(self, extract):
        """Blob ends right after the length byte — no text to read."""
        blob = b"\x00NSString\x01+"
        assert extract(blob) == ""

    def test_truncated_text(self, extract):
        """Length byte says 20 but only 5 bytes remain."""
        text = b"short"
        blob = b"\x00NSString\x01+" + bytes([20]) + text
        result = extract(blob)
        assert "short" in result
