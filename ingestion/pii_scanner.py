"""PII detection and redaction via Azure AI Language service.

Azure AI Language constraints (as of 2024-06-01 API):
  - Max 5 documents per API call (InvalidDocumentBatch if exceeded)
  - Max 5,120 characters per document (InvalidDocument if exceeded)
  - Rate limit: 429 responses under heavy load

This module enforces both limits with a single constant each, batches all
calls (including sub-chunks from long texts), retries transient failures
with jitter to prevent thundering herd, and degrades gracefully per-chunk
rather than per-document.
"""

import logging
import random
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Azure AI Language API hard limits — do NOT increase these.
# Source: https://learn.microsoft.com/en-us/azure/ai-services/language-service/
#         personally-identifiable-information/service-limits
# ---------------------------------------------------------------------------
API_MAX_DOCS_PER_BATCH = 5      # Max documents in a single recognize_pii_entities call
API_MAX_CHARS_PER_DOC = 5120    # Max characters per document
_CHUNK_TARGET_SIZE = 5000       # Leave headroom below API_MAX_CHARS_PER_DOC for word-boundary splits

# Retry settings — matches embedder's enterprise pattern
_MAX_RETRIES = 6
_RETRY_BASE_DELAY_S = 1.0      # Exponential backoff with jitter
_RETRY_CEILING_S = 30.0         # Cap backoff at 30s

_text_client = None
_client_lock = threading.Lock()


def _get_text_client():
    """Lazy-load Azure AI Text Analytics client (thread-safe singleton)."""
    global _text_client
    if _text_client is not None:
        return _text_client

    with _client_lock:
        if _text_client is not None:
            return _text_client

        from azure.ai.textanalytics import TextAnalyticsClient
        from azure.identity import DefaultAzureCredential

        from .config import settings

        endpoint = settings.FOUNDRY_PII_ENDPOINT or settings.FOUNDRY_ENDPOINT

        if not endpoint:
            raise ValueError("FOUNDRY_ENDPOINT or FOUNDRY_PII_ENDPOINT is required for FoundryPiiScanner")

        _text_client = TextAnalyticsClient(endpoint=endpoint, credential=DefaultAzureCredential())
        logger.info(f"[PiiScanner] TextAnalyticsClient initialized: endpoint={endpoint}")
    return _text_client


# ---------------------------------------------------------------------------
# PII categories to redact — intentionally narrow to avoid false positives
# on common business terms (Person, Organization, DateTime excluded).
# ---------------------------------------------------------------------------
_CATEGORY_LABELS = {
    "USSocialSecurityNumber": "[SSN REDACTED]",
    "CreditCardNumber": "[CARD REDACTED]",
    "PhoneNumber": "[PHONE REDACTED]",
    "Email": "[EMAIL REDACTED]",
    "ABARoutingNumber": "[BANK ACCT REDACTED]",
    "USBankAccountNumber": "[BANK ACCT REDACTED]",
    "USDriversLicenseNumber": "[DL REDACTED]",
    "USUKPassportNumber": "[PASSPORT REDACTED]",
    "InternationalBankingAccountNumber": "[BANK ACCT REDACTED]",
    "SWIFTCode": "[BANK ACCT REDACTED]",
    "IPAddress": "[IP REDACTED]",
    "Address": "[LOCATION REDACTED]",
}

from .config import settings as _cfg

_DEFAULT_ALLOWLIST = set(
    term.strip().lower()
    for term in _cfg.PII_DEFAULT_ALLOWLIST.split(",")
    if term.strip()
)


def _is_transient_error(exc: Exception) -> bool:
    """Return True if the exception looks like a retryable transient error."""
    msg = str(exc).lower()
    # Tightened patterns to avoid matching non-transient errors like "bad connection string"
    for signal in ("429", "too many requests", "503", "service unavailable",
                   "connection reset", "connection refused", "connection timed out",
                   "timeout", "temporarily unavailable", "502", "504"):
        if signal in msg:
            return True
    # Azure SDK HttpResponseError carries a status_code attribute
    status = getattr(exc, "status_code", None)
    if status in (429, 503, 502, 504):
        return True
    return False


