"""Push chunks to Azure AI Search index (merge_or_upload for idempotent upserts)."""

import logging

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
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


def _build_index_schema(index_name: str) -> SearchIndex:
    """Build the AI Search index schema matching the ingestion pipeline output."""
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
        SimpleField(name="last_modified", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="ingested_at", type=SearchFieldDataType.String, filterable=True, sortable=True),
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

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name="default-hnsw",
                parameters={"m": 4, "efConstruction": 400, "efSearch": 500, "metric": "cosine"},
            )
        ],
        profiles=[
            VectorSearchProfile(
                name="foundry-vector-profile",
                algorithm_configuration_name="default-hnsw",
                vectorizer_name="foundry-openai-vectorizer" if vectorizer else None,
            )
        ],
        vectorizers=[vectorizer] if vectorizer else [],
    )

    semantic_config = SemanticConfiguration(
        name="custom-kb-semantic-config",
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
    semantic_search = SemanticSearch(configurations=[semantic_config])

    return SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )


class SearchPusher:
    """Push document chunks to Azure AI Search. Creates index if it doesn't exist."""

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
        """Create or update the search index (idempotent)."""
        try:
            index_schema = _build_index_schema(self.index_name)
            self._index_client.create_or_update_index(index_schema)
            logger.info(f"[SearchPusher] Index '{self.index_name}' ensured (create_or_update)")
        except Exception as e:
            logger.error(f"[SearchPusher] Failed to ensure index '{self.index_name}': {e}")
            raise

    def push(self, chunks: list[dict], batch_size: int = 1000) -> dict:
        """Push chunks to AI Search using merge_or_upload for idempotent upserts."""
        total_success = 0
        total_failed = 0
        errors = []

        logger.info(f"[SearchPusher] Pushing {len(chunks)} chunks to index '{self.index_name}'")

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            cleaned_batch = [{k: v for k, v in chunk.items() if v is not None} for chunk in batch]

            try:
                result = self.client.merge_or_upload_documents(documents=cleaned_batch)
                for r in result:
                    if r.succeeded:
                        total_success += 1
                    else:
                        total_failed += 1
                        err_msg = f"Failed to index {r.key}: {r.error_message}"
                        errors.append(err_msg)
                        logger.error(f"[SearchPusher] {err_msg}")
            except Exception as e:
                total_failed += len(batch)
                err_msg = f"Batch push failed: {e}"
                errors.append(err_msg)
                logger.error(f"[SearchPusher] {err_msg}")

        logger.info(f"[SearchPusher] Push complete: {total_success} succeeded, {total_failed} failed")
        return {"success": total_success, "failed": total_failed, "errors": errors}
