"""PII detection and redaction via Azure AI Language service."""

import logging
import threading

logger = logging.getLogger(__name__)

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
        from azure.core.credentials import AzureKeyCredential
        from azure.identity import DefaultAzureCredential

        from .config import settings

        endpoint = settings.FOUNDRY_PII_ENDPOINT or settings.FOUNDRY_ENDPOINT
        api_key = settings.FOUNDRY_PII_KEY

        if not endpoint:
            raise ValueError("FOUNDRY_ENDPOINT or FOUNDRY_PII_ENDPOINT is required for FoundryPiiScanner")

        credential = AzureKeyCredential(api_key) if api_key else DefaultAzureCredential()

        _text_client = TextAnalyticsClient(endpoint=endpoint, credential=credential)
        logger.info(f"[FoundryPiiScanner] TextAnalyticsClient initialized: endpoint={endpoint}")
    return _text_client


# Only redact truly sensitive PII categories — broad categories like Person,
# Organization, DateTime are excluded to avoid false positives on business terms
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

_DEFAULT_ALLOWLIST = {
    "merchant", "customer", "member", "borrower", "vendor", "applicant",
    "cardholder", "accountholder", "beneficiary", "guarantor", "co-signer",
    "navy federal", "nfcu", "visa", "mastercard", "american express",
}

from .config import settings as _cfg

_env_allowlist = _cfg.PII_DOMAIN_ALLOWLIST
_DOMAIN_ALLOWLIST = (
    {term.strip().lower() for term in _env_allowlist.split(",") if term.strip()}
    if _env_allowlist
    else _DEFAULT_ALLOWLIST
)


