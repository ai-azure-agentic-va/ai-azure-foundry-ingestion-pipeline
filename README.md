# AI Foundry Document Ingestion Pipeline

A production-grade, serverless document ingestion pipeline built on **Azure AI Foundry** and **Azure Functions**. Upload any document (PDF, DOCX, XLSX, PPTX, Markdown, or plain text) to ADLS Gen2 blob storage -- within 30 seconds it's automatically parsed, chunked, PII-redacted, embedded, and indexed into **Azure AI Search** for hybrid retrieval (keyword + vector + semantic reranking).

Built for teams that need a **RAG-ready knowledge base** without managing indexers, skillsets, or orchestration infrastructure. The pipeline is push-only (no pull indexers), fully event-driven, and runs on consumption-plan Functions with zero idle cost.

### Why This Exists

Azure AI Search has built-in indexers and skillsets, but they come with limitations: rigid scheduling, limited file-type support, no control over chunking strategy, and no straightforward way to plug in custom PII redaction before indexing. This pipeline replaces all of that with a simple, transparent, code-first approach:

- **You control every stage.** Chunking strategy, PII categories, embedding model, batch sizes -- it's all in Python, not hidden behind portal config.
- **Two processing paths.** AI Foundry Services for production quality, or custom open-source libraries (PyMuPDF, Presidio, spaCy) for bulk loading at a fraction of the cost.
- **One deploy command.** `./deploy.sh` creates the Function App, assigns RBAC, pushes settings, and publishes code.

Drop a document into blob storage. 30 seconds later, it's searchable with all PII redacted.

---

## How It Works

```
  YOU UPLOAD A DOCUMENT                    6-STAGE PIPELINE                              SEARCHABLE
  ========================    ============================================    ========================

                              +----------+    +----------+    +----------+
                              |  1.READ  | -> |  2.PARSE | -> | 3.CHUNK  |
                              | Download |    | Extract  |    | Split    |
  +------------------------+  | from     |    | text,    |    | into     |    +----------------------+
  | ADLS Gen2              |  | ADLS     |    | tables,  |    | 1024-    |    | Azure AI Search      |
  | (Blob Storage)         |  +----------+    | images   |    | token    |    | (custom-kb-index)    |
  |                        |                  +----------+    | segments |    |                      |
  | raw-documents/         |                                  +----------+    | Hybrid Search:       |
  |   reports/q4.pdf       |                                                  |  - Keyword (BM25)    |
  |   wiki/onboarding.docx |  +----------+    +----------+    +----------+    |  - Vector (cosine)   |
  |   data/export.xlsx     |  | 4.PII    | -> | 5.EMBED  | -> | 6.INDEX  |    |  - Semantic rerank   |
  |   notes/design.md      |  | Detect & |    | Generate |    | Push to  |    |                      |
  +------------------------+  | redact   |    | 1536-dim |    | AI Search| -> | Answers in < 1 sec   |
         |                    | SSN,name |    | vectors  |    | (upsert) |    +----------------------+
         |                    | email,IP |    |          |    |          |
         | Blob trigger       +----------+    +----------+    +----------+
         | (auto-fires on     Azure Language   Foundry LLM    merge_or_upload
         |  new blob)         PII service     text-embedding  batch=100
         |                                    -3-small
         v
  Azure Function App
  (processes the document)
```

**The pipeline is fully automatic.** Upload a file. Walk away. It's indexed.

---

## What Each Stage Does

| Stage | What Happens | How |
|-------|-------------|-----|
| **1. Read** | Downloads the raw document bytes from ADLS Gen2 | `azure-storage-blob` SDK, Managed Identity |
| **2. Parse** | Extracts text, tables, images, layout into structured markdown | **AI Foundry**: Content Understanding (single API call) / **Custom**: PyMuPDF, python-docx, openpyxl, python-pptx |
| **3. Chunk** | Splits the extracted text into 1024-token overlapping segments | `tiktoken` (cl100k_base) + `langchain` splitters, 200-token overlap |
| **4. PII Redact** | Detects and replaces sensitive data: names, SSN, email, phone, IP, credit cards, addresses | **AI Foundry**: Azure Language PII (50+ entity types) / **Custom**: Presidio + spaCy (local, no API calls) |
| **5. Embed** | Generates 1536-dimensional vector for each chunk | Azure OpenAI `text-embedding-3-small` via Foundry endpoint |
| **6. Index** | Pushes chunks to Azure AI Search with idempotent upserts | `merge_or_upload_documents`, batches of 100 |

### PII Redaction Example

```
BEFORE:  "John Smith (SSN: 123-45-6789) reviewed the architecture at john@example.com"
AFTER:   "[NAME REDACTED] (SSN: [SSN REDACTED]) reviewed the architecture at [EMAIL REDACTED]"
```

