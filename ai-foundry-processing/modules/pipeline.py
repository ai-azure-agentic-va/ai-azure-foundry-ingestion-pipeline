"""AI Foundry Services document processing pipeline.
Uses Azure AI Content Understanding for parsing (all file types).
Content Understanding handles text, tables, figures, and image verbalization
in a single API call — replacing Document Intelligence + GPT-4o Vision.
Uses Azure AI Language for PII detection and redaction.
Uses tiktoken + langchain for chunking (same as custom path).
Uses Foundry LLM for embeddings.

This is the pipeline for ai-foundry-processing."""

import logging
import os

from .adls_reader import AdlsReader
from .foundry_parser import FoundryParser
from .foundry_pii_scanner import FoundryPiiScanner
from .chunker import TokenChunker, MarkdownChunker
from .embedder import FoundryEmbedder
from .search_pusher import SearchPusher

logger = logging.getLogger(__name__)


class FoundryDocPipeline:
    """Process documents using Azure AI Foundry services.

    Pipeline: Read -> Parse (Content Understanding) -> Chunk (tiktoken)
              -> PII (Azure Language) -> Embed (Foundry LLM) -> Push (AI Search)

    Key advantage: Content Understanding returns structured markdown with tables,
    figures, and image descriptions in a single API call — optimized for RAG.
    """

    PIPELINE_NAME = "AI_FOUNDRY_SERVICES"

    def __init__(self):
        self.adls = AdlsReader()
        self.parser = FoundryParser()
        self.token_chunker = TokenChunker(
            chunk_size=int(os.environ.get("CHUNK_SIZE_TOKENS", "1024")),
            chunk_overlap=int(os.environ.get("CHUNK_OVERLAP_TOKENS", "200")),
        )
        self.md_chunker = MarkdownChunker(
            chunk_size=int(os.environ.get("CHUNK_SIZE_TOKENS", "1024")),
            chunk_overlap=int(os.environ.get("CHUNK_OVERLAP_TOKENS", "200")),
        )
        self.pii_scanner = FoundryPiiScanner(
            confidence_threshold=float(os.environ.get("PII_CONFIDENCE_THRESHOLD", "0.8")),
            enabled=os.environ.get("PII_ENABLED", "true").lower() == "true",
        )
        self.embedder = FoundryEmbedder()
        self.pusher = SearchPusher()

        logger.info("[FoundryDocPipeline] Initialized "
                    "(Content Understanding + Azure Language PII)")

    def process_document(self, container: str, blob_path: str, metadata: dict | None = None) -> dict:
        """Process a single document through the AI Foundry pipeline."""
        file_name = os.path.basename(blob_path)
        logger.info(f"[FoundryDocPipeline] START: {container}/{blob_path}")

        # 1. Read document from ADLS
        try:
            file_bytes = self.adls.read_blob(container, blob_path)
            logger.info(f"[FoundryDocPipeline] [1/6] Read {len(file_bytes)} bytes from ADLS")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Failed to read blob: {e}")
            return {"status": "error", "stage": "read", "error": str(e)}

        # Always read metadata sidecar — sidecar values (e.g. SharePoint source_url) override trigger defaults
        if metadata is None:
            metadata = {}
        sidecar = self.adls.read_metadata_sidecar(container, blob_path)
        for key, value in sidecar.items():
            if value:  # Sidecar values take precedence over trigger-provided defaults
                metadata[key] = value

        metadata.setdefault("file_name", file_name)
        metadata.setdefault("file_path", blob_path)
        metadata.setdefault("source_type", _infer_source_type(blob_path))

        # 2. Parse document (Content Understanding — text, tables, figures, markdown)
        try:
            parse_result = self.parser.parse(file_bytes, file_name)
            if not parse_result.full_text.strip():
                logger.warning(f"[FoundryDocPipeline] No text extracted from {file_name}")
                return {"status": "skipped", "reason": "no_text_extracted"}
            logger.info(f"[FoundryDocPipeline] [2/6] Parsed: "
                        f"{len(parse_result.full_text)} chars, {parse_result.page_count} pages")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Parse failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Parse error: {e}")
            return {"status": "error", "stage": "parse", "error": str(e)}

        # 3. Chunk text (tiktoken + langchain — same as custom path)
        try:
            is_markdown = file_name.lower().endswith((".md", ".markdown"))
            chunker = self.md_chunker if is_markdown else self.token_chunker
            # For markdown, pass parser-extracted sections so chunker uses AST sections
            chunk_metadata = {**metadata, **parse_result.metadata} if is_markdown else metadata
            chunks = chunker.chunk(parse_result.full_text, chunk_metadata)
            if not chunks:
                logger.warning(f"[FoundryDocPipeline] No chunks produced for {file_name}")
                return {"status": "skipped", "reason": "no_chunks_produced"}
            logger.info(f"[FoundryDocPipeline] [3/6] Chunked: {len(chunks)} chunks")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Chunking failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Chunk error: {e}")
            return {"status": "error", "stage": "chunk", "error": str(e)}

        # 4. PII scan and redact (Azure AI Language via Foundry)
        pii_count = 0
        try:
            for chunk in chunks:
                redacted_text, pii_found, entities = self.pii_scanner.scan_and_redact(chunk["chunk_content"])
                chunk["chunk_content"] = redacted_text
                chunk["pii_redacted"] = pii_found
                if pii_found:
                    pii_count += 1
            logger.info(f"[FoundryDocPipeline] [4/6] PII scan: {pii_count}/{len(chunks)} chunks had PII redacted")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] PII scan failed for {file_name}: {e}")
            logger.warning("[FoundryDocPipeline] Proceeding without PII redaction")

        # 5. Generate embeddings (Foundry LLM)
        try:
            chunks = self.embedder.embed_chunks(chunks)
            logger.info(f"[FoundryDocPipeline] [5/6] Embedded: {len(chunks)} chunks via Foundry LLM")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Embedding failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Embedding error: {e}")
            return {"status": "error", "stage": "embed", "error": str(e)}

        # 6. Push to Azure AI Search
        try:
            result = self.pusher.push(chunks)
            logger.info(f"[FoundryDocPipeline] [6/6] Pushed to AI Search: "
                        f"{result['success']} succeeded, {result['failed']} failed")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Push to search failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Search push error: {e}")
            return {"status": "error", "stage": "push", "error": str(e)}

        logger.info(f"[FoundryDocPipeline] COMPLETE: {file_name} -> {result['success']} chunks indexed")
        return {
            "status": "success",
            "processing_path": self.PIPELINE_NAME,
            "file_name": file_name,
            "blob_path": blob_path,
            "chars_extracted": len(parse_result.full_text),
            "chunks_created": len(chunks),
            "pii_chunks_redacted": pii_count,
            "chunks_indexed": result["success"],
            "chunks_failed": result["failed"],
        }


def _infer_source_type(blob_path: str) -> str:
    """Infer source type from blob path prefix."""
    if blob_path.startswith("sharepoint/") or "/sharepoint/" in blob_path:
        return "sharepoint"
    elif blob_path.startswith("wiki/") or "/wiki/" in blob_path:
        return "wiki"
    return "unknown"
