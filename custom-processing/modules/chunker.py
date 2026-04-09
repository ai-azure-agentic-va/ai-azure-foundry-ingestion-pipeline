"""Token-based text chunking using tiktoken + langchain splitters.
No Azure AI services - all local computation."""

import base64
import logging
from datetime import datetime, timezone

from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    MarkdownHeaderTextSplitter,
)

logger = logging.getLogger(__name__)


class TokenChunker:
    """Split text into token-sized chunks for standard documents (PDF, DOCX, etc.)."""

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200, encoding: str = "cl100k_base"):
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
    """Split markdown by headers first, then by token size. For .md files."""

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200):
        self.header_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[
                ("#", "heading_1"),
                ("##", "heading_2"),
                ("###", "heading_3"),
            ]
        )
        self.token_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk(self, text: str, metadata: dict) -> list[dict]:
        if not text.strip():
            logger.warning("[MarkdownChunker] Empty text, returning no chunks")
            return []

        # Split by markdown headers to preserve structure
        header_splits = self.header_splitter.split_text(text)

        # Then split large sections by token size
        all_chunks = []
        for section in header_splits:
            sub_chunks = self.token_splitter.split_text(section.page_content)
            all_chunks.extend(sub_chunks)

        logger.info(f"[MarkdownChunker] Split into {len(all_chunks)} chunks from {len(header_splits)} sections")

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
