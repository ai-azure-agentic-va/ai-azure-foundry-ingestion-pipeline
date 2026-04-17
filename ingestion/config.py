"""Centralized configuration — all environment variables in one place."""

import os


class Settings:
    """All environment-driven settings for the ingestion pipeline."""

    def __init__(self):
        # ADLS / Blob Storage
        self.ADLS_ACCOUNT_NAME: str | None = os.environ.get("ADLS_ACCOUNT_NAME")
        self.ADLS_CONTAINER_RAW: str = os.environ.get("ADLS_CONTAINER_RAW", "raw-documents")
        self.ADLS_CONTAINER_FAILED: str = os.environ.get("ADLS_CONTAINER_FAILED", "raw-documents-failed")

        # Azure AI Foundry (auth via Managed Identity — DefaultAzureCredential)
        self.FOUNDRY_ENDPOINT: str | None = os.environ.get("FOUNDRY_ENDPOINT")
        self.FOUNDRY_ANALYZER_ID: str = os.environ.get(
            "FOUNDRY_ANALYZER_ID",
            os.environ.get("FOUNDRY_DOC_INTELLIGENCE_MODEL", "prebuilt-documentSearch"),
        )

        # Embeddings
        self.FOUNDRY_EMBEDDING_DEPLOYMENT: str = os.environ.get(
            "FOUNDRY_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
        )
        self.FOUNDRY_EMBEDDING_DIMENSIONS: int = int(
            os.environ.get("FOUNDRY_EMBEDDING_DIMENSIONS", "3072")
        )
        self.FOUNDRY_EMBEDDING_MODEL: str = os.environ.get(
            "FOUNDRY_EMBEDDING_MODEL", "text-embedding-3-large"
        )
        self.FOUNDRY_API_VERSION: str = os.environ.get("FOUNDRY_API_VERSION", "2024-06-01")
        self.EMBEDDING_BATCH_SIZE: int = int(os.environ.get("EMBEDDING_BATCH_SIZE", "16"))
        self.EMBEDDING_MAX_RETRIES: int = int(os.environ.get("EMBEDDING_MAX_RETRIES", "6"))
        self.EMBEDDING_BACKOFF_CEILING: float = float(
            os.environ.get("EMBEDDING_BACKOFF_CEILING", "60.0")
        )
        self.EMBEDDING_TPM_RESERVE_FRACTION: float = float(
            os.environ.get("EMBEDDING_TPM_RESERVE_FRACTION", "0.2")
        )
        self.EMBEDDING_MAX_BATCH_TOKENS: int = int(
            os.environ.get("EMBEDDING_MAX_BATCH_TOKENS", "8000")
        )
        self.EMBEDDING_ESTIMATED_INSTANCES: int = int(
            os.environ.get("EMBEDDING_ESTIMATED_INSTANCES", "30")
        )

        # PII Detection (Azure AI Language — auth via Managed Identity)
        self.FOUNDRY_PII_ENDPOINT: str | None = os.environ.get("FOUNDRY_PII_ENDPOINT")
        self.PII_CONFIDENCE_THRESHOLD: float = float(
            os.environ.get("PII_CONFIDENCE_THRESHOLD", "0.8")
        )
        self.PII_ENABLED: bool = os.environ.get("PII_ENABLED", "true").lower() == "true"
        self.PII_DOMAIN_ALLOWLIST: str = os.environ.get("PII_DOMAIN_ALLOWLIST", "")
        self.PII_DEFAULT_ALLOWLIST: str = os.environ.get(
            "PII_DEFAULT_ALLOWLIST",
            "merchant,customer,member,borrower,vendor,applicant,cardholder,accountholder,beneficiary,guarantor,co-signer",
        )

        # Azure AI Search
        self.SEARCH_ENDPOINT: str | None = os.environ.get("SEARCH_ENDPOINT")
        self.SEARCH_INDEX_NAME: str = os.environ.get("SEARCH_INDEX_NAME", "rag-index")
        self.SEARCH_SEMANTIC_CONFIG_NAME: str = os.environ.get(
            "SEARCH_SEMANTIC_CONFIG_NAME", "custom-kb-semantic-config"
        )
        self.SEARCH_PUSH_BATCH_SIZE: int = int(
            os.environ.get("SEARCH_PUSH_BATCH_SIZE", "100")
        )

        # Source type inference patterns (comma-separated)
        self.SOURCE_TYPE_WIKI_PATTERNS: str = os.environ.get(
            "SOURCE_TYPE_WIKI_PATTERNS", "wiki"
        )
        self.SOURCE_TYPE_SHAREPOINT_PATTERNS: str = os.environ.get(
            "SOURCE_TYPE_SHAREPOINT_PATTERNS", "sharepoint"
        )

        # Chunking
        self.CHUNK_SIZE_TOKENS: int = int(os.environ.get("CHUNK_SIZE_TOKENS", "2000"))
        self.CHUNK_OVERLAP_TOKENS: int = int(os.environ.get("CHUNK_OVERLAP_TOKENS", "100"))
        self.CHUNK_STRATEGY_MD: str = os.environ.get("CHUNK_STRATEGY_MD", "header_based")
        self.CHUNK_STRATEGY_XLSX: str = os.environ.get("CHUNK_STRATEGY_XLSX", "sheet_based")
        self.CHUNK_STRATEGY_PDF: str = os.environ.get("CHUNK_STRATEGY_PDF", "semantic")
        self.CHUNK_STRATEGY_DEFAULT: str = os.environ.get("CHUNK_STRATEGY_DEFAULT", "recursive")

        # File size guard — reject files larger than this before loading into memory
        self.MAX_FILE_SIZE_MB: int = int(os.environ.get("MAX_FILE_SIZE_MB", "500"))

        # PII fail policy: "halt" = skip document on PII failure, "proceed" = index unredacted
        self.PII_FAIL_POLICY: str = os.environ.get("PII_FAIL_POLICY", "halt")

        # Document Intelligence (fallback for image OCR when CU fails)
        # Uses Managed Identity (DefaultAzureCredential) — no API keys
        self.DOC_INTELLIGENCE_ENDPOINT: str | None = os.environ.get("DOC_INTELLIGENCE_ENDPOINT")

        # Function App / Triggers
        self.LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
        self.FUNCTION_APP_NAME: str = os.environ.get("FUNCTION_APP_NAME", "ai-foundry-processing")
        self.TRIGGER_MODE: str = os.environ.get("TRIGGER_MODE", "BLOB")
        self.QUEUE_NAME: str = os.environ.get("QUEUE_NAME", "doc-processing-queue")


settings = Settings()
