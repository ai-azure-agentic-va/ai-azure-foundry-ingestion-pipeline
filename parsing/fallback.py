"""Parser factory — routes file extensions to the correct parser."""

import logging
import os

from parsing.base import BaseParser, ParseResult
from parsing.docx import DocxParser
from parsing.markdown import MarkdownParser
from parsing.pdf import PdfParser
from parsing.pptx import PptxParser
from parsing.txt import TextParser
from parsing.xlsx import XlsxParser

logger = logging.getLogger(__name__)

_PARSERS: list[BaseParser] = [
    PdfParser(),
    DocxParser(),
    XlsxParser(),
    PptxParser(),
    MarkdownParser(),
    TextParser(),
]

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
            logger.warning(f"[ParserFactory] No parser for extension '{ext}' — rejecting '{file_name}'")
            return ParseResult(
                full_text="",
                metadata={"format": "unsupported", "error": f"No parser for extension: {ext}"},
            )

        logger.info(f"[ParserFactory] Using {parser.__class__.__name__} for '{file_name}'")
        return parser.parse(file_bytes)
