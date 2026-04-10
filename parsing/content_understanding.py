"""Document parsing via Azure AI Content Understanding through Foundry endpoint."""

import logging
import os
import threading

logger = logging.getLogger(__name__)

# Text-based formats bypass Content Understanding — CU adds no value for these
_DIRECT_PARSE_EXTENSIONS = {
    ".md", ".markdown",
    ".txt", ".text",
    ".csv", ".json", ".xml",
    ".xlsx", ".xls", ".xlsm",
}


class FoundryParser:
    """Parse documents using Azure AI Content Understanding via Foundry.

    Binary formats (PDF, DOCX, PPTX, images) go through CU in one API call.
    Text-based formats bypass CU and route directly to fallback parsers.
    """

    def __init__(self, endpoint: str | None = None, analyzer_id: str | None = None):
        from ingestion.config import settings

        self.endpoint = endpoint or settings.FOUNDRY_ENDPOINT
        self.analyzer_id = analyzer_id or settings.FOUNDRY_ANALYZER_ID

        if not self.endpoint:
            raise ValueError("FOUNDRY_ENDPOINT is required for FoundryParser")

        self._client = None
        self._client_lock = threading.Lock()

        logger.info(
            f"[FoundryParser] Initialized: endpoint={self.endpoint}, analyzer={self.analyzer_id}"
        )

    def _get_client(self) -> "ContentUnderstandingClient":
        """Lazy-load Content Understanding client (thread-safe)."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    from azure.ai.contentunderstanding import ContentUnderstandingClient
                    from azure.core.credentials import AzureKeyCredential
                    from azure.identity import DefaultAzureCredential

                    from ingestion.config import settings as _settings

                    api_key = _settings.FOUNDRY_API_KEY
                    credential = AzureKeyCredential(api_key) if api_key else DefaultAzureCredential()

                    self._client = ContentUnderstandingClient(
                        endpoint=self.endpoint,
                        credential=credential,
                    )
                    logger.info("[FoundryParser] ContentUnderstandingClient initialized")
        return self._client

    def parse(self, file_bytes: bytes, file_name: str = "document") -> "ParseResult":
        """Parse document using Content Understanding, with fallback to custom parsers."""
        from parsing.base import ParseResult

        ext = os.path.splitext(file_name)[1].lower()
        if ext in _DIRECT_PARSE_EXTENSIONS:
            logger.info(f"[FoundryParser] Text format '{ext}' — routing to custom parser")
            return self._fallback_parse(file_bytes, file_name)

        logger.info(
            f"[FoundryParser] Analyzing '{file_name}' ({len(file_bytes)} bytes) with {self.analyzer_id}"
        )

        try:
            client = self._get_client()

            poller = client.begin_analyze_binary(
                analyzer_id=self.analyzer_id,
                binary_input=file_bytes,
            )
            result = poller.result()

            if not result.contents:
                logger.warning(f"[FoundryParser] CU returned empty contents[] for '{file_name}' — falling back")
                return self._fallback_parse(file_bytes, file_name)

            content = result.contents[0]
            full_text = content.markdown or ""

            if not full_text.strip():
                logger.warning(f"[FoundryParser] CU returned empty markdown for '{file_name}' — falling back")
                return self._fallback_parse(file_bytes, file_name)

            pages = []
            table_count = 0
            figure_count = 0

            from azure.ai.contentunderstanding.models import DocumentContent

            if isinstance(content, DocumentContent):
                if content.pages:
                    for page in content.pages:
                        pages.append({"page_number": page.page_number, "text": ""})
                if content.tables:
                    table_count = len(content.tables)
                if hasattr(content, "figures") and content.figures:
                    figure_count = len(content.figures)

            page_count = len(pages) if pages else 1

            logger.info(
                f"[FoundryParser] Extracted {len(full_text)} chars, "
                f"{page_count} pages, {table_count} tables, {figure_count} figures"
            )

            return ParseResult(
                full_text=full_text,
                pages=pages,
                page_count=page_count,
                metadata={
                    "format": "content_understanding",
                    "analyzer": self.analyzer_id,
                    "tables_found": table_count,
                    "figures_found": figure_count,
                },
            )

        except Exception as e:
            logger.error(f"[FoundryParser] Content Understanding failed for '{file_name}': {e}")
            logger.warning("[FoundryParser] Falling back to custom parser")
            return self._fallback_parse(file_bytes, file_name)

    def _fallback_parse(self, file_bytes: bytes, file_name: str) -> "ParseResult":
        """Fall back to custom parser if Content Understanding fails or is skipped."""
        from parsing.fallback import ParserFactory

        return ParserFactory.parse(file_bytes, file_name)
