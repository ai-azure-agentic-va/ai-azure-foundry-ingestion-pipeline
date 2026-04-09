"""Token-based text chunking using tiktoken + langchain splitters.
No Azure AI services - all local computation.

Chunking strategy is configurable per file type via env vars:
  CHUNK_STRATEGY_MD=header_based     (default: header_based)
  CHUNK_STRATEGY_XLSX=sheet_based    (default: sheet_based)
  CHUNK_STRATEGY_PDF=semantic        (default: semantic)
  CHUNK_STRATEGY_DEFAULT=recursive   (default: recursive)
  CHUNK_SIZE_TOKENS=1024             (default: 1024)
  CHUNK_OVERLAP_TOKENS=200           (default: 200)
"""

import base64
import logging
import os
import re
from datetime import datetime, timezone

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


class TokenChunker:
    """Split text into token-sized chunks for standard documents (PDF, DOCX, etc.)."""

    def __init__(
        self,
        chunk_size: int = 1024,
        chunk_overlap: int = 200,
        encoding: str = "cl100k_base",
    ):
        self.enc = tiktoken.get_encoding(encoding)
        self.splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name=encoding,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", ". ", " ", ""],
        )

    def chunk(self, text: str, metadata: dict) -> list[dict]:
        if not text.strip():
            logger.warning("[TokenChunker] Empty text, returning no chunks")
            return []

        chunks = self.splitter.split_text(text)
        logger.info(f"[TokenChunker] Split into {len(chunks)} chunks")

        return [
            {
                "id": _make_chunk_id(metadata.get("file_path", "unknown"), i),
                "chunk_content": chunk,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "document_title": metadata.get("file_name", ""),
                "source_url": metadata.get("source_url", ""),
                "source_type": metadata.get("source_type", ""),
                "file_name": metadata.get("file_name", ""),
                "page_number": metadata.get("page_number"),
                "last_modified": metadata.get("last_modified"),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "pii_redacted": False,
            }
            for i, chunk in enumerate(chunks)
        ]


class MarkdownChunker:
    """Split markdown using pre-parsed sections from MarkdownParser (mistune AST).

    Expects metadata["sections"] from MarkdownParser — already split by headers
    with "Section: H1 > H2" prefixes. Falls back to plain token splitting if
    sections aren't available.
    """

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200):
        self.token_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk(self, text: str, metadata: dict) -> list[dict]:
        if not text.strip():
            logger.warning("[MarkdownChunker] Empty text, returning no chunks")
            return []

        # Use pre-parsed sections from MarkdownParser if available
        sections = metadata.get("sections", [])

        all_chunks = []
        if sections:
            # Sections already have "Section: H1 > H2\n\nbody" prefix from parser
            for section in sections:
                sub_chunks = self.token_splitter.split_text(section)
                all_chunks.extend(sub_chunks)
            logger.info(
                f"[MarkdownChunker] Split into {len(all_chunks)} chunks from {len(sections)} AST sections"
            )
        else:
            # Fallback: plain token splitting (no header context)
            all_chunks = self.token_splitter.split_text(text)
            logger.info(
                f"[MarkdownChunker] Fallback split into {len(all_chunks)} chunks (no AST sections)"
            )

        return [
            {
                "id": _make_chunk_id(metadata.get("file_path", "unknown"), i),
                "chunk_content": chunk,
                "chunk_index": i,
                "total_chunks": len(all_chunks),
                "document_title": metadata.get("file_name", ""),
                "source_url": metadata.get("source_url", ""),
                "source_type": metadata.get("source_type", ""),
                "file_name": metadata.get("file_name", ""),
                "page_number": None,
                "last_modified": metadata.get("last_modified"),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "pii_redacted": False,
            }
            for i, chunk in enumerate(all_chunks)
        ]


class SheetChunker:
    """Split Excel files by sheet, then by rows within a sheet.

    Each sheet becomes its own chunk group. Within a sheet, rows are batched
    into token-sized chunks so that large sheets don't produce a single
    oversized chunk. The sheet name is prepended as context for every chunk.
    """

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200):
        self.token_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk(self, text: str, metadata: dict) -> list[dict]:
        if not text.strip():
            logger.warning("[SheetChunker] Empty text, returning no chunks")
            return []

        # Split on "Sheet: <name>" boundaries produced by XlsxParser
        sheets = _split_on_sheet_headers(text)

        all_chunks = []
        for sheet_name, sheet_text in sheets:
            sub_chunks = self.token_splitter.split_text(sheet_text)
            for sub in sub_chunks:
                # Prepend sheet context so each chunk is self-contained
                all_chunks.append(f"[Sheet: {sheet_name}]\n{sub}")

        logger.info(
            f"[SheetChunker] Split into {len(all_chunks)} chunks from {len(sheets)} sheets"
        )

        return [
            {
                "id": _make_chunk_id(metadata.get("file_path", "unknown"), i),
                "chunk_content": chunk,
                "chunk_index": i,
                "total_chunks": len(all_chunks),
                "document_title": metadata.get("file_name", ""),
                "source_url": metadata.get("source_url", ""),
                "source_type": metadata.get("source_type", ""),
                "file_name": metadata.get("file_name", ""),
                "page_number": None,
                "last_modified": metadata.get("last_modified"),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "pii_redacted": False,
            }
            for i, chunk in enumerate(all_chunks)
        ]


