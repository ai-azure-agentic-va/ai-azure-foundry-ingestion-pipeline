"""PII detection and redaction using Microsoft Presidio (open-source, runs locally).
No Azure Text Analytics. No Azure AI services. All local."""

import logging
import os

logger = logging.getLogger(__name__)

# Lazy-loaded to avoid slow import at module level
_analyzer = None
_anonymizer = None


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        # Configure spaCy model (en_core_web_md to fit Consumption Plan memory limits)
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_md"}],
        })
        nlp_engine = provider.create_engine()
        _analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
        logger.info("[PiiScanner] Presidio AnalyzerEngine initialized with en_core_web_md")
    return _analyzer


def _get_anonymizer():
    global _anonymizer
    if _anonymizer is None:
        from presidio_anonymizer import AnonymizerEngine
        _anonymizer = AnonymizerEngine()
        logger.info("[PiiScanner] Presidio AnonymizerEngine initialized")
    return _anonymizer


# Only detect truly sensitive PII entity types.
# PERSON and LOCATION are excluded — they cause false positives on business terms
# like "merchant", "customer", company names, and city references in documents.
PII_ENTITIES = [
    "US_SSN",
    "CREDIT_CARD",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "US_BANK_NUMBER",
    "US_DRIVER_LICENSE",
    "US_PASSPORT",
    "IP_ADDRESS",
]

# Redaction labels per entity type
REDACTION_OPERATORS = {
    "US_SSN": "[SSN REDACTED]",
    "CREDIT_CARD": "[CARD REDACTED]",
    "PHONE_NUMBER": "[PHONE REDACTED]",
    "EMAIL_ADDRESS": "[EMAIL REDACTED]",
    "US_BANK_NUMBER": "[BANK ACCT REDACTED]",
    "US_DRIVER_LICENSE": "[DL REDACTED]",
    "US_PASSPORT": "[PASSPORT REDACTED]",
    "IP_ADDRESS": "[IP REDACTED]",
    "DEFAULT": "[PII REDACTED]",
}

# Domain-specific terms that should never be redacted even if flagged by the model.
_DOMAIN_ALLOWLIST = {
    "merchant", "customer", "member", "borrower", "vendor", "applicant",
    "cardholder", "accountholder", "beneficiary", "guarantor", "co-signer",
    "navy federal", "nfcu", "visa", "mastercard", "american express",
}


class PiiScanner:
    """Scan text for PII and redact using Presidio (local, no Azure calls)."""

    def __init__(self, confidence_threshold: float = 0.8, enabled: bool = True):
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

    def scan_and_redact(self, text: str) -> tuple[str, bool, list[dict]]:
        """Scan text for PII and redact.

        Returns: (redacted_text, pii_found, detected_entities)
        """
        if not self.enabled:
            logger.debug("[PiiScanner] PII scanning disabled")
            return text, False, []

        if not text.strip():
            return text, False, []

        try:
            from presidio_anonymizer.entities import OperatorConfig

            analyzer = _get_analyzer()
            anonymizer = _get_anonymizer()

            # Analyze
            results = analyzer.analyze(
                text=text,
                entities=PII_ENTITIES,
                language="en",
                score_threshold=self.confidence_threshold,
            )

            if not results:
                return text, False, []

            # Filter out domain allowlisted terms
            results = [
                r for r in results
                if text[r.start:r.end].lower() not in _DOMAIN_ALLOWLIST
            ]

            if not results:
                return text, False, []

            # Build operators for redaction
            operators = {}
            for entity_type, label in REDACTION_OPERATORS.items():
                operators[entity_type] = OperatorConfig("replace", {"new_value": label})

            # Redact
            redacted = anonymizer.anonymize(
                text=text,
                analyzer_results=results,
                operators=operators,
            )

            detected = [
                {
                    "entity_type": r.entity_type,
                    "score": round(r.score, 3),
                    "start": r.start,
                    "end": r.end,
                }
                for r in results
            ]

            logger.info(f"[PiiScanner] Found {len(detected)} PII entities: {set(r.entity_type for r in results)}")
            return redacted.text, True, detected

        except ImportError:
            logger.error("[PiiScanner] Presidio not installed. Skipping PII scan.")
            return text, False, []
        except Exception as e:
            logger.error(f"[PiiScanner] Error during PII scan: {e}")
            return text, False, []
