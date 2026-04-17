# AI Foundry Ingestion Pipeline

An Azure Functions pipeline that ingests documents from ADLS Gen2, parses them using Azure AI Content Understanding (with automatic fallback to custom parsers), chunks text, scans for PII, generates embeddings, and pushes vectors to Azure AI Search for RAG retrieval.

---

## End-to-End Flow

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           DOCUMENT INGESTION PIPELINE                        │
│                                                                              │
│   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌──────┐   ┌───────┐   ┌────┐ │
│   │  1.READ  │──▶│ 2.PARSE │──▶│ 3.CHUNK │──▶│4.PII │──▶│5.EMBED│──▶│6.  │ │
│   │  (ADLS)  │   │         │   │         │   │ SCAN │   │       │   │PUSH│ │
│   └─────────┘   └────┬────┘   └─────────┘   └──────┘   └───────┘   └────┘ │
│                       │                                                      │
│              ┌────────┴─────────┐                                            │
│              ▼                  ▼                                             │
│   ┌──────────────────┐  ┌─────────────────┐                                  │
│   │ Content           │  │ Fallback         │                                 │
│   │ Understanding     │  │ (Custom Parsers) │                                 │
│   │ (Azure AI)        │  │ (Local Python)   │                                 │
│   └──────────────────┘  └─────────────────┘                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Triggers

The pipeline supports three trigger modes (controlled by `TRIGGER_MODE` env var):

| Mode | How It Works |
|------|-------------|
| `EVENTGRID_DIRECT` | Blob created in ADLS → Event Grid fires directly to the Function |
| `EVENTGRID_QUEUE` | Blob created → Event Grid → Storage Queue → Function (recommended for reliability) |
| `BLOB` | Function polls ADLS container directly (simplest setup, slightly slower) |

All three triggers extract the blob path and call the same `pipeline.process_document()` method. There's also an HTTP health check at `GET /api/health`.

---

## Pipeline Stages (Detailed)

### Stage 1: Read (`ingestion/reader.py`)

Downloads the raw document bytes from ADLS Gen2. Also reads:
- **Blob metadata** — custom properties set on the blob (e.g., `source_url`, `source_type`)
- **Sidecar `.metadata.json`** — optional JSON file next to the blob with additional metadata

Metadata is merged in priority order: sidecar > blob metadata > trigger defaults.

If the blob path contains `sharepoint`, the source type is inferred as `sharepoint`. If it contains `wiki`, it's `wiki`. Otherwise `unknown`.

### Stage 2: Parse (`parsing/`)

This is where the two-path strategy lives:

```
                    ┌─────────────────────┐
                    │  File arrives for    │
                    │  parsing             │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Check extension     │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                                 │
     Binary formats                    Text-based formats
     .pdf .docx .pptx                  .md .txt .csv .json
     .jpg .png .tiff                   .xml .xlsx .xls .xlsm
              │                                 │
              ▼                                 │
   ┌─────────────────────┐                      │
   │ Azure AI Content     │                      │
   │ Understanding (CU)   │                      │
   │                      │                      │
   │ Single API call:     │                      │
   │ - Text extraction    │                      │
   │ - Table detection    │                      │
   │ - Figure verbalize   │                      │
   │ - Structured markdown│                      │
   └──────────┬──────────┘                      │
              │                                 │
     ┌────────┼────────┐                        │
     │                 │                        │
  Success           Failure                     │
     │            (CU error,                    │
     │             empty result,                │
     │             service down)                │
     │                 │                        │
     │                 ▼                        ▼
     │        ┌──────────────────────────────────┐
     │        │       Fallback Parser Factory     │
     │        │                                   │
     │        │  Extension → Parser mapping:      │
     │        │  .pdf      → PdfParser (PyMuPDF)  │
     │        │  .docx     → DocxParser           │
     │        │  .pptx     → PptxParser           │
     │        │  .xlsx/xls → XlsxParser (openpyxl)│
     │        │  .md       → MarkdownParser       │
     │        │  .txt/csv  → TextParser           │
     │        │  unknown   → UTF-8 decode attempt │
     │        └──────────────────────────────────┘
     │                 │
     ▼                 ▼
   ┌───────────────────────┐
   │   ParseResult          │
   │   - full_text (str)    │
   │   - pages (list)       │
   │   - page_count (int)   │
   │   - metadata (dict)    │
   └───────────────────────┘
```

