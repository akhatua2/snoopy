"""Thin wrapper re-exporting Rust-accelerated parsers from snoopy_native."""

from snoopy_native import (
    extract_attributed_body_text,
    parse_lsof_output,
    parse_transcript,
)

__all__ = [
    "extract_attributed_body_text",
    "parse_lsof_output",
    "parse_transcript",
]
