"""Foundry LLM embedding generator - the ONLY external AI service call in the pipeline.
Uses text-embedding-3-small via Azure OpenAI (Foundry) endpoint."""

import logging
import os
import random
import time

from openai import AzureOpenAI, RateLimitError

logger = logging.getLogger(__name__)


class FoundryEmbedder:
    """Generate embeddings via Foundry LLM (text-embedding-3-small).
    This is the ONLY external AI call in the entire ingestion pipeline."""

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        deployment: str | None = None,
        api_version: str | None = None,
    ):
        self.endpoint = endpoint or os.environ.get("FOUNDRY_ENDPOINT")
        self.api_key = api_key or os.environ.get("FOUNDRY_API_KEY")
        self.deployment = deployment or os.environ.get(
            "FOUNDRY_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
        )
        self.api_version = api_version or os.environ.get(
            "FOUNDRY_API_VERSION", "2024-06-01"
        )

        if not self.endpoint:
            raise ValueError("FOUNDRY_ENDPOINT is required")

        # Support both API key and Managed Identity
        if self.api_key:
            self.client = AzureOpenAI(
                azure_endpoint=self.endpoint,
                api_key=self.api_key,
                api_version=self.api_version,
            )
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            credential = DefaultAzureCredential()
            token_provider = get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            )
            self.client = AzureOpenAI(
                azure_endpoint=self.endpoint,
                azure_ad_token_provider=token_provider,
                api_version=self.api_version,
            )

        logger.info(
            f"[FoundryEmbedder] Initialized: endpoint={self.endpoint}, deployment={self.deployment}"
        )

    def embed_chunks(self, chunks: list[dict], batch_size: int = 16) -> list[dict]:
        """Generate embeddings for chunks. Batches up to 16 texts per API call.

        Adds 'content_vector' field to each chunk dict.
        """
        total = len(chunks)
        logger.info(
            f"[FoundryEmbedder] Embedding {total} chunks in batches of {batch_size}"
        )

        for i in range(0, total, batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c["chunk_content"] for c in batch]

            response = self._embed_with_backoff(texts)

            for j, embedding_data in enumerate(response.data):
                batch[j]["content_vector"] = embedding_data.embedding

            logger.debug(
                f"[FoundryEmbedder] Batch {i // batch_size + 1}: embedded {len(batch)} chunks"
            )

        logger.info(f"[FoundryEmbedder] All {total} chunks embedded successfully")
        return chunks

    def _embed_with_backoff(self, texts: list[str], max_retries: int = 5):
        """Call embedding API with exponential backoff on rate limits."""
        for attempt in range(max_retries):
            try:
                return self.client.embeddings.create(
                    input=texts,
                    model=self.deployment,
                )
            except RateLimitError:
                wait = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"[FoundryEmbedder] Rate limited. Retrying in {wait:.1f}s (attempt {attempt + 1})"
                )
                time.sleep(wait)

        raise RuntimeError(
            f"Embedding failed after {max_retries} retries due to rate limiting"
        )