def _call_pii_api_with_retry(client, documents: list[str]) -> list:
    """Call recognize_pii_entities with retry + exponential backoff + jitter.

    Args:
        client: TextAnalyticsClient instance
        documents: List of text strings (must be <= API_MAX_DOCS_PER_BATCH,
                   each <= API_MAX_CHARS_PER_DOC)

    Returns:
        List of document results from the API.

    Raises:
        Exception: After all retries exhausted.
    """
    # Production guard — never use assert (stripped by Python -O flag)
    if len(documents) > API_MAX_DOCS_PER_BATCH:
        raise ValueError(
            f"Bug: tried to send {len(documents)} docs, max is {API_MAX_DOCS_PER_BATCH}"
        )

    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            return client.recognize_pii_entities(documents=documents, language="en")
        except Exception as exc:
            last_exc = exc
            if _is_transient_error(exc) and attempt < _MAX_RETRIES - 1:
                # Full jitter backoff (matches embedder pattern)
                base_delay = min(_RETRY_CEILING_S, _RETRY_BASE_DELAY_S * (2 ** attempt))
                delay = random.uniform(0, base_delay)
                logger.warning(
                    f"[PiiScanner] Transient error (attempt {attempt + 1}/{_MAX_RETRIES}), "
                    f"retrying in {delay:.1f}s: {exc}"
                )
                time.sleep(delay)
            else:
                raise
    raise last_exc  # unreachable, but satisfies type checker


