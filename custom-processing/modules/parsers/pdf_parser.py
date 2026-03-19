"""PDF parser using PyMuPDF (fitz) - no Azure Document Intelligence."""

import logging
import fitz  # PyMuPDF
from .base import BaseParser, ParseResult

logger = logging.getLogger(__name__)


class PdfParser(BaseParser):

    @property
    def supported_extensions(self) -> list[str]:
        return [".pdf"]

    def parse(self, file_bytes: bytes) -> ParseResult:
        logger.info(f"[PdfParser] Parsing PDF ({len(file_bytes)} bytes)")
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text("text")
            table_text = ""

            # Extract tables if available (PyMuPDF >= 1.23.0)
            try:
                tables = page.find_tables()
                if tables and tables.tables:
                    for table in tables.tables:
                        df = table.to_pandas()
                        table_text += df.to_string(index=False) + "\n"
            except Exception as e:
                logger.debug(f"[PdfParser] Table extraction skipped for page {page_num + 1}: {e}")

            pages.append({
                "page_number": page_num + 1,
                "text": text.strip(),
                "table_text": table_text.strip(),
            })

        doc.close()

        full_text = "\n\n".join(
            p["text"] + ("\n" + p["table_text"] if p["table_text"] else "")
            for p in pages
        )

        logger.info(f"[PdfParser] Extracted {len(pages)} pages, {len(full_text)} chars")
        return ParseResult(
            full_text=full_text,
            pages=pages,
            page_count=len(pages),
            metadata={"format": "pdf"},
        )