### Supported File Types

| Category | Extensions | Parser |
|----------|-----------|--------|
| Documents | `.pdf` `.docx` `.doc` | Content Understanding / PyMuPDF, python-docx |
| Spreadsheets | `.xlsx` `.xls` `.csv` | Content Understanding / openpyxl |
| Presentations | `.pptx` `.ppt` | Content Understanding / python-pptx |
| Text | `.md` `.txt` `.json` `.xml` `.html` `.log` | Direct read (skips Content Understanding) |

---

## Two Processing Paths

Both paths run the same 6-stage pipeline. They differ only in **Stage 2 (Parse)** and **Stage 4 (PII)**:

| | AI Foundry Processing | Custom Processing |
|---|---|---|
| **When to use** | Production (best quality) | Bulk loading (cheapest) |
| **Parse** | Content Understanding (single API call) | PyMuPDF + python-docx + openpyxl + python-pptx |
| **PII** | Azure Language PII (cloud, 50+ entity types) | Presidio + spaCy (local, zero API calls) |
| **Deploy size** | 68 MB | 166 MB |
| **Monthly cost** | ~$110-190 | ~$6-35 |
| **Cost driver** | Content Understanding + Language PII API calls | Embeddings only (parsing & PII are free/local) |

Both paths share the same: ADLS reader, chunker, embedder, search pusher, AI Search index, and trigger infrastructure.

---

## Quick Start

### 1. Prerequisites

These Azure resources must exist before deploying:

| Resource | What It Is |
|----------|-----------|
| **ADLS Gen2 Storage Account** | Where documents land (HNS enabled) |
| **3 Blob Containers** | `raw-documents` (ingest), `raw-documents-failed` (dead letter), `processing-state` (watermarks) |
| **Azure AI Search** | The search service with `custom-kb-index` |
| **Azure AI Foundry** | For Content Understanding, Language PII, and Embeddings |

### 2. Configure

```bash
cd ai-foundry-processing
cp .env.example .env
# Edit .env -- fill in your Azure resource names and SEARCH_ADMIN_KEY
```

### 3. Deploy

**Linux / Mac:**
```bash
chmod +x deploy.sh
./deploy.sh
```

**Windows (PowerShell):**
```powershell
.\deploy.ps1
```

The deploy script:
1. Creates the Function App + storage account
2. Enables Managed Identity + assigns RBAC roles
3. Pushes all app settings from `.env`
4. Publishes the function code
5. Restarts and verifies

### 4. Test

```bash
# Upload a test document
echo "John Smith (SSN: 123-45-6789) reviewed the architecture." > /tmp/test.md

az storage blob upload \
  --account-name <your-storage-account> \
  --container-name raw-documents \
  --name test-docs/test.md \
  --file /tmp/test.md --auth-mode login

# Wait ~30 seconds, then query AI Search
curl -s "https://<your-search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: <your-search-key>" \
  -d '{"search": "architecture", "top": 5, "select": "chunk_content,pii_redacted,file_name"}'

# Expected: PII redacted, document searchable
```

---

## AI Search Index Schema

Every chunk pushed to `custom-kb-index` has these fields:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | `Edm.String` (key) | Deterministic: base64 of `{file_path}_{chunk_index}` |
| `chunk_content` | `Edm.String` | The text content (PII-redacted if applicable) |
| `content_vector` | `Collection(Edm.Single)` | 1536-dim embedding (HNSW, cosine similarity) |
| `document_title` | `Edm.String` | Original file name |
| `source_url` | `Edm.String` | Blob storage URL |
| `source_type` | `Edm.String` | `sharepoint`, `wiki`, or `unknown` |
| `file_name` | `Edm.String` | File name (filterable) |
| `chunk_index` | `Edm.Int32` | Position within document (0-based) |
| `total_chunks` | `Edm.Int32` | Total chunks for this document |
| `page_number` | `Edm.Int32` | Source page (PDFs/PPTX) |
| `last_modified` | `Edm.DateTimeOffset` | Source last-modified timestamp |
| `ingested_at` | `Edm.DateTimeOffset` | When this chunk was indexed |
| `pii_redacted` | `Edm.Boolean` | `true` if PII was found and redacted |

**Search capabilities:**
- **Keyword search** (BM25) on `chunk_content`
- **Vector search** (HNSW cosine) on `content_vector` -- `retrievable: true`
- **Semantic reranking** via `custom-kb-semantic-config`
- **Hybrid** = all three combined for best relevance

---

## Trigger Modes

The Function App supports 3 trigger modes. Set via `TRIGGER_MODE` env var:

