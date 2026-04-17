"""PII detection and redaction via Azure AI Language service.

Azure AI Language hard limits:
  - Max 5 documents per API call
  - Max 5,120 characters per document
"""

import logging
import random
import threading
import time

logger = logging.getLogger(__name__)

API_MAX_DOCS_PER_BATCH = 5
API_MAX_CHARS_PER_DOC = 5120
_CHUNK_TARGET_SIZE = 5000  # headroom below API_MAX_CHARS_PER_DOC for word-boundary splits

_MAX_RETRIES = 6
_RETRY_BASE_DELAY_S = 1.0
_RETRY_CEILING_S = 30.0

_text_client = None
_client_lock = threading.Lock()


def _get_text_client():
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
    msg = str(exc).lower()
    for signal in ("429", "too many requests", "503", "service unavailable",
                   "connection reset", "connection refused", "connection timed out",
                   "timeout", "temporarily unavailable", "502", "504"):
        if signal in msg:
            return True
    status = getattr(exc, "status_code", None)
    if status in (429, 503, 502, 504):
        return True
    return False


def _call_pii_api_with_retry(client, documents: list[str]) -> list:
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
                base_delay = min(_RETRY_CEILING_S, _RETRY_BASE_DELAY_S * (2 ** attempt))
                delay = random.uniform(0, base_delay)
                logger.warning(
                    f"[PiiScanner] Transient error (attempt {attempt + 1}/{_MAX_RETRIES}), "
                    f"retrying in {delay:.1f}s: {exc}"
                )
                time.sleep(delay)
            else:
                raise
    raise last_exc


class FoundryPiiScanner:

    def __init__(self, confidence_threshold: float = 0.8, enabled: bool = True):
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

        from .config import settings
        env_allowlist = settings.PII_DOMAIN_ALLOWLIST
        self._domain_allowlist = (
            {term.strip().lower() for term in env_allowlist.split(",") if term.strip()}
            if env_allowlist
            else _DEFAULT_ALLOWLIST
        )

    def scan_and_redact_batch(self, texts: list[str]) -> list[tuple[str, bool, list[dict]]]:
        if not self.enabled:
            return [(t, False, []) for t in texts]

        results: list[tuple[str, bool, list[dict]] | None] = [None] * len(texts)
        short_queue: list[tuple[int, str]] = []

        for idx, text in enumerate(texts):
            if not text or not text.strip():
                results[idx] = (text, False, [])
            elif len(text) > API_MAX_CHARS_PER_DOC:
                results[idx] = self._scan_long_text(text)
            else:
                short_queue.append((idx, text))

        if short_queue:
            self._process_short_batch(short_queue, texts, results)

        for idx in range(len(results)):
            if results[idx] is None:
                logger.error(f"[PiiScanner] Chunk {idx} has no result — returning unredacted")
                results[idx] = (texts[idx], False, [])

        return results

    def scan_and_redact(self, text: str) -> tuple[str, bool, list[dict]]:
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

    def _process_short_batch(self, queue: list[tuple[int, str]], all_texts: list[str], results: list):
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
                    if doc_result.is_error:
                        logger.error(
                            f"[PiiScanner] Batch doc error (chunk_idx={orig_idx}): "
                            f"{doc_result.error.message}"
                        )
                        results[orig_idx] = self._fallback(all_texts[orig_idx], "doc_error")
                        continue
                    results[orig_idx] = self._process_doc_result(all_texts[orig_idx], doc_result)

            except Exception as e:
                logger.error(f"[PiiScanner] Batch API call failed for chunks {batch_indices}: {e}")
                for idx in batch_indices:
                    if results[idx] is None:
                        results[idx] = self._fallback(all_texts[idx], "batch_exception")

    def _scan_long_text(self, text: str) -> tuple[str, bool, list[dict]]:
        sub_chunks = self._split_text(text)

        chunk_offsets = []
        offset = 0
        for sc in sub_chunks:
            chunk_offsets.append(offset)
            offset += len(sc)

        all_entities = []
        failed_chunk_indices = []

        try:
            client = _get_text_client()
        except Exception as e:
            logger.error(f"[PiiScanner] Failed to get client for long text: {e}")
            return self._fallback(text, "client_init_long")

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
                        all_entities.append((offset_base + entity.offset, entity.length, entity))

            except Exception as e:
                logger.error(
                    f"[PiiScanner] Long text batch failed for sub-chunks {batch_chunk_ids}: {e}"
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

        # Apply redactions end-to-start so offsets stay valid
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
        chunks = []
        start = 0
        while start < len(text):
            end = start + _CHUNK_TARGET_SIZE
            if end < len(text):
                space_idx = text.rfind(" ", start, end)
                if space_idx > start:
                    end = space_idx + 1
            else:
                end = len(text)
            chunks.append(text[start:end])
            start = end
        return chunks

    def _filter_entities(self, entities):
        return [
            e for e in entities
            if e.category in _CATEGORY_LABELS
            and e.confidence_score >= self.confidence_threshold
            and e.text.lower() not in self._domain_allowlist
        ]

    def _process_doc_result(self, text: str, doc_result) -> tuple[str, bool, list[dict]]:
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
        sorted_entities = sorted(entities, key=lambda e: e.offset, reverse=True)
        result = original_text
        for entity in sorted_entities:
            label = _CATEGORY_LABELS.get(entity.category, "[PII REDACTED]")
            start = entity.offset
            end = entity.offset + entity.length
            result = result[:start] + label + result[end:]
        return result

    def _make_detected_list(self, entities) -> list[dict]:
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

    @staticmethod
    def _fallback(text: str, reason: str) -> tuple[str, bool, list[dict]]:
        logger.warning(f"[PiiScanner] Returning text without redaction (reason={reason})")
        return text, False, []
