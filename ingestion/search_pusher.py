"""Push chunks to Azure AI Search index (merge_or_upload for idempotent upserts)."""

import logging
import random
import time

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    ScalarQuantizationCompression,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

logger = logging.getLogger(__name__)

from .config import settings as _cfg

VECTOR_DIMENSIONS = _cfg.FOUNDRY_EMBEDDING_DIMENSIONS
FOUNDRY_VECTORIZER_ENDPOINT = _cfg.FOUNDRY_ENDPOINT or ""
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = _cfg.FOUNDRY_EMBEDDING_DEPLOYMENT
AZURE_OPENAI_EMBEDDING_MODEL = _cfg.FOUNDRY_EMBEDDING_MODEL

_PUSH_MAX_RETRIES = 5
_PUSH_BACKOFF_CEILING = 30.0


def _build_index_schema(index_name: str) -> SearchIndex:
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="chunk_content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            hidden=False,
            vector_search_dimensions=VECTOR_DIMENSIONS,
            vector_search_profile_name="foundry-vector-profile",
        ),
        SearchableField(
            name="document_title", type=SearchFieldDataType.String,
            filterable=True, sortable=True, analyzer_name="en.microsoft",
        ),
        SimpleField(name="source_url", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="source_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="file_name", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="breadcrumb", type=SearchFieldDataType.String, filterable=True, analyzer_name="en.microsoft"),
        SimpleField(name="chunk_index", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="total_chunks", type=SearchFieldDataType.Int32, filterable=True),
        SimpleField(name="page_number", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
        SimpleField(name="last_modified", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SimpleField(name="ingested_at", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SimpleField(name="pii_redacted", type=SearchFieldDataType.Boolean, filterable=True, facetable=True),
    ]

    vectorizer = None
    if FOUNDRY_VECTORIZER_ENDPOINT:
        vectorizer = AzureOpenAIVectorizer(
            vectorizer_name="foundry-openai-vectorizer",
            parameters=AzureOpenAIVectorizerParameters(
                resource_url=FOUNDRY_VECTORIZER_ENDPOINT,
                deployment_name=AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
                model_name=AZURE_OPENAI_EMBEDDING_MODEL,
            ),
        )

    sq_compression = ScalarQuantizationCompression(compression_name="sq-compression")

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="default-hnsw",
                parameters={"m": 8, "efConstruction": 400, "efSearch": 500, "metric": "cosine"},
            )
        ],
        compressions=[sq_compression],
        profiles=[
            VectorSearchProfile(
                name="foundry-vector-profile",
                algorithm_configuration_name="default-hnsw",
                vectorizer_name="foundry-openai-vectorizer" if vectorizer else None,
                compression_name="sq-compression",
            )
        ],
        vectorizers=[vectorizer] if vectorizer else [],
    )

    semantic_config = SemanticConfiguration(
        name=_cfg.SEARCH_SEMANTIC_CONFIG_NAME,
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="document_title"),
            content_fields=[SemanticField(field_name="chunk_content")],
            keyword_fields=[
                SemanticField(field_name="source_type"),
                SemanticField(field_name="file_name"),
                SemanticField(field_name="breadcrumb"),
            ],
        ),
    )

    return SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=SemanticSearch(configurations=[semantic_config]),
    )


