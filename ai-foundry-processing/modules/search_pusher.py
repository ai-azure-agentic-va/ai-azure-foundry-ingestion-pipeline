"""Push chunks to Azure AI Search index using the SDK (merge_or_upload for idempotent upserts).
No indexers, no skillsets, no data sources - push-only model."""

import logging
import os

from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential

logger = logging.getLogger(__name__)


class SearchPusher:
    """Push document chunks to Azure AI Search custom-kb-index."""

    def __init__(
        self,
        endpoint: str | None = None,
        index_name: str | None = None,
        admin_key: str | None = None,
    ):
        self.endpoint = endpoint or os.environ.get("SEARCH_ENDPOINT")
        self.index_name = index_name or os.environ.get("SEARCH_INDEX_NAME", "custom-kb-index")
        self.admin_key = admin_key or os.environ.get("SEARCH_ADMIN_KEY")

        if not self.endpoint or not self.admin_key:
            raise ValueError("SEARCH_ENDPOINT and SEARCH_ADMIN_KEY are required")

        self.client = SearchClient(
            endpoint=self.endpoint,
            index_name=self.index_name,
            credential=AzureKeyCredential(self.admin_key),
        )
        logger.info(f"[SearchPusher] Initialized: endpoint={self.endpoint}, index={self.index_name}")

    def push(self, chunks: list[dict], batch_size: int = 100) -> dict:
        """Push chunks to AI Search using merge_or_upload for idempotent upserts.

        Returns: {"success": int, "failed": int, "errors": list}
        """
        total_success = 0
        total_failed = 0
        errors = []

        logger.info(f"[SearchPusher] Pushing {len(chunks)} chunks to index '{self.index_name}'")

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]

            # Clean up None values that AI Search doesn't accept for certain fields
            cleaned_batch = []
            for chunk in batch:
                doc = {k: v for k, v in chunk.items() if v is not None}
                cleaned_batch.append(doc)

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
