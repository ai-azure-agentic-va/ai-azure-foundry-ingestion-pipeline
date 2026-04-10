"""AI Foundry Services document processing pipeline."""

import logging
import os

from .chunker import ChunkerFactory
from .embedder import FoundryEmbedder
from .pii_scanner import FoundryPiiScanner
from .reader import AdlsReader
from .search_pusher import SearchPusher
from parsing.content_understanding import FoundryParser

logger = logging.getLogger(__name__)


class FoundryDocPipeline:
    """Process documents: Read → Parse → Chunk → PII scan → Embed → Push to AI Search."""

    PIPELINE_NAME = "AI_FOUNDRY_SERVICES"

    def __init__(self):
        self.adls = AdlsReader()
        self.parser = FoundryParser()
        self.chunker_factory = ChunkerFactory()
        from .config import settings

        self.pii_scanner = FoundryPiiScanner(
            confidence_threshold=settings.PII_CONFIDENCE_THRESHOLD,
            enabled=settings.PII_ENABLED,
        )
        self.embedder = FoundryEmbedder()
        self.pusher = SearchPusher()

        logger.info("[FoundryDocPipeline] Initialized (Content Understanding + Azure Language PII)")

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

        # Merge metadata: blob metadata > sidecar > trigger-provided defaults
        if metadata is None:
            metadata = {}
        blob_meta = self.adls.read_blob_metadata(container, blob_path)
        for key, value in blob_meta.items():
            if value:
                metadata[key] = value
        sidecar = self.adls.read_metadata_sidecar(container, blob_path)
        for key, value in sidecar.items():
            if value:
                metadata[key] = value

        metadata.setdefault("file_name", file_name)
        metadata.setdefault("file_path", blob_path)
        metadata.setdefault("source_type", _infer_source_type(blob_path))

        # 2. Parse document
        try:
            parse_result = self.parser.parse(file_bytes, file_name)
            if not parse_result.full_text.strip():
                logger.warning(f"[FoundryDocPipeline] No text extracted from {file_name}")
                return {"status": "skipped", "reason": "no_text_extracted"}
            logger.info(
                f"[FoundryDocPipeline] [2/6] Parsed: "
                f"{len(parse_result.full_text)} chars, {parse_result.page_count} pages"
            )
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Parse failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Parse error: {e}")
            return {"status": "error", "stage": "parse", "error": str(e)}

        # 3. Chunk text
        try:
            ext = os.path.splitext(file_name)[1].lower()
            chunk_metadata = {**metadata, **parse_result.metadata, "pages": parse_result.pages}
            chunks = self.chunker_factory.chunk(parse_result.full_text, chunk_metadata, ext)
            if not chunks:
                logger.warning(f"[FoundryDocPipeline] No chunks produced for {file_name}")
                return {"status": "skipped", "reason": "no_chunks_produced"}
            logger.info(f"[FoundryDocPipeline] [3/6] Chunked: {len(chunks)} chunks")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Chunking failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Chunk error: {e}")
            return {"status": "error", "stage": "chunk", "error": str(e)}

        # 4. PII scan and redact
        pii_count = 0
        try:
            texts = [chunk["chunk_content"] for chunk in chunks]
            pii_results = self.pii_scanner.scan_and_redact_batch(texts)
            for chunk, (redacted_text, pii_found, entities) in zip(chunks, pii_results):
                chunk["chunk_content"] = redacted_text
                chunk["pii_redacted"] = pii_found
                if pii_found:
                    pii_count += 1
            logger.info(f"[FoundryDocPipeline] [4/6] PII scan: {pii_count}/{len(chunks)} chunks had PII redacted")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] PII scan failed for {file_name}: {e}")
            logger.warning("[FoundryDocPipeline] Proceeding without PII redaction")

        # 5. Generate embeddings
        try:
            chunks = self.embedder.embed_chunks(chunks)
            logger.info(f"[FoundryDocPipeline] [5/6] Embedded: {len(chunks)} chunks")
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Embedding failed for {file_name}: {e}")
            self.adls.move_to_failed(blob_path, f"Embedding error: {e}")
            return {"status": "error", "stage": "embed", "error": str(e)}

        # 6. Push to Azure AI Search
        try:
            result = self.pusher.push(chunks)
            logger.info(
                f"[FoundryDocPipeline] [6/6] Pushed: {result['success']} succeeded, {result['failed']} failed"
            )
        except Exception as e:
            logger.error(f"[FoundryDocPipeline] Push failed for {file_name}: {e}")
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
    path_lower = blob_path.lower()
    if "sharepoint" in path_lower:
        return "sharepoint"
    if "wiki" in path_lower or "nfcu-va-wiki" in path_lower:
        return "wiki"
    return "unknown"
