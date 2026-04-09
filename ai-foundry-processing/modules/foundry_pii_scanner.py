"""PII detection and redaction via Azure AI Language service through Foundry endpoint.
Replaces Presidio (local) with Azure's cloud-based PII detection.

Used when DOC_PROCESSING=AI_FOUNDRY_SERVICES."""

import logging
import os
import threading

logger = logging.getLogger(__name__)

# Thread-safe lazy-loaded client — safe for concurrent Azure Function invocations
_text_client = None
_client_lock = threading.Lock()


def _get_text_client():
    """Lazy-load Azure AI Text Analytics client (thread-safe singleton)."""
    global _text_client
    if _text_client is not None:
        return _text_client

    with _client_lock:
        # Double-check after acquiring lock
        if _text_client is not None:
            return _text_client

        from azure.ai.textanalytics import TextAnalyticsClient
        from azure.identity import DefaultAzureCredential
        from azure.core.credentials import AzureKeyCredential

        endpoint = os.environ.get("FOUNDRY_PII_ENDPOINT") or os.environ.get(
            "FOUNDRY_ENDPOINT"
        )
        api_key = os.environ.get("FOUNDRY_PII_KEY")

        if not endpoint:
            raise ValueError(
                "FOUNDRY_ENDPOINT or FOUNDRY_PII_ENDPOINT is required for FoundryPiiScanner"
            )

        if api_key:
            credential = AzureKeyCredential(api_key)
        else:
            credential = DefaultAzureCredential()

        _text_client = TextAnalyticsClient(
            endpoint=endpoint,
            credential=credential,
        )
        logger.info(
            f"[FoundryPiiScanner] TextAnalyticsClient initialized: endpoint={endpoint}"
        )
    return _text_client


# Only redact truly sensitive PII categories.
# Broad categories like Person, Organization, DateTime are excluded because they
# cause false positives on business terms (e.g., "merchant", "customer", "Visa").
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

# Domain-specific terms that should never be redacted even if flagged by the model.
# Case-insensitive matching. Configurable via PII_DOMAIN_ALLOWLIST env var
# (comma-separated). Falls back to built-in defaults if not set.
_DEFAULT_ALLOWLIST = {
    "merchant",
    "customer",
    "member",
    "borrower",
    "vendor",
    "applicant",
    "cardholder",
    "accountholder",
    "beneficiary",
    "guarantor",
    "co-signer",
    "navy federal",
    "nfcu",
    "visa",
    "mastercard",
    "american express",
}

_env_allowlist = os.environ.get("PII_DOMAIN_ALLOWLIST", "")
_DOMAIN_ALLOWLIST = (
    {term.strip().lower() for term in _env_allowlist.split(",") if term.strip()}
    if _env_allowlist
    else _DEFAULT_ALLOWLIST
)


class FoundryPiiScanner:
    """Scan text for PII and redact using Azure AI Language PII via Foundry.

    Same interface as PiiScanner (Presidio) for drop-in replacement.
    """

    def __init__(self, confidence_threshold: float = 0.8, enabled: bool = True):
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

    def scan_and_redact(self, text: str) -> tuple[str, bool, list[dict]]:
        """Scan text for PII and redact using Azure AI Language.

        Returns: (redacted_text, pii_found, detected_entities)
        """
        if not self.enabled:
            logger.debug("[FoundryPiiScanner] PII scanning disabled")
            return text, False, []

        if not text.strip():
            return text, False, []

        try:
            client = _get_text_client()

            # Azure Text Analytics accepts lists of documents
            # Each document has a max of 5,120 characters
            if len(text) > 5120:
                return self._scan_long_text(text, client)

            result = client.recognize_pii_entities(
                documents=[text],
                language="en",
            )

            doc_result = result[0]
            if doc_result.is_error:
                logger.error(
                    f"[FoundryPiiScanner] API error: {doc_result.error.message}"
                )
                return self._fallback_scan(text)

            if not doc_result.entities:
                return text, False, []

            # Filter by: supported category, confidence threshold, and domain allowlist
            entities = [
                e
                for e in doc_result.entities
                if e.category in _CATEGORY_LABELS
                and e.confidence_score >= self.confidence_threshold
                and e.text.lower() not in _DOMAIN_ALLOWLIST
            ]

            if not entities:
                return text, False, []

            # Apply our custom labels for consistency with Presidio output
            redacted_text = self._apply_custom_labels(text, entities)

            detected = [
                {
                    "entity_type": e.category,
                    "score": round(e.confidence_score, 3),
                    "start": e.offset,
                    "end": e.offset + e.length,
                    "text_preview": e.text[:20] + "..." if len(e.text) > 20 else e.text,
                }
                for e in entities
            ]

            logger.info(
                f"[FoundryPiiScanner] Found {len(detected)} PII entities: "
                f"{set(e.category for e in entities)}"
            )
            return redacted_text, True, detected

        except ImportError:
            logger.error("[FoundryPiiScanner] azure-ai-textanalytics not installed.")
            return self._fallback_scan(text)
        except Exception as e:
            logger.error(f"[FoundryPiiScanner] Azure PII scan failed: {e}")
            return self._fallback_scan(text)

    def _apply_custom_labels(self, original_text: str, entities) -> str:
        """Replace PII entities with labeled placeholders (matching Presidio format)."""
        # Sort entities by offset descending so replacements don't shift positions
        sorted_entities = sorted(entities, key=lambda e: e.offset, reverse=True)

        result = original_text
        for entity in sorted_entities:
            label = _CATEGORY_LABELS.get(entity.category, "[PII REDACTED]")
            start = entity.offset
            end = entity.offset + entity.length
            result = result[:start] + label + result[end:]

        return result

    def _scan_long_text(self, text: str, client) -> tuple[str, bool, list[dict]]:
        """Handle texts longer than 5,120 characters by splitting into chunks."""
        chunk_size = 5000  # Leave margin under 5120 limit
        chunks = []
        for i in range(0, len(text), chunk_size):
            chunks.append(text[i : i + chunk_size])

        all_entities = []
        has_pii = False

        results = client.recognize_pii_entities(
            documents=chunks,
            language="en",
        )

        for chunk_idx, doc_result in enumerate(results):
            if doc_result.is_error:
                logger.warning(
                    f"[FoundryPiiScanner] Chunk {chunk_idx} error: {doc_result.error.message}"
                )
                continue

            offset_base = chunk_idx * chunk_size
            for entity in doc_result.entities:
                if (
                    entity.category in _CATEGORY_LABELS
                    and entity.confidence_score >= self.confidence_threshold
                    and entity.text.lower() not in _DOMAIN_ALLOWLIST
                ):
                    has_pii = True
                    all_entities.append(
                        {
                            "entity": entity,
                            "global_offset": offset_base + entity.offset,
                            "length": entity.length,
                        }
                    )

        if not has_pii:
            return text, False, []

        # Apply redactions from end to start
        redacted = text
        sorted_entities = sorted(
            all_entities, key=lambda e: e["global_offset"], reverse=True
        )
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

        logger.info(
            f"[FoundryPiiScanner] Long text: found {len(detected)} PII entities across {len(chunks)} chunks"
        )
        return redacted, True, detected

    def _fallback_scan(self, text: str) -> tuple[str, bool, list[dict]]:
        """Return text unmodified when Azure AI Language fails.
        No Presidio fallback in this Function App (spaCy not installed)."""
        logger.warning(
            "[FoundryPiiScanner] Azure PII scan failed, returning text without redaction"
        )
        return text, False, []
