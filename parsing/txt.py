"""Plain text and CSV parser."""

import logging

from parsing.base import BaseParser, ParseResult

logger = logging.getLogger(__name__)


class TextParser(BaseParser):
    """Parse plain text, CSV, and other text-based formats."""

    @property
    def supported_extensions(self) -> list[str]:
        return [".txt", ".csv", ".log", ".json", ".xml", ".html", ".htm"]

    def parse(self, file_bytes: bytes) -> ParseResult:
        logger.info(f"[TextParser] Parsing text file ({len(file_bytes)} bytes)")

        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1", errors="replace")

        logger.info(f"[TextParser] Extracted {len(text)} chars")
        return ParseResult(
            full_text=text,
            pages=[],
            page_count=1,
            metadata={"format": "text"},
        )
