"""Foundry LLM embedding generator using text-embedding-3-large via Azure OpenAI.

Production-grade implementation with:
- Token-aware batching (tiktoken) to avoid exceeding TPM limits per request
- Adaptive rate limiting from x-ratelimit-remaining-* response headers
- TPM-aware pacing to prevent thundering herd across multiple instances
- Exponential backoff with full jitter on retries
- Response validation (shape + count)
- Per-batch resilience: failed batches don't kill the entire document
"""

import logging
import random
import time
from typing import Any

import tiktoken
from openai import APIConnectionError, APITimeoutError, AzureOpenAI, RateLimitError

logger = logging.getLogger(__name__)

# text-embedding-3-large uses cl100k_base encoding
_ENCODING: tiktoken.Encoding | None = None

# Azure OpenAI hard limit per input text
_API_MAX_TOKENS_PER_INPUT = 8191


def _get_encoding() -> tiktoken.Encoding:
    """Lazy-load tiktoken encoding (expensive first call, cached after)."""
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def _estimate_tokens(text: str) -> int:
    """Estimate token count for a single text string."""
    return len(_get_encoding().encode(text))


def _full_jitter_backoff(attempt: int, ceiling: float) -> float:
    """Exponential backoff with full jitter: sleep = random(0, min(ceiling, 2^attempt)).

    Per AWS Architecture Blog best-practice for distributed retries.
    """
    base = min(ceiling, 2.0 ** attempt)
    return random.uniform(0, base)