class FoundryPiiScanner:
    """Scan text for PII and redact using Azure AI Language."""

    def __init__(self, confidence_threshold: float = 0.8, enabled: bool = True):
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

    def _filter_entities(self, entities):
        return [
            e for e in entities
            if e.category in _CATEGORY_LABELS
            and e.confidence_score >= self.confidence_threshold
            and e.text.lower() not in _DOMAIN_ALLOWLIST
        ]

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

    def scan_and_redact_batch(self, texts: list[str]) -> list[tuple[str, bool, list[dict]]]:
        """Batch scan multiple texts for PII."""
        if not self.enabled:
            return [(t, False, []) for t in texts]

        short_indices = []
        short_texts = []
        results = [None] * len(texts)

        for idx, text in enumerate(texts):
            if not text.strip():
                results[idx] = (text, False, [])
            elif len(text) > 5120:
                try:
                    client = _get_text_client()
                    results[idx] = self._scan_long_text(text, client)
                except Exception as e:
                    logger.error(f"[FoundryPiiScanner] Long text scan failed: {e}")
                    results[idx] = self._fallback_scan(text)
            else:
                short_indices.append(idx)
                short_texts.append(text)

        if short_texts:
            try:
                client = _get_text_client()
                api_batch_size = 25
                for batch_start in range(0, len(short_texts), api_batch_size):
                    batch = short_texts[batch_start:batch_start + api_batch_size]
                    batch_indices = short_indices[batch_start:batch_start + api_batch_size]

                    api_results = client.recognize_pii_entities(documents=batch, language="en")

                    for j, doc_result in enumerate(api_results):
                        orig_idx = batch_indices[j]
                        orig_text = texts[orig_idx]

                        if doc_result.is_error:
                            logger.error(f"[FoundryPiiScanner] Batch doc {j} error: {doc_result.error.message}")
                            results[orig_idx] = self._fallback_scan(orig_text)
                            continue

                        if not doc_result.entities:
                            results[orig_idx] = (orig_text, False, [])
                            continue

                        entities = self._filter_entities(doc_result.entities)
                        if not entities:
                            results[orig_idx] = (orig_text, False, [])
                            continue

                        redacted_text = self._apply_custom_labels(orig_text, entities)
                        results[orig_idx] = (redacted_text, True, self._make_detected_list(entities))

            except Exception as e:
                logger.error(f"[FoundryPiiScanner] Batch PII scan failed: {e}")
                for idx in short_indices:
                    if results[idx] is None:
                        results[idx] = self._fallback_scan(texts[idx])

        return results

    def scan_and_redact(self, text: str) -> tuple[str, bool, list[dict]]:
        """Scan text for PII and redact."""
        if not self.enabled:
            return text, False, []

        if not text.strip():
            return text, False, []

        try:
            client = _get_text_client()

            if len(text) > 5120:
                return self._scan_long_text(text, client)

            result = client.recognize_pii_entities(documents=[text], language="en")
            doc_result = result[0]

            if doc_result.is_error:
                logger.error(f"[FoundryPiiScanner] API error: {doc_result.error.message}")
                return self._fallback_scan(text)

            if not doc_result.entities:
                return text, False, []

            entities = self._filter_entities(doc_result.entities)
            if not entities:
                return text, False, []

            redacted_text = self._apply_custom_labels(text, entities)
            logger.info(
                f"[FoundryPiiScanner] Found {len(entities)} PII entities: "
                f"{set(e.category for e in entities)}"
            )
            return redacted_text, True, self._make_detected_list(entities)

        except ImportError:
            logger.error("[FoundryPiiScanner] azure-ai-textanalytics not installed.")
            return self._fallback_scan(text)
        except Exception as e:
            logger.error(f"[FoundryPiiScanner] Azure PII scan failed: {e}")
            return self._fallback_scan(text)

    def _apply_custom_labels(self, original_text: str, entities) -> str:
        """Replace PII entities with labeled placeholders."""
        sorted_entities = sorted(entities, key=lambda e: e.offset, reverse=True)
        result = original_text
        for entity in sorted_entities:
            label = _CATEGORY_LABELS.get(entity.category, "[PII REDACTED]")
            start = entity.offset
            end = entity.offset + entity.length
            result = result[:start] + label + result[end:]
        return result

    def _scan_long_text(self, text: str, client) -> tuple[str, bool, list[dict]]:
        """Handle texts longer than 5,120 chars by splitting on word boundaries."""
        chunk_size = 5000
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            if end < len(text):
                space_idx = text.rfind(" ", start, end)
                if space_idx > start:
                    end = space_idx + 1
            chunks.append(text[start:end])
            start = end

        all_entities = []
        has_pii = False

        chunk_offsets = []
        running_offset = 0
        for chunk in chunks:
            chunk_offsets.append(running_offset)
            running_offset += len(chunk)

        results = client.recognize_pii_entities(documents=chunks, language="en")

        for chunk_idx, doc_result in enumerate(results):
            if doc_result.is_error:
                logger.warning(f"[FoundryPiiScanner] Chunk {chunk_idx} error: {doc_result.error.message}")
                continue

            offset_base = chunk_offsets[chunk_idx]
            for entity in doc_result.entities:
                if (
                    entity.category in _CATEGORY_LABELS
                    and entity.confidence_score >= self.confidence_threshold
                    and entity.text.lower() not in _DOMAIN_ALLOWLIST
                ):
                    has_pii = True
                    all_entities.append({
                        "entity": entity,
                        "global_offset": offset_base + entity.offset,
                        "length": entity.length,
                    })

        if not has_pii:
            return text, False, []

        redacted = text
        sorted_entities = sorted(all_entities, key=lambda e: e["global_offset"], reverse=True)
        for ent_info in sorted_entities:
            entity = ent_info["entity"]
            label = _CATEGORY_LABELS.get(entity.category, "[PII REDACTED]")
            start = ent_info["global_offset"]
            end = start + ent_info["length"]
            redacted = redacted[:start] + label + redacted[end:]

        detected = [
            {
                "entity_type": e["entity"].category,
                "score": round(e["entity"].confidence_score, 3),
                "start": e["global_offset"],
                "end": e["global_offset"] + e["length"],
            }
            for e in sorted_entities
        ]

        logger.info(f"[FoundryPiiScanner] Long text: found {len(detected)} PII entities across {len(chunks)} chunks")
        return redacted, True, detected

    def _fallback_scan(self, text: str) -> tuple[str, bool, list[dict]]:
        """Return text unmodified when Azure AI Language fails."""
        logger.warning("[FoundryPiiScanner] Azure PII scan failed, returning text without redaction")
        return text, False, []