**Why the split?**
- **Binary formats** (PDF, DOCX, PPTX, images) benefit from CU — it extracts text, tables, and figures in one call, producing structured markdown optimized for RAG.
- **Text-based formats** (.md, .xlsx, .txt, .csv, .json, .xml) gain nothing from CU — they're already text or need specialized parsing (e.g., openpyxl for Excel sheets). Sending them through CU wastes time and money.
- **Automatic fallback** ensures the pipeline never fails just because CU is down or returns empty results.

### Stage 3: Chunk (`ingestion/chunker.py`)

Splits the parsed text into token-sized chunks using tiktoken (cl100k_base encoding). The chunking strategy is chosen automatically based on file type:

| File Type | Strategy | How It Works |
|-----------|----------|-------------|
| `.pdf` | **Semantic** | Splits by page boundaries, then by tokens within each page |
| `.md` | **Header-based** | Uses pre-parsed markdown sections (from mistune AST), then splits large sections by tokens |
| `.xlsx` | **Sheet-based** | Splits by Excel sheet name, then by rows within each sheet |
| `.pptx` | **Semantic** | Splits by slide, then by tokens |
| Everything else | **Recursive** | Token-based splitting with separators: `\n\n` → `\n` → `. ` → ` ` |

Each strategy is configurable via env vars (`CHUNK_STRATEGY_PDF`, `CHUNK_STRATEGY_MD`, etc.).

Default chunk size: **1024 tokens** with **200 token overlap**.

Every chunk gets a unique ID (`base64(file_path + chunk_index)`), a title, and metadata from the parsed document.

### Stage 4: PII Scan (`ingestion/pii_scanner.py`)

Detects and redacts personally identifiable information using Azure AI Language service.

