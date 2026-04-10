"""Document parsing package."""

from .base import BaseParser, ParseResult
from .content_understanding import FoundryParser

__all__ = ["BaseParser", "ParseResult", "FoundryParser", "parse_document"]


def parse_document(file_bytes: bytes, file_name: str) -> ParseResult:
    """Parse a document using Content Understanding with automatic fallback."""
    parser = FoundryParser()
    return parser.parse(file_bytes, file_name)
