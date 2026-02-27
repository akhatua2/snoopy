"""Tests for iMessage attributedBody blob parser (Rust native via PyO3)."""

from snoopy._native import extract_attributed_body_text


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


class TestExtractAttributedBodyText:
    def test_basic_extraction(self):
        blob = _make_blob("Hello world")
        assert extract_attributed_body_text(blob) == "Hello world"

    def test_empty_blob(self):
        assert extract_attributed_body_text(b"") == ""

    def test_no_nsstring_marker(self):
        assert extract_attributed_body_text(b"\x00\x01\x02\x03random data") == ""

    def test_no_plus_marker(self):
        blob = b"\x00NSString\x00\x00\x00end"
        assert extract_attributed_body_text(blob) == ""

    def test_unicode_text(self):
        blob = _make_blob("caf\u00e9")
        assert extract_attributed_body_text(blob) == "caf\u00e9"

    def test_length_byte_at_end(self):
        """Blob ends right after the length byte — no text to read."""
        blob = b"\x00NSString\x01+"
        assert extract_attributed_body_text(blob) == ""

    def test_truncated_text(self):
        """Length byte says 20 but only 5 bytes remain."""
        text = b"short"
        blob = b"\x00NSString\x01+" + bytes([20]) + text
        result = extract_attributed_body_text(blob)
        assert "short" in result
