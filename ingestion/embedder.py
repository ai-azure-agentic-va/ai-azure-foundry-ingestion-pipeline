"""Embedding generator using text-embedding-3-large via Azure OpenAI.

Token-aware batching, adaptive rate limiting from response headers,
TPM-aware pacing, exponential backoff with full jitter, and per-batch
resilience (failed batches don't kill the entire document).
"""

import logging
import random
import time
from typing import Any

import tiktoken
from openai import APIConnectionError, APITimeoutError, AzureOpenAI, RateLimitError

logger = logging.getLogger(__name__)

_ENCODING: tiktoken.Encoding | None = None
_API_MAX_TOKENS_PER_INPUT = 8191


def _get_encoding() -> tiktoken.Encoding:
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def _estimate_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


def _full_jitter_backoff(attempt: int, ceiling: float) -> float:
    """sleep = random(0, min(ceiling, 2^attempt)) — AWS best-practice for distributed retries."""
    return random.uniform(0, min(ceiling, 2.0 ** attempt))


class FoundryEmbedder:

    def __init__(
        self,
        endpoint: str | None = None,
        deployment: str | None = None,
        dimensions: int | None = None,
        api_version: str | None = None,
    ):
        from .config import settings

        self.endpoint = endpoint if endpoint is not None else settings.FOUNDRY_ENDPOINT
        self.deployment = deployment if deployment is not None else settings.FOUNDRY_EMBEDDING_DEPLOYMENT
        self.dimensions = dimensions if dimensions is not None else settings.FOUNDRY_EMBEDDING_DIMENSIONS
        self.api_version = api_version if api_version is not None else settings.FOUNDRY_API_VERSION

        self.max_retries: int = settings.EMBEDDING_MAX_RETRIES
        self.backoff_ceiling: float = settings.EMBEDDING_BACKOFF_CEILING
        self.tpm_reserve_fraction: float = settings.EMBEDDING_TPM_RESERVE_FRACTION
        self.max_batch_tokens: int = settings.EMBEDDING_MAX_BATCH_TOKENS
        self.estimated_instances: int = settings.EMBEDDING_ESTIMATED_INSTANCES

        if not self.endpoint:
            raise ValueError("FOUNDRY_ENDPOINT is required")

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

        self._remaining_tokens: int | None = None
        self._remaining_requests: int | None = None
        self._tpm_limit: int | None = None
        self._consecutive_429s: int = 0

        logger.info(
            f"[FoundryEmbedder] Initialized: endpoint={self.endpoint}, "
            f"deployment={self.deployment}, max_retries={self.max_retries}, "
            f"estimated_instances={self.estimated_instances}"
        )

    def embed_chunks(self, chunks: list[dict], batch_size: int | None = None) -> list[dict]:
        if batch_size is None:
            from .config import settings as _settings
            batch_size = _settings.EMBEDDING_BATCH_SIZE

        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        total = len(chunks)
        if total == 0:
            return chunks

        for i, chunk in enumerate(chunks):
            content = chunk.get("chunk_content")
            if not content or not isinstance(content, str):
                logger.warning(
                    f"[FoundryEmbedder] Chunk {i} has invalid chunk_content "
                    f"(type={type(content).__name__}). Setting to empty placeholder."
                )
                chunk["chunk_content"] = "[empty]"

        batches = self._build_token_aware_batches(chunks, batch_size)
        logger.info(
            f"[FoundryEmbedder] Embedding {total} chunks in {len(batches)} "
            f"token-aware batches (max {batch_size} per batch)"
        )

        failed_chunks = 0
        for batch_num, (batch, batch_tokens) in enumerate(batches, 1):
            texts = [c["chunk_content"] for c in batch]
            self._adaptive_throttle(batch_tokens)

            try:
                embeddings = self._embed_with_retry(texts)
                for j, vec in enumerate(embeddings):
                    batch[j]["content_vector"] = vec
                self._consecutive_429s = 0
                self._pace_between_batches(batch_tokens)

            except RuntimeError:
                logger.error(
                    f"[FoundryEmbedder] Batch {batch_num}/{len(batches)} failed permanently. "
                    f"Marking {len(batch)} chunks as unembedded."
                )
                failed_chunks += len(batch)
                for chunk in batch:
                    chunk["content_vector"] = None

            if batch_num % 50 == 0 or batch_num == len(batches):
                logger.info(
                    f"[FoundryEmbedder] Progress: {batch_num}/{len(batches)} batches "
                    f"({failed_chunks} chunks failed so far)"
                )

        if failed_chunks:
            logger.warning(
                f"[FoundryEmbedder] {failed_chunks}/{total} chunks failed embedding. "
                f"Successfully embedded {total - failed_chunks} chunks."
            )
        else:
            logger.info(f"[FoundryEmbedder] All {total} chunks embedded successfully")

        return chunks

    def _build_token_aware_batches(
        self, chunks: list[dict], max_batch_size: int
    ) -> list[tuple[list[dict], int]]:
        max_batch_tokens = self.max_batch_tokens
        batches: list[tuple[list[dict], int]] = []
        current_batch: list[dict] = []
        current_tokens = 0

        for chunk in chunks:
            text = chunk["chunk_content"]
            tokens = _estimate_tokens(text)

            if tokens > _API_MAX_TOKENS_PER_INPUT:
                logger.warning(
                    f"[FoundryEmbedder] Chunk has {tokens} tokens "
                    f"(exceeds {_API_MAX_TOKENS_PER_INPUT} API limit). Truncating."
                )
                enc = _get_encoding()
                token_ids = enc.encode(text)[:_API_MAX_TOKENS_PER_INPUT]
                chunk["chunk_content"] = enc.decode(token_ids)
                tokens = len(token_ids)

            if tokens >= max_batch_tokens:
                if current_batch:
                    batches.append((current_batch, current_tokens))
                    current_batch = []
                    current_tokens = 0
                batches.append(([chunk], tokens))
                continue

            if (
                len(current_batch) >= max_batch_size
                or current_tokens + tokens > max_batch_tokens
            ):
                if current_batch:
                    batches.append((current_batch, current_tokens))
                current_batch = []
                current_tokens = 0

            current_batch.append(chunk)
            current_tokens += tokens

        if current_batch:
            batches.append((current_batch, current_tokens))

        return batches

    def _adaptive_throttle(self, batch_tokens: int) -> None:
        if self._remaining_tokens is not None and batch_tokens > 0:
            # Divide by instances — each sees its own headers but quota is shared
            effective_remaining = self._remaining_tokens / max(1, self.estimated_instances)
            headroom = batch_tokens * (1.0 + self.tpm_reserve_fraction)

            if effective_remaining < headroom:
                deficit = headroom - effective_remaining
                refill_rate = (self._tpm_limit or 240_000) / 60.0
                wait = min(60.0, max(0.5, deficit / max(1.0, refill_rate)))
                logger.info(
                    f"[FoundryEmbedder] Adaptive throttle: effective_remaining="
                    f"{int(effective_remaining)}, need~{int(headroom)}, sleeping {wait:.1f}s"
                )
                time.sleep(wait)

        if self._remaining_requests is not None and self._remaining_requests < 2:
            logger.info(
                f"[FoundryEmbedder] Adaptive throttle: only {self._remaining_requests} "
                f"requests remaining, sleeping 2s"
            )
            time.sleep(2.0)

    def _pace_between_batches(self, batch_tokens: int) -> None:
        tpm = self._tpm_limit or 240_000
        per_instance_tpm = tpm / max(1, self.estimated_instances)
        seconds_of_quota = (batch_tokens / per_instance_tpm) * 60.0
        pace_delay = seconds_of_quota + random.uniform(0, 0.5)
        if pace_delay > 0.1:
            time.sleep(pace_delay)

    def _read_rate_limit_headers(self, headers: Any) -> None:
        try:
            remaining_tokens = headers.get("x-ratelimit-remaining-tokens")
            remaining_requests = headers.get("x-ratelimit-remaining-requests")
            limit_tokens = headers.get("x-ratelimit-limit-tokens")
            if remaining_tokens is not None:
                self._remaining_tokens = int(remaining_tokens)
            if remaining_requests is not None:
                self._remaining_requests = int(remaining_requests)
            if limit_tokens is not None:
                self._tpm_limit = int(limit_tokens)
        except (ValueError, TypeError):
            pass

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        expected_count = len(texts)

        for attempt in range(self.max_retries):
            try:
                raw_response = self.client.embeddings.with_raw_response.create(
                    input=texts,
                    model=self.deployment,
                    dimensions=self.dimensions,
                )
                self._read_rate_limit_headers(raw_response.headers)
                response = raw_response.parse()

                if not hasattr(response, "data") or response.data is None:
                    raise ValueError(
                        f"Response missing 'data' attribute (type={type(response).__name__})"
                    )
                if len(response.data) != expected_count:
                    raise ValueError(
                        f"Expected {expected_count} embeddings, got {len(response.data)}"
                    )

                sorted_data = sorted(response.data, key=lambda x: x.index)
                return [item.embedding for item in sorted_data]

            except RateLimitError as e:
                self._consecutive_429s += 1
                self._remaining_tokens = 0
                self._remaining_requests = 0

                retry_after = getattr(e, "retry_after", None)

                if (
                    self._consecutive_429s >= 3
                    and retry_after
                    and float(retry_after) > 30
                ):
                    raise RuntimeError(
                        f"Persistent rate limiting (retry-after={retry_after}s "
                        f"after {self._consecutive_429s} consecutive 429s). "
                        f"Failing fast to release instance."
                    )

                if retry_after:
                    wait = float(retry_after) + random.uniform(0, float(retry_after) * 0.3)
                else:
                    wait = _full_jitter_backoff(attempt, self.backoff_ceiling)
                logger.warning(
                    f"[FoundryEmbedder] Rate limited (429). "
                    f"attempt={attempt + 1}/{self.max_retries}, "
                    f"consecutive_429s={self._consecutive_429s}, wait={wait:.1f}s"
                )
                time.sleep(wait)

            except (APIConnectionError, APITimeoutError) as e:
                wait = _full_jitter_backoff(attempt, self.backoff_ceiling)
                logger.warning(
                    f"[FoundryEmbedder] Transient error: {type(e).__name__}: {e}. "
                    f"attempt={attempt + 1}/{self.max_retries}, wait={wait:.1f}s"
                )
                time.sleep(wait)

            except ValueError as e:
                wait = _full_jitter_backoff(attempt, self.backoff_ceiling)
                logger.warning(
                    f"[FoundryEmbedder] Bad response: {e}. "
                    f"attempt={attempt + 1}/{self.max_retries}, wait={wait:.1f}s"
                )
                time.sleep(wait)

            except Exception as e:
                wait = _full_jitter_backoff(attempt, self.backoff_ceiling)
                logger.error(
                    f"[FoundryEmbedder] Unexpected error: {type(e).__name__}: {e}. "
                    f"attempt={attempt + 1}/{self.max_retries}, wait={wait:.1f}s",
                    exc_info=True,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"Embedding failed after {self.max_retries} retries for batch of {expected_count} texts"
        )
