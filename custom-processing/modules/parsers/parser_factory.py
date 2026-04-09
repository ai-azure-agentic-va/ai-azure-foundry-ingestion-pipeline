"""Parser factory - routes file extension to the correct parser."""

import os
import logging
from .base import BaseParser, ParseResult
from .pdf_parser import PdfParser
from .docx_parser import DocxParser
from .xlsx_parser import XlsxParser
from .pptx_parser import PptxParser
from .markdown_parser import MarkdownParser
from .txt_parser import TextParser

logger = logging.getLogger(__name__)

# Registry of all parsers
_PARSERS: list[BaseParser] = [
    PdfParser(),
    DocxParser(),
    XlsxParser(),
    PptxParser(),
    MarkdownParser(),
    TextParser(),
]

# Build extension -> parser lookup
_EXTENSION_MAP: dict[str, BaseParser] = {}
for parser in _PARSERS:
    for ext in parser.supported_extensions:
        _EXTENSION_MAP[ext.lower()] = parser


class ParserFactory:
    """Route file to the correct parser based on extension."""

    @staticmethod
    def parse(file_bytes: bytes, file_name: str) -> ParseResult:
        ext = os.path.splitext(file_name)[1].lower()
        parser = _EXTENSION_MAP.get(ext)

        if parser is None:
            # Attempt plain text decode as fallback
            logger.warning(f"[ParserFactory] No parser for extension '{ext}', attempting text fallback")
            try:
                text = file_bytes.decode("utf-8", errors="replace")
                return ParseResult(full_text=text, metadata={"format": "fallback"})
            except Exception:
                logger.error(f"[ParserFactory] Cannot parse file '{file_name}' with extension '{ext}'")
                return ParseResult(full_text="", metadata={"format": "unsupported", "error": f"Unsupported extension: {ext}"})

        logger.info(f"[ParserFactory] Using {parser.__class__.__name__} for '{file_name}'")
        return parser.parse(file_bytes)
