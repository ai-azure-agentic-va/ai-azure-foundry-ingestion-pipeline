"""Markdown parser - plain text read, no external library needed."""

import logging
from .base import BaseParser, ParseResult

logger = logging.getLogger(__name__)


class MarkdownParser(BaseParser):

    @property
    def supported_extensions(self) -> list[str]:
        return [".md", ".markdown"]

    def parse(self, file_bytes: bytes) -> ParseResult:
        logger.info(f"[MarkdownParser] Parsing Markdown ({len(file_bytes)} bytes)")
        text = file_bytes.decode("utf-8", errors="replace")

        logger.info(f"[MarkdownParser] Extracted {len(text)} chars")
        return ParseResult(
            full_text=text,
            pages=[],
            page_count=1,
            metadata={"format": "markdown"},
        )