class FoundryPiiScanner:
    """Scan text for PII and redact using Azure AI Language.

    Public interface:
        scan_and_redact(text) -> (redacted_text, pii_found, detected_entities)
        scan_and_redact_batch(texts) -> list of (redacted_text, pii_found, detected_entities)
    """

    def __init__(self, confidence_threshold: float = 0.8, enabled: bool = True):
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

        # Build allowlist at init time (not module level) so env vars are resolved correctly
        from .config import settings
        env_allowlist = settings.PII_DOMAIN_ALLOWLIST
        self._domain_allowlist = (
            {term.strip().lower() for term in env_allowlist.split(",") if term.strip()}
            if env_allowlist
            else _DEFAULT_ALLOWLIST
        )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def scan_and_redact_batch(self, texts: list[str]) -> list[tuple[str, bool, list[dict]]]:
        """Batch scan multiple texts for PII.

        Handles three cases per text:
          1. Empty/whitespace -> skip
          2. Over API_MAX_CHARS_PER_DOC -> split into sub-chunks, batch those
          3. Under limit -> batch directly

        All API calls respect API_MAX_DOCS_PER_BATCH. Failures are isolated
        per-chunk (successful redactions are preserved even if some chunks fail).
        """
        if not self.enabled:
            return [(t, False, []) for t in texts]

        results: list[tuple[str, bool, list[dict]] | None] = [None] * len(texts)

        # Separate short (API-ready) texts from long texts needing sub-chunking
        short_queue: list[tuple[int, str]] = []  # (original_index, text)

        for idx, text in enumerate(texts):
            if not text or not text.strip():
                results[idx] = (text, False, [])
            elif len(text) > API_MAX_CHARS_PER_DOC:
                results[idx] = self._scan_long_text(text)
            else:
                short_queue.append((idx, text))

        # Process short texts in API-compliant batches
        if short_queue:
            self._process_short_batch(short_queue, texts, results)

        # Safety: fill any remaining None slots (should not happen, but defensive)
        for idx in range(len(results)):
            if results[idx] is None:
                logger.error(f"[PiiScanner] Chunk {idx} has no result — returning unredacted")
                results[idx] = (texts[idx], False, [])

        return results

    def scan_and_redact(self, text: str) -> tuple[str, bool, list[dict]]:
        """Scan a single text for PII and redact."""
        if not self.enabled:
            return text, False, []

        if not text or not text.strip():
            return text, False, []

        try:
            if len(text) > API_MAX_CHARS_PER_DOC:
                return self._scan_long_text(text)

            client = _get_text_client()
            api_results = _call_pii_api_with_retry(client, [text])
            doc_result = api_results[0]

            if doc_result.is_error:
                logger.error(f"[PiiScanner] API error: {doc_result.error.message}")
                return self._fallback(text, "single_scan")
            return self._process_doc_result(text, doc_result)

        except ImportError:
            logger.error("[PiiScanner] azure-ai-textanalytics not installed")
            return self._fallback(text, "missing_sdk")
        except Exception as e:
            logger.error(f"[PiiScanner] scan_and_redact failed: {e}")
            return self._fallback(text, "exception")

    # ------------------------------------------------------------------
    # Internal: batch processing for short texts
    # ------------------------------------------------------------------

    def _process_short_batch(
        self,
        queue: list[tuple[int, str]],
        all_texts: list[str],
        results: list,
    ):
        """Send short texts to the API in batches of API_MAX_DOCS_PER_BATCH."""
        try:
            client = _get_text_client()
        except Exception as e:
            logger.error(f"[PiiScanner] Failed to get client: {e}")
            for idx, _ in queue:
                results[idx] = self._fallback(all_texts[idx], "client_init")
            return

        for batch_start in range(0, len(queue), API_MAX_DOCS_PER_BATCH):
            batch_items = queue[batch_start:batch_start + API_MAX_DOCS_PER_BATCH]
            batch_texts = [text for _, text in batch_items]
            batch_indices = [idx for idx, _ in batch_items]

            try:
                api_results = _call_pii_api_with_retry(client, batch_texts)

                for j, doc_result in enumerate(api_results):
                    orig_idx = batch_indices[j]
                    orig_text = all_texts[orig_idx]

                    if doc_result.is_error:
                        logger.error(
                            f"[PiiScanner] Batch doc error (chunk_idx={orig_idx}): "
                            f"{doc_result.error.message}"
                        )
                        results[orig_idx] = self._fallback(orig_text, "doc_error")
                        continue

                    results[orig_idx] = self._process_doc_result(orig_text, doc_result)

            except Exception as e:
                logger.error(
                    f"[PiiScanner] Batch API call failed for chunks "
                    f"{batch_indices}: {e}"
                )
                for idx in batch_indices:
                    if results[idx] is None:
                        results[idx] = self._fallback(all_texts[idx], "batch_exception")

    # ------------------------------------------------------------------
    # Internal: long text handling (>5120 chars)
    # ------------------------------------------------------------------

    def _scan_long_text(self, text: str) -> tuple[str, bool, list[dict]]:
        """Split text >API_MAX_CHARS_PER_DOC into sub-chunks and scan each.

        Sub-chunks are batched in groups of API_MAX_DOCS_PER_BATCH to respect
        the API limit. Failures on individual sub-chunk batches are isolated —
        successfully scanned sub-chunks still get redacted.
        """
        # Split on word boundaries, staying under the char limit
        sub_chunks = self._split_text(text)

        # Pre-compute the char offset where each sub-chunk starts in the original
        chunk_offsets = []
        offset = 0
        for sc in sub_chunks:
            chunk_offsets.append(offset)
            offset += len(sc)

        all_entities = []  # list of (global_offset, length, entity)
        failed_chunk_indices = []

        try:
            client = _get_text_client()
        except Exception as e:
            logger.error(f"[PiiScanner] Failed to get client for long text: {e}")
            return self._fallback(text, "client_init_long")

        # Send sub-chunks in API-compliant batches
        for batch_start in range(0, len(sub_chunks), API_MAX_DOCS_PER_BATCH):
            batch = sub_chunks[batch_start:batch_start + API_MAX_DOCS_PER_BATCH]
            batch_chunk_ids = list(range(batch_start, batch_start + len(batch)))

            try:
                api_results = _call_pii_api_with_retry(client, batch)

                for j, doc_result in enumerate(api_results):
                    chunk_idx = batch_chunk_ids[j]
                    if doc_result.is_error:
                        logger.warning(
                            f"[PiiScanner] Long text sub-chunk {chunk_idx}/{len(sub_chunks)} "
                            f"error: {doc_result.error.message}"
                        )
                        failed_chunk_indices.append(chunk_idx)
                        continue

                    offset_base = chunk_offsets[chunk_idx]
                    for entity in self._filter_entities(doc_result.entities):
                        all_entities.append((
                            offset_base + entity.offset,
                            entity.length,
                            entity,
                        ))

            except Exception as e:
                logger.error(
                    f"[PiiScanner] Long text batch failed for sub-chunks "
                    f"{batch_chunk_ids}: {e}"
                )
                failed_chunk_indices.extend(batch_chunk_ids)

        if failed_chunk_indices:
            logger.warning(
                f"[PiiScanner] Long text: {len(failed_chunk_indices)}/{len(sub_chunks)} "
                f"sub-chunks failed PII scan (indices: {failed_chunk_indices}). "
                f"Redacting entities found in successful sub-chunks."
            )

        if not all_entities:
            return text, False, []

        # Apply redactions from end to start so offsets stay valid
        redacted = text
        sorted_entities = sorted(all_entities, key=lambda e: e[0], reverse=True)
        for global_offset, length, entity in sorted_entities:
            label = _CATEGORY_LABELS.get(entity.category, "[PII REDACTED]")
            redacted = redacted[:global_offset] + label + redacted[global_offset + length:]

        detected = [
            {
                "entity_type": ent.category,
                "score": round(ent.confidence_score, 3),
                "start": g_off,
                "end": g_off + length,
                "text_preview": ent.text[:20] + "..." if len(ent.text) > 20 else ent.text,
            }
            for g_off, length, ent in sorted_entities
        ]

        logger.info(
            f"[PiiScanner] Long text ({len(text)} chars, {len(sub_chunks)} sub-chunks): "
            f"found {len(detected)} PII entities"
        )
        return redacted, True, detected

    @staticmethod
    def _split_text(text: str) -> list[str]:
        """Split text into sub-chunks of <= _CHUNK_TARGET_SIZE on word boundaries."""
        chunks = []
        start = 0
        while start < len(text):
            end = start + _CHUNK_TARGET_SIZE
            if end < len(text):
                # Find last space within range to avoid splitting mid-word
                space_idx = text.rfind(" ", start, end)
                if space_idx > start:
                    end = space_idx + 1
            else:
                end = len(text)
            chunks.append(text[start:end])
            start = end
        return chunks

    # ------------------------------------------------------------------
    # Entity filtering and redaction helpers
    # ------------------------------------------------------------------

    def _filter_entities(self, entities):
        """Keep only entities in our category list that meet confidence threshold."""
        return [
            e for e in entities
            if e.category in _CATEGORY_LABELS
            and e.confidence_score >= self.confidence_threshold
            and e.text.lower() not in self._domain_allowlist
        ]

    def _process_doc_result(self, text: str, doc_result) -> tuple[str, bool, list[dict]]:
        """Extract entities from a single API doc result and apply redactions."""
        if not doc_result.entities:
            return text, False, []

        entities = self._filter_entities(doc_result.entities)
        if not entities:
            return text, False, []

        redacted_text = self._apply_custom_labels(text, entities)
        detected = self._make_detected_list(entities)
        logger.info(
            f"[PiiScanner] Found {len(entities)} PII entities: "
            f"{set(e.category for e in entities)}"
        )
        return redacted_text, True, detected

    def _apply_custom_labels(self, original_text: str, entities) -> str:
        """Replace PII entities with labeled placeholders (reverse order for stable offsets)."""
        sorted_entities = sorted(entities, key=lambda e: e.offset, reverse=True)
        result = original_text
        for entity in sorted_entities:
            label = _CATEGORY_LABELS.get(entity.category, "[PII REDACTED]")
            start = entity.offset
            end = entity.offset + entity.length
            result = result[:start] + label + result[end:]
        return result

    def _make_detected_list(self, entities) -> list[dict]:
        """Build a serializable list of detected PII for metadata."""
        return [
            {
                "entity_type": e.category,
                "score": round(e.confidence_score, 3),
                "start": e.offset,
                "end": e.offset + e.length,
                "text_preview": e.text[:20] + "..." if len(e.text) > 20 else e.text,
            }
            for e in entities
        ]

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback(text: str, reason: str) -> tuple[str, bool, list[dict]]:
        """Return text unredacted when PII scan fails. Logs the reason."""
        logger.warning(f"[PiiScanner] Returning text without redaction (reason={reason})")
        return text, False, []