| Mode | How It Works | When to Use |
|------|-------------|-------------|
| **`BLOB`** (default) | Function polls the blob container directly | Development, simple setups. No extra infrastructure. |
| **`EVENTGRID_QUEUE`** | Event Grid catches blob events -> pushes to Queue -> Function reads Queue | Production. Best retry semantics, dead-letter support. |
| **`EVENTGRID_DIRECT`** | Event Grid fires directly to the Function | Lowest latency. No retry queue. |

Switch modes by setting the env var and redeploying:

```bash
# Default (BLOB) -- simplest
./deploy.sh

# Production (Event Grid + Queue)
TRIGGER_MODE=EVENTGRID_QUEUE ./deploy.sh

# Low latency (Event Grid direct)
TRIGGER_MODE=EVENTGRID_DIRECT ./deploy.sh
```

---

## Error Handling

| Failure | What Happens |
|---------|-------------|
| Parse fails | Document moved to `raw-documents-failed` container with `.error.json` sidecar |
| Chunk fails | Same -- moved to failed container |
| PII scan fails | **Continues without redaction** (logged as warning, not fatal) |
| Embedding fails | Document moved to failed container |
| Search push fails | Document moved to failed container |
| Rate limited (embeddings) | Exponential backoff with jitter, up to 5 retries |
| Text > 5120 chars (PII) | Auto-splits into sub-chunks, scans each, reassembles |
| Zero-byte file uploaded | Silently skipped |
| Metadata sidecar file | Silently skipped (`.metadata.json`, `.error.json`) |

---

## Project Structure

```
ai-azure-foundry-ingestion-pipeline/
  README.md                              # This file
  .gitignore                             # Excludes .env, local.settings.json, __pycache__, .venv

  ai-foundry-processing/                 # AI Foundry Services path (production primary)
    deploy.sh / deploy.ps1               # One-command deploy (Linux/Mac/Windows)
    .env.example                         # Template -- copy to .env and fill in values
    function_app.py                      # 4 triggers: EventGrid, Queue, Blob, HTTP health
    host.json                            # Timeout (10min), queue + blob config
    requirements.txt                     # azure-ai-*, openai, langchain, tiktoken
    modules/
      pipeline.py                        # FoundryDocPipeline -- orchestrates the 6 stages
      adls_reader.py                     # Stage 1: Read from ADLS Gen2
      foundry_parser.py                  # Stage 2: Content Understanding (CU)
      chunker.py                         # Stage 3: tiktoken + langchain splitters
      foundry_pii_scanner.py             # Stage 4: Azure Language PII
      embedder.py                        # Stage 5: text-embedding-3-small via Foundry
      search_pusher.py                   # Stage 6: Push to AI Search (merge_or_upload)
      parsers/                           # Fallback parsers (for .md/.txt and CU failures)
        base.py                          # ParseResult dataclass + BaseParser ABC
        parser_factory.py                # Routes file extension -> parser
        pdf_parser.py                    # PyMuPDF
        docx_parser.py                   # python-docx
        xlsx_parser.py                   # openpyxl
        pptx_parser.py                   # python-pptx
        markdown_parser.py               # UTF-8 decode
        txt_parser.py                    # UTF-8/latin-1 decode
    README.md                            # Deep-dive: architecture, RBAC, schema, costs

  custom-processing/                     # Custom Libraries path (bulk loading, cheaper)
    deploy.sh / deploy.ps1               # Same deploy structure
    .env.example                         # Same template
    function_app.py                      # Same 4 triggers
    host.json                            # Same config
    requirements.txt                     # PyMuPDF, Presidio, spaCy (larger)
    modules/
      pipeline.py                        # CustomDocPipeline -- same 6 stages, different parse/PII
      adls_reader.py                     # Same Stage 1
      chunker.py                         # Same Stage 3
      pii_scanner.py                     # Stage 4: Presidio + spaCy (local)
      embedder.py                        # Same Stage 5
      search_pusher.py                   # Same Stage 6
      parsers/                           # Primary parsers (not fallback here)
    README.md                            # Custom path docs
```

---

## Security

- **No secrets in code.** All credentials via `.env` (gitignored) or Managed Identity.
- **Managed Identity** for ADLS, Foundry, and Search (API key only for Search admin operations).
- **RBAC roles** auto-assigned by deploy script: `Storage Blob Data Contributor`, `Cognitive Services User`, `Search Index Data Contributor`, `Storage Queue Data Contributor`.
- **PII redaction** runs before embedding and indexing -- sensitive data never reaches the search index.
- **HTTPS only** enforced on Function App.

---

## Documentation

| Document | What's In It |
|----------|-------------|
| **[ai-foundry-processing/README.md](ai-foundry-processing/README.md)** | Full architecture deep-dive, Content Understanding details, RBAC setup, AI Search schema, deployment reference |
| **[custom-processing/README.md](custom-processing/README.md)** | Custom path quick-start, Presidio/spaCy setup, cost breakdown |
