"""Token-based text chunking using tiktoken + langchain splitters.
No Azure AI services - all local computation."""

import base64
import logging
from datetime import datetime, timezone

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)


class TokenChunker:
    """Split text into token-sized chunks for standard documents (PDF, DOCX, etc.)."""

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200, encoding: str = "cl100k_base"):
        self.encoding_name = encoding
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
            logger.info(f"[MarkdownChunker] Split into {len(all_chunks)} chunks from {len(sections)} AST sections")
        else:
            # Fallback: plain token splitting (no header context)
            all_chunks = self.token_splitter.split_text(text)
            logger.info(f"[MarkdownChunker] Fallback split into {len(all_chunks)} chunks (no AST sections)")

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


def _make_chunk_id(file_path: str, chunk_index: int) -> str:
    """Deterministic chunk ID for idempotent upserts."""
    raw = f"{file_path}_{chunk_index}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
