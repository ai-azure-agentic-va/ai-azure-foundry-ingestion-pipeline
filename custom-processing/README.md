# Custom Processing — custom-processing

**Deploy Size:** 166 MB | **Python 3.11**

Cost-optimized document processing using 100% open-source Python libraries for parsing (Stage 2) and PII detection (Stage 4). All other stages (ADLS read, chunking, embeddings, AI Search push) are identical to the AI Foundry path.

---

## When to Use

- Initial bulk loads (10K+ documents) where cost matters and image verbalization is not needed
- Markdown document processing (plain text — no benefit from Document Intelligence)
- Environments where PII must never leave the Function App process boundary

**Use the AI Foundry path instead** for documents with diagrams/screenshots (image verbalization), scanned PDFs (OCR), or when higher PII accuracy justifies the API cost.

---

## Quick Start

### Configure Environment Variables

```bash
# Copy the template and fill in SEARCH_ADMIN_KEY (the only secret)
cd custom-processing
cp .env.example .env
vi .env   # Replace <replace-with-actual-key> with the actual AI Search admin key
```

### Deploy

```bash
# Deploy this Function App (shared infra must already exist)
cd custom-processing
IS_ACTIVE=true ./deploy.sh

# Verify health (replace with your Function App name from deploy.sh)
curl https://<your-func-app-name>.azurewebsites.net/api/health

# Upload a test document (replace with your ADLS account name from .env)
az storage blob upload \
  --account-name $ADLS_ACCOUNT_NAME \
  --container-name raw-documents \
  --name test-docs/test.md \
  --file /tmp/test.md --auth-mode login

# Check logs
func azure functionapp logstream <your-func-app-name>
```

---

## Dependencies

```
# Document Parsing (local, no API calls)
PyMuPDF>=1.24.0              # PDF (text + tables per page)
python-docx>=1.1.0           # Word .docx
openpyxl>=3.1.0              # Excel .xlsx
python-pptx>=0.6.23          # PowerPoint .pptx

# PII Detection (local, no API calls)
presidio-analyzer>=2.2.0     # Microsoft open-source PII detection
presidio-anonymizer>=2.2.0   # Microsoft open-source PII redaction
spacy>=3.7.0,<3.8.0          # NLP backend for Presidio NER
en_core_web_md-3.7.1         # spaCy NER model (40 MB)

# Shared with AI Foundry path
openai>=1.12.0               # Embeddings only (Stage 5)
azure-search-documents>=11.6.0  # AI Search push (Stage 6)
azure-storage-blob>=12.19.0  # ADLS read/write (Stage 1)
tiktoken>=0.6.0              # Chunking (Stage 3)
```

**NO** `azure-ai-documentintelligence`. **NO** `azure-ai-textanalytics`.

---

## Azure Service Dependencies

| Azure Service | Env Variable | What Custom Processing Uses It For |
|--------------|-------------|-----------------------------------|
| AI Foundry | `FOUNDRY_ENDPOINT` | Embeddings only (Stage 5) |
| AI Search | `SEARCH_ENDPOINT` | Push chunks to search index (Stage 6) |
| ADLS Gen2 | `ADLS_ACCOUNT_NAME` | Read raw docs, write failed docs, queue messages (Stage 1) |

Parsing (Stage 2) and PII detection (Stage 4) run entirely locally.

---

## Environment Variables

The `.env` file is the **single source of truth** for all environment variables during Azure deployment. The deploy script (`deploy.sh` in this folder) reads this file and pushes all settings to the Function App.

```bash
# Setup
cp .env.example .env
vi .env   # Replace <replace-with-actual-key> with the actual AI Search admin key
```

All common settings are shared with the AI Foundry path (ADLS, embeddings, search, queue, trigger mode). The only Custom-specific setting:

| Variable | Value |
|----------|-------|
| `DOC_PROCESSING` | `CUSTOM_LIBRARIES` |

Custom Processing does **not** use Foundry Doc Intelligence, Vision, or PII endpoints — those variables are absent from its `.env`. Parsing and PII run locally via PyMuPDF, Presidio, and spaCy.

> **Note:** `.env` is git-ignored. For local development, use `local.settings.json` (also git-ignored). Both files should be kept in sync when adding new variables.

See [ai-foundry-processing/README.md](../ai-foundry-processing/README.md) for the full environment variables reference.

---

## Project Structure

```
custom-processing/
  deploy.sh                    # Deploy this Function App (create, RBAC, settings, publish)
  .env                         # Environment variables for deployment (git-ignored, contains secrets)
  .env.example                 # Template — copy to .env and fill in SEARCH_ADMIN_KEY
  local.settings.json          # Local dev settings for `func start` (git-ignored)
  function_app.py              # 4 triggers (EventGrid, Queue, Blob, HTTP)
  host.json                    # 10-min timeout, queue + blob config
  requirements.txt             # PyMuPDF, Presidio, spaCy (NO azure-ai-*)
  modules/
    pipeline.py                # CustomDocPipeline (6-stage orchestrator)
    adls_reader.py             # ADLS read/write/state/move-to-failed
    chunker.py                 # TokenChunker + MarkdownChunker (tiktoken)
    pii_scanner.py             # Presidio + spaCy en_core_web_md
    embedder.py                # Foundry LLM text-embedding-3-large
    search_pusher.py           # AI Search merge_or_upload (batch=100)
    parsers/
      parser_factory.py        # Extension -> parser routing
      pdf_parser.py            # PyMuPDF (fitz)
      docx_parser.py           # python-docx
      xlsx_parser.py           # openpyxl
      pptx_parser.py           # python-pptx
      markdown_parser.py       # Plain text (built-in open())
      txt_parser.py            # Plain text (built-in open())
```

---

## Switching to AI Foundry Path

```bash
cd ../ai-foundry-processing
IS_ACTIVE=true TRIGGER_MODE=EVENTGRID_QUEUE ./deploy.sh
```

No code redeployment needed. Event Grid routing and trigger enable/disable settings are updated automatically.

---

## Further Reading

- [../README.md](../README.md) — Full pipeline architecture overview