class FoundryEmbedder:
    """Generate embeddings via Foundry LLM (Azure OpenAI)."""

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

        # Retry / throttle config
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

        # Adaptive throttle state — updated after every successful API call
        self._remaining_tokens: int | None = None
        self._remaining_requests: int | None = None
        self._tpm_limit: int | None = None  # learned from x-ratelimit-limit-tokens header
        self._consecutive_429s: int = 0

        logger.info(
            f"[FoundryEmbedder] Initialized: endpoint={self.endpoint}, "
            f"deployment={self.deployment}, max_retries={self.max_retries}, "
            f"estimated_instances={self.estimated_instances}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def embed_chunks(self, chunks: list[dict], batch_size: int | None = None) -> list[dict]:
        """Generate embeddings for chunks. Adds 'content_vector' field to each chunk.

        Batches are sized by BOTH count and estimated tokens to stay within
        the Azure OpenAI TPM quota per request. Failed batches are isolated —
        successful batches are preserved even when some fail.
        """
        if batch_size is None:
            from .config import settings as _settings
            batch_size = _settings.EMBEDDING_BATCH_SIZE

        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        total = len(chunks)
        if total == 0:
            return chunks

        # Validate all chunks have string content
        for i, chunk in enumerate(chunks):
            content = chunk.get("chunk_content")
            if not content or not isinstance(content, str):
                logger.warning(
                    f"[FoundryEmbedder] Chunk {i} has invalid chunk_content "
                    f"(type={type(content).__name__}). Setting to empty placeholder."
                )
                chunk["chunk_content"] = "[empty]"

        # Build token-aware batches (returns pre-computed token counts)
        batches = self._build_token_aware_batches(chunks, batch_size)
        num_batches = len(batches)
        logger.info(
            f"[FoundryEmbedder] Embedding {total} chunks in {num_batches} "
            f"token-aware batches (max {batch_size} per batch)"
        )

        failed_chunks = 0
        for batch_num, (batch, batch_tokens) in enumerate(batches, 1):
            texts = [c["chunk_content"] for c in batch]

            # Adaptive pre-flight throttle based on remaining capacity
            self._adaptive_throttle(batch_tokens)

            try:
                embeddings = self._embed_with_retry(texts)
                for j, vec in enumerate(embeddings):
                    batch[j]["content_vector"] = vec
                self._consecutive_429s = 0  # reset on success

                # TPM-aware pacing: spread load across instances
                self._pace_between_batches(batch_tokens)

            except RuntimeError:
                logger.error(
                    f"[FoundryEmbedder] Batch {batch_num}/{num_batches} failed permanently. "
                    f"Marking {len(batch)} chunks as unembedded."
                )
                failed_chunks += len(batch)
                for chunk in batch:
                    chunk["content_vector"] = None  # signal to push stage to skip

            # Periodic progress logging at INFO level
            if batch_num % 50 == 0 or batch_num == num_batches:
                logger.info(
                    f"[FoundryEmbedder] Progress: {batch_num}/{num_batches} batches "
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

    # ------------------------------------------------------------------
    # Token-aware batching (returns pre-computed token counts per batch)
    # ------------------------------------------------------------------

    def _build_token_aware_batches(
        self, chunks: list[dict], max_batch_size: int
    ) -> list[tuple[list[dict], int]]:
        """Split chunks into batches respecting both count and token limits.

        Returns list of (batch, batch_token_count) tuples to avoid double-counting.
        Oversized chunks (>8191 tokens) are truncated to fit the API limit.
        """
        max_batch_tokens = self.max_batch_tokens
        batches: list[tuple[list[dict], int]] = []
        current_batch: list[dict] = []
        current_tokens = 0

        for chunk in chunks:
            text = chunk["chunk_content"]
            tokens = _estimate_tokens(text)

            # Truncate oversized chunks that would exceed the per-input API limit
            if tokens > _API_MAX_TOKENS_PER_INPUT:
                logger.warning(
                    f"[FoundryEmbedder] Chunk has {tokens} tokens "
                    f"(exceeds {_API_MAX_TOKENS_PER_INPUT} API limit). Truncating."
                )
                enc = _get_encoding()
                token_ids = enc.encode(text)[:_API_MAX_TOKENS_PER_INPUT]
                chunk["chunk_content"] = enc.decode(token_ids)
                tokens = len(token_ids)

            # If a single chunk fills most of a batch, give it its own
            if tokens >= max_batch_tokens:
                if current_batch:
                    batches.append((current_batch, current_tokens))
                    current_batch = []
                    current_tokens = 0
                batches.append(([chunk], tokens))
                continue

            # Would adding this chunk breach count or token limits?
            if (
                len(current_batch) >= max_batch_size
                or current_tokens + tokens > max_batch_tokens
            ):
                if current_batch:  # guard against empty batch
                    batches.append((current_batch, current_tokens))
                current_batch = []
                current_tokens = 0

            current_batch.append(chunk)
            current_tokens += tokens

        if current_batch:
            batches.append((current_batch, current_tokens))

        return batches

    # ------------------------------------------------------------------
    # Adaptive throttle from rate-limit headers
    # ------------------------------------------------------------------

    def _adaptive_throttle(self, batch_tokens: int) -> None:
        """Sleep if remaining token/request capacity is low.

        Uses the x-ratelimit-remaining-tokens header captured from the previous
        API response. Divides by estimated_instances to account for other
        instances consuming the shared quota simultaneously.
        """
        if self._remaining_tokens is not None and batch_tokens > 0:
            # Divide by estimated instances — each instance only sees its own headers,
            # but the quota is shared. This prevents optimistic over-estimation.
            effective_remaining = self._remaining_tokens / max(1, self.estimated_instances)
            headroom = batch_tokens * (1.0 + self.tpm_reserve_fraction)

            if effective_remaining < headroom:
                deficit = headroom - effective_remaining
                # Use actual TPM limit if known, else conservative default
                refill_rate = (self._tpm_limit or 240_000) / 60.0  # tokens per second
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
        """TPM-aware pacing between batches to spread load across instances.

        Even when the throttle doesn't trigger, we add a small pacing delay
        proportional to tokens consumed to prevent thundering herd.
        """
        tpm = self._tpm_limit or 240_000
        per_instance_tpm = tpm / max(1, self.estimated_instances)
        # How many seconds of quota did this batch consume?
        seconds_of_quota = (batch_tokens / per_instance_tpm) * 60.0
        # Add jitter to desynchronize instances
        pace_delay = seconds_of_quota + random.uniform(0, 0.5)
        if pace_delay > 0.1:  # only sleep if meaningful
            time.sleep(pace_delay)

    def _read_rate_limit_headers(self, headers: Any) -> None:
        """Extract rate-limit info from Azure OpenAI response headers."""
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
            logger.debug(
                f"[FoundryEmbedder] Rate-limit headers: "
                f"remaining_tokens={self._remaining_tokens}, "
                f"remaining_requests={self._remaining_requests}, "
                f"tpm_limit={self._tpm_limit}"
            )
        except (ValueError, TypeError):
            pass  # headers missing or unparseable — ignore, don't crash

    # ------------------------------------------------------------------
    # Retry logic
    # ------------------------------------------------------------------

    def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        """Call embedding API with exponential backoff + full jitter.

        Uses with_raw_response to capture rate-limit headers, then parses
        the response and validates shape.

        Returns:
            List of embedding vectors (one per input text).

        Raises:
            RuntimeError: If all retries are exhausted.
        """
        expected_count = len(texts)

        for attempt in range(self.max_retries):
            try:
                # Use with_raw_response to get HTTP headers for adaptive throttle
                raw_response = self.client.embeddings.with_raw_response.create(
                    input=texts,
                    model=self.deployment,
                    dimensions=self.dimensions,
                )

                # Read rate-limit headers for adaptive throttle
                self._read_rate_limit_headers(raw_response.headers)

                # Parse the JSON body into the typed response object
                response = raw_response.parse()

                # --- Validate response shape ---
                if not hasattr(response, "data") or response.data is None:
                    raise ValueError(
                        f"Response missing 'data' attribute (type={type(response).__name__})"
                    )
                if len(response.data) != expected_count:
                    raise ValueError(
                        f"Expected {expected_count} embeddings, got {len(response.data)}"
                    )

                # Extract vectors, sorted by index to be safe
                sorted_data = sorted(response.data, key=lambda x: x.index)
                return [item.embedding for item in sorted_data]

            except RateLimitError as e:
                self._consecutive_429s += 1
                # Reset remaining tokens — we know we're at/over quota
                self._remaining_tokens = 0
                self._remaining_requests = 0

                retry_after = getattr(e, "retry_after", None)

                # Fail fast if persistent rate limiting with long retry-after
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
                    # Add jitter on top of retry-after to desynchronize instances
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
                # Malformed response — likely caused by rate-limiting or API degradation.
                wait = _full_jitter_backoff(attempt, self.backoff_ceiling)
                logger.warning(
                    f"[FoundryEmbedder] Bad response: {e}. "
                    f"attempt={attempt + 1}/{self.max_retries}, wait={wait:.1f}s"
                )
                time.sleep(wait)

            except Exception as e:
                # Unexpected errors — log full detail, still retry with backoff
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
