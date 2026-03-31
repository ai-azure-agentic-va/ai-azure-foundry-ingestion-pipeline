"""Document parsing via Azure AI Content Understanding through Foundry endpoint.

Replaces Document Intelligence + GPT-4o Vision with a single unified API call.
Content Understanding extracts text, tables, figures, and generates structured
markdown optimized for RAG — all in one call.

Text-based formats (.md, .txt, etc.) are routed directly to custom parsers
since CU adds no value for files that are already structured text.

Used when DOC_PROCESSING=AI_FOUNDRY_SERVICES."""

import logging
import os

logger = logging.getLogger(__name__)

# File extensions that are already structured text — CU adds no value for these.
# These formats don't need OCR, layout analysis, or figure verbalization.
# Routing them directly to custom parsers avoids ~30s of wasted CU API timeout.
_DIRECT_PARSE_EXTENSIONS = {
    ".md",
    ".markdown",  # Markdown
    ".txt",
    ".text",  # Plain text
    ".csv",  # CSV (tabular text)
    ".json",  # JSON
    ".xml",  # XML
    ".xlsx",
    ".xls",
    ".xlsm",  # Excel — openpyxl handles natively, skip CU
}


class FoundryParser:
    """Parse documents using Azure AI Content Understanding via Foundry.

    Handles binary file types (PDF, DOCX, XLSX, PPTX, images) in one API call.
    Returns structured markdown with tables, figures, and layout preserved.
    Figure analysis (image verbalization) is handled natively by the analyzer —
    no separate GPT-4o Vision call is needed.

    Text-based formats (.md, .txt, etc.) bypass CU and route directly to
    custom parsers for instant processing.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        analyzer_id: str | None = None,
    ):
        self.endpoint = endpoint or os.environ.get("FOUNDRY_ENDPOINT")
        self.analyzer_id = analyzer_id or os.environ.get(
            "FOUNDRY_ANALYZER_ID",
            # Backward compat: fall back to old env var if set
            os.environ.get("FOUNDRY_DOC_INTELLIGENCE_MODEL", "prebuilt-documentSearch"),
        )

        if not self.endpoint:
            raise ValueError("FOUNDRY_ENDPOINT is required for FoundryParser")

        # Lazy-loaded client
        self._client = None

        logger.info(
            f"[FoundryParser] Initialized: endpoint={self.endpoint}, "
            f"analyzer={self.analyzer_id}"
        )

    def _get_client(self) -> "ContentUnderstandingClient":
        """Lazy-load Content Understanding client."""
        if self._client is None:
            from azure.ai.contentunderstanding import ContentUnderstandingClient
            from azure.identity import DefaultAzureCredential
            from azure.core.credentials import AzureKeyCredential

            api_key = os.environ.get("FOUNDRY_API_KEY")
            if api_key:
                credential = AzureKeyCredential(api_key)
            else:
                credential = DefaultAzureCredential()

            self._client = ContentUnderstandingClient(
                endpoint=self.endpoint,
                credential=credential,
            )
            logger.info("[FoundryParser] ContentUnderstandingClient initialized")
        return self._client

    def parse(self, file_bytes: bytes, file_name: str = "document") -> "ParseResult":
        """Parse document using Content Understanding.

        Text-based formats (.md, .txt, .csv, .json, .xml) are routed directly
        to custom parsers — they don't need CU's OCR/layout capabilities.

        Args:
            file_bytes: Raw document bytes
            file_name: Original file name (for logging)

        Returns:
            ParseResult with extracted text, pages, and metadata
        """
        from .parsers.base import ParseResult

        # Route text-based formats directly to custom parsers — CU adds no
        # value and would waste ~30s on an API call that returns empty results.
        ext = os.path.splitext(file_name)[1].lower()
        if ext in _DIRECT_PARSE_EXTENSIONS:
            logger.info(
                f"[FoundryParser] Text format '{ext}' detected — "
                f"routing directly to custom parser (skipping CU)"
            )
            return self._fallback_parse(file_bytes, file_name)

        logger.info(
            f"[FoundryParser] Analyzing '{file_name}' ({len(file_bytes)} bytes) with {self.analyzer_id}"
        )

        try:
            client = self._get_client()

            # Single API call replaces Document Intelligence + GPT-4o Vision.
            # Content Understanding returns structured markdown with tables,
            # figures, and layout already included.
            poller = client.begin_analyze_binary(
                analyzer_id=self.analyzer_id,
                binary_input=file_bytes,
            )
            result = poller.result()

            # Defensive check: CU may return empty contents[] for unsupported
            # or edge-case formats. Fall back gracefully instead of crashing.
            if not result.contents:
                logger.warning(
                    f"[FoundryParser] CU returned empty contents[] for '{file_name}' — "
                    f"falling back to custom parser"
                )
                return self._fallback_parse(file_bytes, file_name)

            content = result.contents[0]
            full_text = content.markdown or ""

            # If CU returned a content object but no actual text, fall back
            if not full_text.strip():
                logger.warning(
                    f"[FoundryParser] CU returned empty markdown for '{file_name}' — "
                    f"falling back to custom parser"
                )
                return self._fallback_parse(file_bytes, file_name)

            # Extract structural metadata from DocumentContent
            pages = []
            table_count = 0
            figure_count = 0

            from azure.ai.contentunderstanding.models import DocumentContent

            if isinstance(content, DocumentContent):
                if content.pages:
                    for page in content.pages:
                        pages.append(
                            {
                                "page_number": page.page_number,
                                "text": "",  # Full text lives in markdown, not per-page
                            }
                        )
                if content.tables:
                    table_count = len(content.tables)
                if hasattr(content, "figures") and content.figures:
                    figure_count = len(content.figures)

            page_count = len(pages) if pages else 1

            logger.info(
                f"[FoundryParser] Extracted {len(full_text)} chars, "
                f"{page_count} pages, {table_count} tables, "
                f"{figure_count} figures"
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
            logger.error(
                f"[FoundryParser] Content Understanding failed for '{file_name}': {e}"
            )
            logger.warning("[FoundryParser] Falling back to custom parser")
            return self._fallback_parse(file_bytes, file_name)

    def _fallback_parse(self, file_bytes: bytes, file_name: str):
        """Fall back to custom parser if Content Understanding fails or is skipped."""
        from .parsers import ParserFactory

        return ParserFactory.parse(file_bytes, file_name)
