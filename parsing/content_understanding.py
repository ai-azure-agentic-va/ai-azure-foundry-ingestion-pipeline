"""Document parsing via Azure AI Content Understanding through Foundry endpoint."""

import io
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

# Image formats that may need resolution preprocessing
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

# Azure CU max: 10,000 x 10,000 pixels
_CU_MAX_DIMENSION = 10000


# Max file size for image preprocessing (200MB) — prevents OOM on Consumption plan
_MAX_IMAGE_BYTES = 200 * 1024 * 1024

# Extension to PIL format mapping
_EXT_TO_FORMAT = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG",
    ".tiff": "TIFF", ".bmp": "BMP",
}


def _preprocess_image(file_bytes: bytes, file_name: str) -> bytes:
    """Downscale images exceeding CU's 10K x 10K limit. Returns original bytes if OK."""
    if len(file_bytes) > _MAX_IMAGE_BYTES:
        logger.warning(
            f"[ImagePreprocess] '{file_name}' is {len(file_bytes) / 1024 / 1024:.0f}MB "
            f"— exceeds {_MAX_IMAGE_BYTES / 1024 / 1024:.0f}MB limit, skipping resize"
        )
        return file_bytes

    try:
        from PIL import Image

        # Guard against decompression bombs (e.g., 30K×40K TIFF = 4.8GB in RAM)
        Image.MAX_IMAGE_PIXELS = 178_956_970  # ~13K×13K

        img = Image.open(io.BytesIO(file_bytes))
        w, h = img.size

        if w <= _CU_MAX_DIMENSION and h <= _CU_MAX_DIMENSION:
            return file_bytes

        scale = min(_CU_MAX_DIMENSION / w, _CU_MAX_DIMENSION / h)
        new_w, new_h = int(w * scale), int(h * scale)
        logger.info(
            f"[ImagePreprocess] Resizing '{file_name}' from {w}x{h} to {new_w}x{new_h}"
        )

        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        ext = os.path.splitext(file_name)[1].lower()
        fmt = _EXT_TO_FORMAT.get(ext, "PNG")
        img.save(buf, format=fmt)
        return buf.getvalue()

    except ImportError:
        logger.warning("[ImagePreprocess] Pillow not installed — skipping resize")
        return file_bytes
    except Exception as e:
        logger.warning(f"[ImagePreprocess] Failed to preprocess '{file_name}': {e}")
        return file_bytes


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

        # Preprocess images that exceed CU's 10K x 10K pixel limit
        is_image = ext in _IMAGE_EXTENSIONS
        if is_image:
            file_bytes = _preprocess_image(file_bytes, file_name)

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
                logger.warning(f"[FoundryParser] CU returned empty contents[] for '{file_name}'")
                if is_image:
                    return self._parse_image_with_doc_intelligence(file_bytes, file_name)
                return self._fallback_parse(file_bytes, file_name)

            content = result.contents[0]
            full_text = content.markdown or ""

            if not full_text.strip():
                logger.warning(f"[FoundryParser] CU returned empty markdown for '{file_name}'")
                if is_image:
                    return self._parse_image_with_doc_intelligence(file_bytes, file_name)
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
            if is_image:
                logger.info("[FoundryParser] Image CU failed — trying Doc Intelligence OCR")
                return self._parse_image_with_doc_intelligence(file_bytes, file_name)
            logger.warning("[FoundryParser] Falling back to custom parser")
            return self._fallback_parse(file_bytes, file_name)

    def _parse_image_with_doc_intelligence(self, file_bytes: bytes, file_name: str) -> "ParseResult":
        """Parse image using Azure Document Intelligence (prebuilt-read) for OCR."""
        from parsing.base import ParseResult

        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.identity import DefaultAzureCredential
            from ingestion.config import settings as _di_cfg

            endpoint = _di_cfg.DOC_INTELLIGENCE_ENDPOINT or _di_cfg.FOUNDRY_ENDPOINT
            credential = DefaultAzureCredential()

            # Images already preprocessed before CU attempt
            client = DocumentIntelligenceClient(endpoint=endpoint, credential=credential)
            poller = client.begin_analyze_document(
                "prebuilt-read",
                analyze_request=file_bytes,
                content_type="application/octet-stream",
            )
            result = poller.result()

            text = result.content or ""
            page_count = len(result.pages) if result.pages else 1

            logger.info(
                f"[DocIntelligence] Extracted {len(text)} chars, {page_count} pages from '{file_name}'"
            )

            return ParseResult(
                full_text=text,
                page_count=page_count,
                metadata={"format": "doc_intelligence", "model": "prebuilt-read"},
            )

        except ImportError:
            logger.warning("[DocIntelligence] azure-ai-documentintelligence not installed")
            return ParseResult(full_text="", metadata={"format": "unsupported", "error": "no_di_sdk"})
        except Exception as e:
            logger.error(f"[DocIntelligence] Failed for '{file_name}': {e}")
            return ParseResult(full_text="", metadata={"format": "unsupported", "error": str(e)})

    def _fallback_parse(self, file_bytes: bytes, file_name: str) -> "ParseResult":
        """Fall back to custom parser if Content Understanding fails or is skipped."""
        from parsing.fallback import ParserFactory

        return ParserFactory.parse(file_bytes, file_name)