class SearchPusher:

    def __init__(self, endpoint: str | None = None, index_name: str | None = None):
        self.endpoint = endpoint or _cfg.SEARCH_ENDPOINT
        self.index_name = index_name or _cfg.SEARCH_INDEX_NAME

        if not self.endpoint:
            raise ValueError("SEARCH_ENDPOINT is required")

        credential = DefaultAzureCredential()
        self._index_client = SearchIndexClient(endpoint=self.endpoint, credential=credential)
        self.client = SearchClient(
            endpoint=self.endpoint, index_name=self.index_name, credential=credential
        )

        self.ensure_index_exists()
        logger.info(f"[SearchPusher] Initialized: endpoint={self.endpoint}, index={self.index_name}")

    def ensure_index_exists(self):
        try:
            index_schema = _build_index_schema(self.index_name)
            self._index_client.create_or_update_index(index_schema)
            logger.info(f"[SearchPusher] Index '{self.index_name}' ensured (create_or_update)")
        except Exception as e:
            logger.error(f"[SearchPusher] Failed to ensure index '{self.index_name}': {e}")
            raise

    def delete_document_chunks(self, file_path: str) -> int:
        """Delete all existing chunks for a document before re-indexing."""
        import os
        file_name = os.path.basename(file_path)
        if not file_name:
            return 0

        try:
            escaped = file_name.replace("'", "''")
            results = self.client.search(
                search_text="*",
                filter=f"file_name eq '{escaped}'",
                select=["id", "breadcrumb"],
                top=1000,
            )

            from .chunker import _make_breadcrumb
            expected_crumb = _make_breadcrumb(file_path)
            doc_ids = [
                r["id"] for r in results
                if not expected_crumb or r.get("breadcrumb", "") == expected_crumb
            ]
            if not doc_ids:
                return 0

            self.client.delete_documents(documents=[{"id": did} for did in doc_ids])
            logger.info(f"[SearchPusher] Deleted {len(doc_ids)} orphan chunks for '{file_path}'")
            return len(doc_ids)
        except Exception as e:
            logger.warning(f"[SearchPusher] Orphan cleanup failed for '{file_path}': {e}")
            return 0

    def push(self, chunks: list[dict], batch_size: int | None = None) -> dict:
        if batch_size is None:
            batch_size = _cfg.SEARCH_PUSH_BATCH_SIZE

        # Skip chunks that failed embedding (content_vector=None)
        pushable = [c for c in chunks if c.get("content_vector") is not None]
        skipped = len(chunks) - len(pushable)
        if skipped > 0:
            logger.warning(
                f"[SearchPusher] Skipping {skipped}/{len(chunks)} chunks with missing embeddings"
            )

        total_success = 0
        total_failed = 0
        errors = []

        logger.info(f"[SearchPusher] Pushing {len(pushable)} chunks to index '{self.index_name}'")

        for i in range(0, len(pushable), batch_size):
            batch = pushable[i : i + batch_size]
            cleaned_batch = [{k: v for k, v in chunk.items() if v is not None} for chunk in batch]

            success, failed, batch_errors = self._push_batch_with_retry(cleaned_batch)
            total_success += success
            total_failed += failed
            errors.extend(batch_errors)

            if i + batch_size < len(pushable):
                time.sleep(random.uniform(0.1, 0.5))

        logger.info(
            f"[SearchPusher] Push complete: {total_success} succeeded, "
            f"{total_failed} failed, {skipped} skipped (no vector)"
        )
        return {"success": total_success, "failed": total_failed, "skipped": skipped, "errors": errors}

    def _push_batch_with_retry(self, batch: list[dict]) -> tuple[int, int, list[str]]:
        for attempt in range(_PUSH_MAX_RETRIES):
            try:
                result = self.client.merge_or_upload_documents(documents=batch)
                success = 0
                failed = 0
                errors = []
                for r in result:
                    if r.succeeded:
                        success += 1
                    else:
                        failed += 1
                        err_msg = f"Failed to index {r.key}: {r.error_message}"
                        errors.append(err_msg)
                        logger.error(f"[SearchPusher] {err_msg}")
                return success, failed, errors

            except Exception as e:
                status = getattr(e, "status_code", None)
                if status and status in (400, 401, 403, 404, 409):
                    err_msg = f"Batch push failed with non-retryable status {status}: {e}"
                    logger.error(f"[SearchPusher] {err_msg}")
                    return 0, len(batch), [err_msg]
                if attempt < _PUSH_MAX_RETRIES - 1:
                    wait = min(_PUSH_BACKOFF_CEILING, 2.0 ** attempt) + random.uniform(0, 2.0)
                    logger.warning(
                        f"[SearchPusher] Batch push failed (attempt {attempt + 1}/{_PUSH_MAX_RETRIES}), "
                        f"retrying in {wait:.1f}s: {e}"
                    )
                    time.sleep(wait)
                else:
                    err_msg = f"Batch push failed after {_PUSH_MAX_RETRIES} retries: {e}"
                    logger.error(f"[SearchPusher] {err_msg}")
                    return 0, len(batch), [err_msg]

        return 0, len(batch), ["Retry loop completed without return"]