- Sends chunks in **batches of 25** (Azure's per-request limit) for efficiency
- Redacts: SSN, credit card numbers, phone numbers, emails, addresses, etc.
- Configurable **confidence threshold** (default 0.8) — only entities above this score are redacted
- Configurable **domain allowlist** — exclude known-safe domains (e.g., company emails) from redaction
- **Non-fatal** — if PII scanning fails, the pipeline continues without redaction (logs a warning)
- Handles long text by splitting at **word boundaries** (not arbitrary char positions) to avoid bisecting PII entities

### Stage 5: Embed (`ingestion/embedder.py`)

Generates vector embeddings using Azure OpenAI via the Foundry endpoint.

- Model: `text-embedding-3-large` (3072 dimensions)
- Processes chunks in **configurable batches** (default 16)
- Retries on rate limits (`RateLimitError`), transient network errors (`APIConnectionError`), and timeouts (`APITimeoutError`) with exponential backoff

### Stage 6: Push (`ingestion/search_pusher.py`)

Upserts chunks to Azure AI Search using `merge_or_upload` (idempotent — safe to re-run).

- Batch size: **1000 documents per request** (Azure's maximum)
- Auto-creates the search index on startup if it doesn't exist
- Index schema includes: vector field (3072d HNSW), text content, metadata fields, and an integrated vectorizer for auto-vectorized queries
- Returns success/failed counts per batch

---

## Project Structure

```
ai-azure-foundry-ingestion-pipeline/
│
├── function_app.py              # Azure Functions entry point (3 triggers + health check)
├── host.json                    # Functions host config
├── requirements.txt             # Python dependencies
├── .env.example                 # All environment variables with descriptions
├── local.settings.json          # Local dev settings
├── deploy.sh / deploy.ps1       # Deployment scripts (infra + code)
│
├── ingestion/                   # Pipeline orchestration and stages
│   ├── config.py                # Centralized settings — ONLY file that reads os.environ
│   ├── exceptions.py            # IngestionError → ParseError, ChunkError, EmbeddingError, etc.
│   ├── pipeline.py              # FoundryDocPipeline — orchestrates all 6 stages
│   ├── reader.py                # ADLS Gen2 blob reader + metadata + sidecar
│   ├── chunker.py               # ChunkerFactory + TokenChunker, MarkdownChunker, SheetChunker, SemanticChunker
│   ├── embedder.py              # OpenAI embeddings with retry/backoff
│   ├── pii_scanner.py           # Azure AI Language PII detection/redaction (batched)
│   └── search_pusher.py         # Azure AI Search push (batched, auto-creates index)
│
├── parsing/                     # Document parsing (CU primary + fallback parsers)
│   ├── content_understanding.py # Azure AI Content Understanding — primary parser
│   ├── fallback.py              # ParserFactory — routes extensions to correct fallback
│   ├── base.py                  # ParseResult dataclass + BaseParser abstract class
│   ├── pdf.py                   # PyMuPDF — page-by-page text + metadata extraction
│   ├── docx.py                  # python-docx — paragraph + table extraction
│   ├── xlsx.py                  # openpyxl — sheet-by-sheet with headers
│   ├── pptx.py                  # python-pptx — slide-by-slide with notes
│   ├── markdown.py              # mistune AST — section-aware parsing with frontmatter
│   └── txt.py                   # Plain text / CSV / JSON / XML (UTF-8 decode)
│
├── README.md
├── LICENSE
└── .gitignore
```

---

## Configuration

All environment variables are centralized in `ingestion/config.py`. No other file reads `os.environ`.

### Azure AI Services

| Variable | Description | Default |
|----------|-------------|---------|
| `FOUNDRY_ENDPOINT` | Azure AI Foundry endpoint URL | *required* |
| `FOUNDRY_API_VERSION` | API version | `2024-06-01` |
| `FOUNDRY_ANALYZER_ID` | Content Understanding analyzer | `prebuilt-documentSearch` |

### Embeddings

| Variable | Description | Default |
|----------|-------------|---------|
| `FOUNDRY_EMBEDDING_DEPLOYMENT` | Embedding model deployment name | `text-embedding-3-large` |
| `FOUNDRY_EMBEDDING_MODEL` | Embedding model name | `text-embedding-3-large` |
| `FOUNDRY_EMBEDDING_DIMENSIONS` | Vector dimensions | `3072` |
| `EMBEDDING_BATCH_SIZE` | Chunks per embedding API call | `16` |

### Azure AI Search

| Variable | Description | Default |
|----------|-------------|---------|
| `SEARCH_ENDPOINT` | Azure AI Search endpoint | *required* |
| `SEARCH_INDEX_NAME` | Target index name | `rag-index` |

### ADLS / Storage

| Variable | Description | Default |
|----------|-------------|---------|
| `ADLS_ACCOUNT_NAME` | Storage account name | *required* |
| `ADLS_CONTAINER_RAW` | Source container for raw documents | `raw-documents` |
| `ADLS_CONTAINER_FAILED` | Container for failed documents | `failed-documents` |

### PII Scanning

| Variable | Description | Default |
|----------|-------------|---------|
| `PII_ENABLED` | Enable/disable PII scanning | `true` |
| `PII_CONFIDENCE_THRESHOLD` | Minimum confidence for redaction (0.0–1.0) | `0.8` |
| `FOUNDRY_PII_ENDPOINT` | PII service endpoint (falls back to `FOUNDRY_ENDPOINT`) | `None` |
| `PII_DOMAIN_ALLOWLIST` | Comma-separated domains to exclude from PII redaction | `""` |

### Chunking

| Variable | Description | Default |
|----------|-------------|---------|
| `CHUNK_SIZE_TOKENS` | Target chunk size in tokens | `1024` |
| `CHUNK_OVERLAP_TOKENS` | Overlap between consecutive chunks | `200` |
| `CHUNK_STRATEGY_PDF` | Strategy for PDFs | `semantic` |
| `CHUNK_STRATEGY_MD` | Strategy for Markdown | `header_based` |
| `CHUNK_STRATEGY_XLSX` | Strategy for Excel | `sheet_based` |
| `CHUNK_STRATEGY_PPTX` | Strategy for PowerPoint | `semantic` |
| `CHUNK_STRATEGY_DEFAULT` | Fallback strategy | `recursive` |

### Function App

| Variable | Description | Default |
|----------|-------------|---------|
| `TRIGGER_MODE` | `BLOB`, `EVENTGRID_QUEUE`, or `EVENTGRID_DIRECT` | `BLOB` |
| `QUEUE_NAME` | Queue name (for queue trigger mode) | `doc-processing-queue` |
| `LOG_LEVEL` | Python logging level | `INFO` |
| `FUNCTION_APP_NAME` | App name (for health check response) | `ai-foundry-processing` |

---

## Deployment

```bash
# 1. Configure
cp .env.example .env
# Fill in Azure resource values

# 2. Deploy (creates infra + publishes code)
./deploy.sh
```

The deploy script:
1. Creates the Azure Function App (if needed)
2. Assigns Managed Identity with RBAC roles for ADLS, AI Search, Key Vault
3. Ensures OpenAI model deployments exist
4. Configures all app settings from `.env`
5. Publishes the function code

---

## Local Development

```bash
cp .env.example .env
# Fill in .env with your Azure resource values

pip install -r requirements.txt
func start
```

Test the health check:
```bash
curl http://localhost:7071/api/health
```

Upload a document to the configured ADLS container to trigger processing.