class SemanticChunker:
    """Split documents using page boundaries from the parser.

    For PDFs, the parser returns per-page text. This chunker respects page
    boundaries: each page becomes its own chunk (or is split further if
    it exceeds the token limit). Small consecutive pages are merged up to
    the token limit to avoid tiny chunks.

    Falls back to recursive splitting if page info isn't available.
    """

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200):
        self.chunk_size = chunk_size
        self.enc = tiktoken.get_encoding("cl100k_base")
        self.token_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def _token_count(self, text: str) -> int:
        return len(self.enc.encode(text))

    def chunk(self, text: str, metadata: dict) -> list[dict]:
        if not text.strip():
            logger.warning("[SemanticChunker] Empty text, returning no chunks")
            return []

        pages = metadata.get("pages", [])

        all_chunks: list[str] = []
        if pages:
            # Merge small pages, split large pages
            buffer = ""
            for page in pages:
                page_text = page.get("text", "")
                table_text = page.get("table_text", "")
                combined = page_text
                if table_text:
                    combined += "\n" + table_text
                if not combined.strip():
                    continue

                page_num = page.get("page_number", "?")
                labeled = f"[Page {page_num}]\n{combined.strip()}"

                if buffer and self._token_count(buffer + "\n\n" + labeled) > self.chunk_size:
                    # Flush buffer as chunk(s)
                    if self._token_count(buffer) > self.chunk_size:
                        all_chunks.extend(self.token_splitter.split_text(buffer))
                    else:
                        all_chunks.append(buffer)
                    buffer = labeled
                else:
                    buffer = (buffer + "\n\n" + labeled).strip() if buffer else labeled

            # Flush remaining buffer
            if buffer.strip():
                if self._token_count(buffer) > self.chunk_size:
                    all_chunks.extend(self.token_splitter.split_text(buffer))
                else:
                    all_chunks.append(buffer)

            logger.info(
                f"[SemanticChunker] Split into {len(all_chunks)} chunks from {len(pages)} pages"
            )
        else:
            # Fallback: no page info
            all_chunks = self.token_splitter.split_text(text)
            logger.info(
                f"[SemanticChunker] Fallback split into {len(all_chunks)} chunks (no page info)"
            )

        return [
            {
                "id": _make_chunk_id(metadata.get("file_path", "unknown"), i),
                "chunk_content": chunk,
                "chunk_index": i,
                "total_chunks": len(all_chunks),
                "document_title": metadata.get("file_name", ""),
                "source_url": metadata.get("source_url", ""),
                "source_type": metadata.get("source_type", ""),
                "file_name": metadata.get("file_name", ""),
                "page_number": None,
                "last_modified": metadata.get("last_modified"),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "pii_redacted": False,
            }
            for i, chunk in enumerate(all_chunks)
        ]


def _split_on_sheet_headers(text: str) -> list[tuple[str, str]]:
    """Split XlsxParser output on 'Sheet: <name>' lines.

    Returns list of (sheet_name, sheet_body) tuples.
    If no sheet headers found, returns [("default", text)].
    """
    parts = re.split(r"(?m)^Sheet:\s*(.+)$", text)
    # re.split with a group produces: [before, name1, body1, name2, body2, ...]
    if len(parts) < 3:
        return [("default", text.strip())]

    sheets = []
    # parts[0] is text before first "Sheet:" header (usually empty)
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            sheets.append((name, body))

    return sheets if sheets else [("default", text.strip())]


class ChunkerFactory:
    """Route to the correct chunking strategy based on file extension and env config."""

    def __init__(self):
        self.chunk_size = int(os.environ.get("CHUNK_SIZE_TOKENS", "1024"))
        self.chunk_overlap = int(os.environ.get("CHUNK_OVERLAP_TOKENS", "200"))

        self.strategies = {
            ".md": os.environ.get("CHUNK_STRATEGY_MD", "header_based"),
            ".markdown": os.environ.get("CHUNK_STRATEGY_MD", "header_based"),
            ".xlsx": os.environ.get("CHUNK_STRATEGY_XLSX", "sheet_based"),
            ".xls": os.environ.get("CHUNK_STRATEGY_XLSX", "sheet_based"),
            ".xlsm": os.environ.get("CHUNK_STRATEGY_XLSX", "sheet_based"),
            ".pdf": os.environ.get("CHUNK_STRATEGY_PDF", "semantic"),
            "default": os.environ.get("CHUNK_STRATEGY_DEFAULT", "recursive"),
        }

        self._recursive = TokenChunker(self.chunk_size, self.chunk_overlap)
        self._header_based = MarkdownChunker(self.chunk_size, self.chunk_overlap)
        self._sheet_based = SheetChunker(self.chunk_size, self.chunk_overlap)
        self._semantic = SemanticChunker(self.chunk_size, self.chunk_overlap)

        logger.info(
            f"[ChunkerFactory] Initialized: size={self.chunk_size}, "
            f"overlap={self.chunk_overlap}, strategies={self.strategies}"
        )

    def get_chunker(
        self, file_extension: str
    ) -> TokenChunker | MarkdownChunker | SheetChunker | SemanticChunker:
        """Return the appropriate chunker for a file extension."""
        ext = file_extension.lower()
        strategy = self.strategies.get(ext, self.strategies["default"])

        if strategy == "header_based":
            return self._header_based
        elif strategy == "sheet_based":
            return self._sheet_based
        elif strategy == "semantic":
            return self._semantic
        else:
            return self._recursive

    def chunk(self, text: str, metadata: dict, file_extension: str) -> list[dict]:
        """Chunk text using the strategy configured for this file type."""
        chunker = self.get_chunker(file_extension)
        return chunker.chunk(text, metadata)


def _make_chunk_id(file_path: str, chunk_index: int) -> str:
    """Deterministic chunk ID for idempotent upserts."""
    raw = f"{file_path}_{chunk_index}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
