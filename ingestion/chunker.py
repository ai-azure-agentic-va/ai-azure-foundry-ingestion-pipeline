"""Token-based text chunking using tiktoken + langchain splitters."""

import base64
import logging
import re
from datetime import datetime, timezone

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)

_SHEET_HEADER_RE = re.compile(r"(?m)^Sheet:\s*(.+)$")


class TokenChunker:
    """Split text into token-sized chunks for standard documents."""

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200, encoding: str = "cl100k_base"):
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
            _build_chunk_dict(metadata, i, chunk, len(chunks))
            for i, chunk in enumerate(chunks)
        ]


class MarkdownChunker:
    """Split markdown using pre-parsed sections from MarkdownParser (mistune AST).

    Falls back to plain token splitting if sections aren't available.
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

        sections = metadata.get("sections", [])

        all_chunks = []
        if sections:
            for section in sections:
                all_chunks.extend(self.token_splitter.split_text(section))
            logger.info(
                f"[MarkdownChunker] Split into {len(all_chunks)} chunks from {len(sections)} AST sections"
            )
        else:
            all_chunks = self.token_splitter.split_text(text)
            logger.info(f"[MarkdownChunker] Fallback split into {len(all_chunks)} chunks")

        return [
            _build_chunk_dict(metadata, i, chunk, len(all_chunks))
            for i, chunk in enumerate(all_chunks)
        ]


class SheetChunker:
    """Split Excel files by sheet, then by rows within a sheet."""

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

        sheets = _split_on_sheet_headers(text)

        all_chunks = []
        for sheet_name, sheet_text in sheets:
            sub_chunks = self.token_splitter.split_text(sheet_text)
            for sub in sub_chunks:
                all_chunks.append(f"[Sheet: {sheet_name}]\n{sub}")

        logger.info(f"[SheetChunker] Split into {len(all_chunks)} chunks from {len(sheets)} sheets")

        return [
            _build_chunk_dict(metadata, i, chunk, len(all_chunks))
            for i, chunk in enumerate(all_chunks)
        ]


class SemanticChunker:
    """Split documents using page boundaries from the parser.

    Merges small consecutive pages up to token limit, splits large pages further.
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
                    if self._token_count(buffer) > self.chunk_size:
                        all_chunks.extend(self.token_splitter.split_text(buffer))
                    else:
                        all_chunks.append(buffer)
                    buffer = labeled
                else:
                    buffer = (buffer + "\n\n" + labeled).strip() if buffer else labeled

            if buffer.strip():
                if self._token_count(buffer) > self.chunk_size:
                    all_chunks.extend(self.token_splitter.split_text(buffer))
                else:
                    all_chunks.append(buffer)

            logger.info(f"[SemanticChunker] Split into {len(all_chunks)} chunks from {len(pages)} pages")
        else:
            all_chunks = self.token_splitter.split_text(text)
            logger.info(f"[SemanticChunker] Fallback split into {len(all_chunks)} chunks")

        return [
            _build_chunk_dict(metadata, i, chunk, len(all_chunks))
            for i, chunk in enumerate(all_chunks)
        ]


def _split_on_sheet_headers(text: str) -> list[tuple[str, str]]:
    """Split XlsxParser output on 'Sheet: <name>' lines."""
    parts = _SHEET_HEADER_RE.split(text)
    if len(parts) < 3:
        return [("default", text.strip())]

    sheets = []
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if body:
            sheets.append((name, body))

    return sheets if sheets else [("default", text.strip())]


class ChunkerFactory:
    """Route to the correct chunking strategy based on file extension."""

    def __init__(self):
        from .config import settings

        self.chunk_size = settings.CHUNK_SIZE_TOKENS
        self.chunk_overlap = settings.CHUNK_OVERLAP_TOKENS

        self.strategies = {
            ".md": settings.CHUNK_STRATEGY_MD,
            ".markdown": settings.CHUNK_STRATEGY_MD,
            ".xlsx": settings.CHUNK_STRATEGY_XLSX,
            ".xls": settings.CHUNK_STRATEGY_XLSX,
            ".xlsm": settings.CHUNK_STRATEGY_XLSX,
            ".pdf": settings.CHUNK_STRATEGY_PDF,
            "default": settings.CHUNK_STRATEGY_DEFAULT,
        }

        self._recursive = TokenChunker(self.chunk_size, self.chunk_overlap)
        self._header_based = MarkdownChunker(self.chunk_size, self.chunk_overlap)
        self._sheet_based = SheetChunker(self.chunk_size, self.chunk_overlap)
        self._semantic = SemanticChunker(self.chunk_size, self.chunk_overlap)

        logger.info(
            f"[ChunkerFactory] Initialized: size={self.chunk_size}, "
            f"overlap={self.chunk_overlap}, strategies={self.strategies}"
        )

    def get_chunker(self, file_extension: str):
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
        chunker = self.get_chunker(file_extension)
        return chunker.chunk(text, metadata)


def _make_chunk_id(file_path: str, chunk_index: int) -> str:
    """Deterministic chunk ID for idempotent upserts."""
    raw = f"{file_path}_{chunk_index}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _make_breadcrumb(file_path: str) -> str:
    """Build a human-readable breadcrumb from a blob path.

    Example: 'wiki/Engineering/Platform/setup-guide.pdf'
          → 'Engineering > Platform > setup-guide.pdf'
    Strips known container prefixes (raw-documents, wiki, sharepoint).
    """
    if not file_path:
        return ""
    parts = file_path.replace("\\", "/").strip("/").split("/")
    # Strip common container/prefix segments
    skip_prefixes = {"raw-documents", "wiki", "sharepoint", "documents"}
    while parts and parts[0].lower() in skip_prefixes:
        parts.pop(0)
    return " > ".join(parts) if parts else file_path


_PAGE_NUM_RE = re.compile(r"^\[Page\s+(\d+)\]")


def _extract_page_number(chunk_content: str, metadata_page: int | None) -> int | None:
    """Extract page number from chunk content prefix, or use metadata fallback."""
    if metadata_page:
        return metadata_page
    m = _PAGE_NUM_RE.search(chunk_content)
    return int(m.group(1)) if m else None


def _build_chunk_dict(metadata: dict, index: int, chunk_content: str, total: int) -> dict:
    return {
        "id": _make_chunk_id(metadata.get("file_path", "unknown"), index),
        "chunk_content": chunk_content,
        "chunk_index": index,
        "total_chunks": total,
        "document_title": metadata.get("file_name", ""),
        "source_url": metadata.get("source_url", ""),
        "source_type": metadata.get("source_type", ""),
        "file_name": metadata.get("file_name", ""),
        "page_number": _extract_page_number(chunk_content, metadata.get("page_number")),
        "last_modified": metadata.get("last_modified"),
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pii_redacted": False,
        "breadcrumb": _make_breadcrumb(metadata.get("file_path", "")),
    }
