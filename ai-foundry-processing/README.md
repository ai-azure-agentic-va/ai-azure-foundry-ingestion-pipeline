# AI Foundry Processing — ai-foundry-processing

**Status:** Deployed & Verified (2026-02-23) | **Environment:** dev | **Region:** East US
**Classification:** Production-Grade Design | **Target:** Production Deployment
**Function App:** `<func-foundry-app>` | **Deploy Size:** 68 MB | **Python 3.11**
**Processing Path:** `DOC_PROCESSING=AI_FOUNDRY_SERVICES` (production primary)
**Trigger Mode:** `EVENTGRID_QUEUE` (Event Grid -> Azure Queue Storage -> Function App) | Also supports `EVENTGRID_DIRECT` and `BLOB`

A production-grade, push-based document ingestion pipeline that processes documents from ADLS Gen2 into Azure AI Search using Azure AI Foundry services -- Content Understanding for universal parsing (text, tables, figures, and image verbalization in a single API call), and Azure Language for PII detection. Events flow through Azure Queue Storage (production default) or direct Event Grid, controlled by the `TRIGGER_MODE` env var.

> **Note:** A secondary Custom Libraries path (`<func-custom-app>`) exists for cost-optimized bulk loading. It uses PyMuPDF, Presidio, and spaCy instead of Foundry APIs. See [../custom-processing/README.md](../custom-processing/README.md) for deployment quick-start.

---

## Quick Start

### Prerequisites

| Prerequisite | How to verify |
|-------------|--------------|
| Azure CLI authenticated | `az account show` |
| Correct subscription set | `az account set --subscription <subscription-id>` |
| Azure Functions Core Tools | `func --version` (v4.x required) |
| Python 3.11 | `python3 --version` |

### Configure Environment Variables

```bash
# Copy the template and fill in your Azure resource endpoints
cd ai-foundry-processing
cp .env.example .env
vi .env   # Update endpoints (auth uses Managed Identity — no keys needed)
```

### Deploy

```bash
# Option 1: Full deploy from repo root (shared infra + both apps + Event Grid routing)
DOC_PROCESSING=AI_FOUNDRY_SERVICES TRIGGER_MODE=EVENTGRID_QUEUE ./deploy.sh

# Option 2: Deploy only this Function App (shared infra must already exist)
cd ai-foundry-processing
IS_ACTIVE=true ./deploy.sh

# Verify health
curl https://<func-foundry-app>.azurewebsites.net/api/health

# Check logs
func azure functionapp logstream <func-foundry-app>

# Tear down everything cleanly (from repo root)
./teardown.sh
```

### End-to-End Validation (Upload a Document, Verify PII Redaction in Search)

```bash
# 1. Create a test file with PII
echo "John Smith (SSN: 123-45-6789) reviewed the architecture for jane@example.org." > /tmp/test.md

# 2. Upload to ADLS -- Event Grid -> Queue -> Function App processes end-to-end
az storage blob upload \
  --account-name <adls-account> \
  --container-name raw-documents \
  --name test-docs/test.md \
  --file /tmp/test.md --auth-mode login

# 3. Wait ~30 seconds, then query AI Search to verify indexing + PII redaction
SEARCH_KEY=$(az search admin-key show --service-name <search-service> \
  --resource-group <shared-rg> --query primaryKey -o tsv)

curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "architecture", "top": 5, "select": "chunk_content,pii_redacted,file_name"}'

# Expected result:
#   chunk_content: "[NAME REDACTED] (SSN: [SSN REDACTED]) reviewed the architecture for [EMAIL REDACTED]."
#   pii_redacted: true
#   file_name: "test.md"
```

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Processing Pipeline: 6 Stages](#2-processing-pipeline-6-stages)
3. [Chunking Strategy & Indexing Strategy](#3-chunking-strategy--indexing-strategy)
4. [Azure AI Foundry Services -- Definitions & Usage](#4-azure-ai-foundry-services)
5. [AI Foundry Processing Path -- What & Why](#5-ai-foundry-processing-path)
6. [Event Routing: Event Grid vs Queue vs Blob (Decision Matrix)](#6-event-routing-event-grid-vs-queue-vs-blob)
7. [Trigger Design & Scheduling](#7-trigger-design--scheduling)
8. [Scale & Performance Analysis](#8-scale--performance-analysis)
9. [Embedding Model Selection](#9-embedding-model-selection)
10. [ARB Q&A -- Architecture Review Board Ready (20 Questions)](#10-arb-qa--architecture-review-board-ready)
11. [Resource Inventory](#11-resource-inventory)
12. [RBAC & Security](#12-rbac--security)
13. [AI Search Index Schema](#13-ai-search-index-schema)
14. [Environment Variables](#14-environment-variables)
15. [Project Structure](#15-project-structure)
16. [Deployment & Infrastructure](#16-deployment--infrastructure)
17. [Data Sources -- How Documents Reach ADLS](#17-data-sources--how-documents-reach-adls)
18. [Future Roadmap](#18-future-roadmap)
19. [Deployment Instructions — Two Options](#19-deployment-instructions--two-options)

---

## 1. Architecture Overview

### Pipeline -- AI Foundry Path (<func-foundry-app>)

> **One-liner:** Azure AI services parse, verbalize images, and redact PII -- captures visual content, handles scanned documents, and provides the highest search quality.

```
ADLS Gen2                   <func-foundry-app>                                AI Search
<adls-account>             (Python 3.11, Consumption Plan, 68 MB deployed)               <search-service>
                                                                                          custom-kb-index
/raw-documents/
sharepoint/{site}/{file}
        |
        |  BlobCreated -> Event Grid -> doc-processing-queue (TRIGGER_MODE=EVENTGRID_QUEUE)
        |  (or Event Grid -> Function App direct when TRIGGER_MODE=EVENTGRID_DIRECT)
        v
  +-----------+     +----------------+     +-----------+     +-------------+     +---------------+     +-------------+
  |  Stage 1  |     |    Stage 2     |     |  Stage 3  |     |   Stage 4   |     |    Stage 5    |     |   Stage 6   |
  |   READ    |---->|    PARSE       |---->|   CHUNK   |---->|    PII      |---->|    EMBED      |---->|    INDEX    |
  |           |     |                |     |           |     |             |     |               |     |             |
  | Download  |     | Doc Intel      |     | tiktoken  |     | Azure AI    |     | Foundry LLM   |     | AI Search   |
  | raw bytes |     | prebuilt-      |     | cl100k    |     | Language    |     | text-embed-   |     | SDK push    |
  | from ADLS |     | layout         |     | 1024 tok  |     | PII via     |     | 3-large       |     | merge_or_   |
  | via SDK   |     | (Foundry API)  |     | 200 ovlp  |     | Foundry     |     | 3072 dims     |     | upload      |
  |           |     |                |     |           |     | endpoint    |     | (Foundry API) |     | batch=100   |
  | Managed   |     | + GPT-4o       |     | Same      |     |             |     |               |     |             |
  | Identity  |     |   Vision for   |     | chunker   |     | Returns pre-|     | Same embedder |     | Idempotent  |
  | auth      |     |   images ->    |     |           |     | redacted    |     |               |     | upsert with |
  |           |     |   text descr.  |     |           |     | text + list |     |               |     | deterministic|
  |           |     |                |     |           |     | of entities |     |               |     | IDs          |
  +-----------+     | ADVANTAGE:     |     +-----------+     +-------------+     +---------------+     +-------------+
                    | Images -> text |                                                                       |
                    | OCR built-in   |                                                                       v
                    | 100+ formats   |                                                          +---------------------+
                    | ~2-5s/doc      |                                                          | custom-kb-index     |
                    +----------------+                                                          | Keyword + Vector    |
                                                                                                | + Semantic Reranking|
                                                                                                | Hybrid Search       |
                                                                                                +---------------------+
                                                                                                          |
                                                                                                          v
                                                                                                +---------------------+
                                                                                                | LangGraph RAG Agent |
                                                                                                +---------------------+
```

### Data Sources Feeding ADLS

```
+-----------------+     Logic App v1 (5-min poll)     +---------------------+
| SharePoint      |  --------------------------------> | ADLS Gen2           |
| ~10,000 docs    |     Copies raw binary +            | <adls-account>     |
| PDF,DOCX,XLSX,  |     metadata.json sidecar          |                     |
| PPTX            |                                    | /raw-documents/     |
+-----------------+                                    |   sharepoint/       |
                                                       |     {site}/{lib}/   |  Event Grid
+-----------------+     Timer (15-min wiki sync)       |   wiki/             |  (BlobCreated)
| ADO Wiki        |  --------------------------------> |     {org}/{proj}/   | ----> Queue ----> Active
| ~10,000 pages   |     Reads from <wiki-storage>     |                     |  (doc-processing  Function App
                                                       |                     |   -queue)         (queue trigger)
| Markdown (.md)  |     devops-wiki-store container     | /raw-documents-     |
+-----------------+     (read-only cross-account)       |   failed/           |
                                                       | /processing-state/  |
                                                       +---------------------+
```

### Why This Architecture

| Decision | Choice | Why |
|----------|--------|-----|
| **ADLS Gen2** over Blob Storage | Hierarchical namespace (HNS) | Atomic directory ops, POSIX ACLs for per-team access, same cost. See [Q&A](#q17-why-adls-gen2-instead-of-standard-blob-storage). |
| **Push-based indexing** over pull-based (indexers) | SDK `merge_or_upload` | Custom PII, custom chunking, full pipeline control, no preview dependencies. See [Q&A](#q18-why-a-code-based-pipeline-instead-of-portal-configured-indexers--skillsets). |
| **Event Grid -> Queue** routing (production default) | `TRIGGER_MODE=EVENTGRID_QUEUE`: Event Grid -> `doc-processing-queue` -> Function App | Queue provides per-message retry (5 attempts), poison queue for permanent failures, batch dequeue for bulk load, concurrency throttling for API rate limits. Switchable to `EVENTGRID_DIRECT` or `BLOB` (simplest, no Event Grid/Queue needed). See [Section 6](#6-event-routing-event-grid-vs-queue-vs-blob). |
| **Foundry endpoint** over standalone AI resources | Single multi-service resource | No need to provision standalone AI resources. Foundry (`<foundry-account>`) already approved. See [Section 4](#4-azure-ai-foundry-services). |
| **`text-embedding-3-large`** over `3-small` | Higher retrieval quality | +2.3% MTEB quality. Negligible cost difference at our scale ($2 vs $0.30 total). See [Section 9](#9-embedding-model-selection). |
| **AI Foundry path** as production primary | Image verbalization, OCR, universal parser, higher PII accuracy | Captures visual content, handles scanned docs, 100+ formats natively. Cost-optimized Custom path available for bulk loads. See [../CUSTOM_README.md](../CUSTOM_README.md). |

### Document Lifecycle (Single File, End-to-End)

```
[1] User uploads PDF to SharePoint
     |
     |  WHY SharePoint as source: It is your organization's existing document management system.
     |  All policies, procedures, and operational docs live there.
     |
[2] Logic App detects change (5-min sliding window poll)
     |
     |  WHY 5-min poll: SharePoint connector does not support webhooks for
     |  document library changes. Sliding window with overlap ensures no missed files.
     |  The -6 min lookback (for a 5-min poll) provides 1-min overlap for safety.
     |
[3] Logic App copies raw binary + metadata.json sidecar to ADLS Gen2
     |  Path: <adls-account>/raw-documents/sharepoint/{site}/{library}/{file}.pdf
     |
     |  WHY raw binary copy with no parsing: The Logic App is a thin transport layer.
     |  All intelligence lives in the Function App. This separation means:
     |  - Logic App never needs updating when parsing logic changes
     |  - Raw files preserved for reprocessing if pipeline logic improves
     |  - Metadata sidecar carries source context (URL, ETag, timestamps)
     |
[4] ADLS fires BlobCreated event -> Event Grid -> doc-processing-queue -> Function App
     |  (TRIGGER_MODE=EVENTGRID_QUEUE: Event Grid -> Queue -> Function App, 0-30s latency)
     |  (TRIGGER_MODE=EVENTGRID_DIRECT: Event Grid -> Function App direct, sub-second latency)
     |  (TRIGGER_MODE=BLOB: Blob trigger polls storage directly, no Event Grid/Queue needed)
     |
     |  WHY Queue as production default: Per-message retry, poison queue for permanent
     |  failures, batch dequeue for initial load, concurrency throttling for API rate
     |  limits. See Section 6 for Event Grid vs Queue vs Blob comparison.
     |
[5] Function App executes 6-stage pipeline:
     |
     |  [Stage 1] READ    -- Download raw bytes from ADLS via azure-storage-blob SDK
     |                       WHY SDK over REST: Managed Identity auth built-in,
     |                       retry policies, streaming for large files
     |
     |  [Stage 2] PARSE   -- Content Understanding (prebuilt-documentSearch) extracts
     |                       text, tables, figures, and image descriptions in one call.
     |                       Returns structured markdown optimized for RAG.
     |                       WHY prebuilt-documentSearch: Layout preservation, table
     |                       detection, figure analysis, OCR for scanned pages — all
     |                       in a single API call (replaces Doc Intelligence + GPT-4o).
     |
     |  [Stage 3] CHUNK   -- Split into 1024-token segments (200-token overlap)
     |                       WHY 1024 tokens: Optimal for embedding quality (research
     |                       shows 512-1024 is sweet spot). WHY 200 overlap: prevents
     |                       context loss at chunk boundaries (~20% of chunk)
     |
     |  [Stage 4] PII     -- Azure Language PII detects and redacts SSN, names, cards,
     |                       emails, phones, addresses, DOB, and 50+ entity types.
     |                       WHY before embedding: Foundry LLM NEVER sees raw PII.
     |                       PII never enters the search index. Original file in ADLS
     |                       is preserved untouched for authorized access.
     |
     |  [Stage 5] EMBED   -- Generate 3072-dim vectors via Foundry text-embedding-3-large
     |                       WHY 3-large over 3-small: 2.3% higher MTEB quality.
     |                       For a production knowledge base serving end users,
     |                       retrieval accuracy directly impacts answer quality.
     |                       See Section 9 for full comparison.
     |
     |  [Stage 6] INDEX   -- Upsert chunks to AI Search (merge_or_upload, batch=100)
     |                       WHY merge_or_upload: Idempotent. Reprocessing a document
     |                       replaces its chunks (deterministic IDs) without duplication.
     |                       WHY batch=100: AI Search SDK limit per request.
     |
[6] Document is now searchable via hybrid search (keyword + vector + semantic reranking)
     |
[7] RAG Agent queries custom-kb-index -> returns answer with source citations
```

---

## 2. Processing Pipeline: 6 Stages

Every document goes through exactly 6 stages:

| Stage | Purpose | AI Foundry Implementation | Why This Stage Exists |
|-------|---------|--------------------------|----------------------|
| **1. Read** | Download raw bytes from ADLS | `azure-storage-blob` SDK, Managed Identity auth | Decouple landing zone from processing. Raw binary preserved for reprocessing. |
| **2. Parse** | Extract text, tables, images from binary | Azure AI Content Understanding (`prebuilt-documentSearch`) — structured markdown with tables, figures, and image descriptions in one call | Convert binary format to searchable text. Images become text descriptions. Scanned PDFs are OCR'd. 100+ formats handled natively. |
| **3. Chunk** | Split into token-sized segments | tiktoken `cl100k_base`, 1024 tokens, 200 overlap | Embedding models have a context window limit. Search quality requires right-sized chunks. |
| **4. PII** | Detect and redact sensitive data | Azure AI Language PII via Foundry endpoint. 50+ entity types. Returns pre-redacted text. | Requirement: PII must never enter the embedding model or search index. |
| **5. Embed** | Generate vector representations | Foundry `text-embedding-3-large` (3072d), batch of 16, exponential backoff | Enable semantic search -- find documents by meaning, not just keywords. |
| **6. Index** | Upsert to search index | `azure-search-documents` SDK, `merge_or_upload`, batch=100, deterministic IDs | Idempotent push-based indexing. Reprocessing replaces, not duplicates. See [Section 3: Indexing Strategy](#indexing-strategy). |

### Why is the chunker shared and not replaced by Content Understanding output?

**Q: Content Understanding returns structured markdown with paragraphs and tables already included. Why re-chunk it?**

**A:** Three reasons:

1. **Embedding model context window.** `text-embedding-3-large` has an 8,191-token context window. A single Content Understanding output for a 50-page PDF can be 50,000+ tokens -- far exceeding the limit. Re-chunking to 1,024 tokens ensures every chunk fits within the embedding window.

2. **Search precision.** Retrieving a 1,024-token chunk is more precise than retrieving an entire 50,000-token document. The RAG agent gets focused, relevant passages -- not entire documents with mostly irrelevant content. Research consistently shows 512-1,024 tokens is the optimal chunk size for embedding-based retrieval.

3. **Index consistency.** All documents in `custom-kb-index` use the same chunk size regardless of processing path, ensuring uniform vector comparison and consistent search relevance scoring.

### Parser (AI Foundry Path)

One unified API call handles ALL file types: `ContentUnderstandingClient.begin_analyze_binary(analyzer_id="prebuilt-documentSearch", binary_input=file_bytes)`.

**Why `prebuilt-documentSearch` over `prebuilt-read` or `prebuilt-layout`?**
- `prebuilt-read` / `prebuilt-layout` are content extraction analyzers (text/structure only)
- `prebuilt-documentSearch` is the RAG-optimized analyzer: layout preservation, table detection, figure analysis, and structured markdown output — all in a single call
- For a knowledge base, table structure, figure descriptions, and RAG-ready markdown are essential

Returns structured markdown from `result.contents[0].markdown` which includes text, tables, and figure descriptions. Image verbalization (describing diagrams, charts, etc.) is handled natively by Content Understanding — no separate GPT-4o Vision call is needed.

### Image Verbalization (Content Understanding)

Content Understanding's `prebuilt-documentSearch` analyzer handles image verbalization natively:
1. Detects figures, diagrams, and charts within the document
2. Uses the Foundry-deployed GPT model (gpt-4.1-mini) to analyze and describe each figure
3. Includes the descriptions in the structured markdown output

This replaces the previous two-step flow (Document Intelligence + separate GPT-4o Vision call) with a single API call.

This makes visual content searchable -- a user searching for "API gateway architecture" can find a diagram that shows it, even if those words never appear in the surrounding text.

**What gets verbalized:** Architecture diagrams, flowcharts, UI screenshots, org charts, data visualizations, training slide visuals -- all become searchable natural language text.

**Real-world impact :** Architecture review documents, infrastructure diagrams, process flowcharts, training slide decks -- critical information encoded ONLY in visual format becomes fully searchable.

### PII Detection (Azure AI Language)

Azure AI Language PII detects 50+ entity types (SSN, credit cards, names, addresses, phone numbers, dates of birth, medical records, bank accounts) and returns pre-redacted text directly -- no manual redaction logic needed.

```
Original text in chunk:
  "John Smith (SSN: 123-45-6789) approved the $50,000 loan for jane@example.org
   at the 123 Main St, Arlington VA office. His card ending 4242-4242-4242-4242."
                     |
PII scan detects:
  PERSON ("John Smith")           -> confidence: 0.85
  US_SSN ("123-45-6789")          -> confidence: 0.99
  EMAIL  ("jane@example.org")        -> confidence: 0.99
  LOCATION ("123 Main St...")     -> confidence: 0.78
  CREDIT_CARD ("4242-4242...")    -> confidence: 0.99
                     |
Redacted text stored in chunk:
  "[NAME REDACTED] (SSN: [SSN REDACTED]) approved the $50,000 loan for
   [EMAIL REDACTED] at the [LOCATION REDACTED] office. His card ending
   [CARD REDACTED]."
                     |
Embedding generated from REDACTED text:
  -> Foundry LLM NEVER sees raw PII (receives only redacted text)
  -> Vector captures semantic meaning of "loan approval" without PII
                     |
AI Search index stores:
  -> chunk_content: redacted text (searchable, retrievable)
  -> content_vector: 3072-dim vector (searchable, retrievable)
  -> pii_redacted: true (filterable flag)
                     |
Original file in ADLS:
  -> Untouched. Raw PII preserved for authorized access by compliance team.
  -> ADLS ACLs can restrict who can read raw files vs search index.
```

---

## 3. Chunking Strategy & Indexing Strategy

### Chunking Strategy

> **One-liner:** We use fixed-size token chunking (1024 tokens, 200 overlap) with tiktoken's `cl100k_base` tokenizer because it matches the embedding model's tokenizer exactly, guarantees uniform chunk sizes for consistent vector comparison, and keeps chunks within the optimal 512-1024 token range for embedding-based retrieval.

#### How Chunking Works

```
Original document (parsed text):
+-----------------------------------------------------------------------------------+
| "The remote access policy requires all employees to use the corporate VPN when    |
| accessing internal systems from outside the corporate network. Multi-factor            |
| authentication (MFA) is mandatory for all VPN connections. The VPN gateway is     |
| hosted on the Azure AKS cluster in East US. Session timeout is set to 8 hours..." |
| ... [continues for 50,000 tokens across 15 pages] ...                             |
+-----------------------------------------------------------------------------------+
       |
       |  tiktoken cl100k_base encoder
       |  chunk_size=1024, chunk_overlap=200
       v
+-------------------+  +-------------------+  +-------------------+      +-------------------+
| Chunk 0           |  | Chunk 1           |  | Chunk 2           |      | Chunk N           |
| Tokens 0-1023     |  | Tokens 824-1847   |  | Tokens 1648-2671  | .... | Tokens X-(X+1023) |
|                   |  |                   |  |                   |      |                   |
| "The remote       |  | "...mandatory for |  | "...Session       |      | [last 1024 tokens |
|  access policy    |  |  all VPN connect- |  |  timeout is set   |      |  of document]     |
|  requires all     |  |  ions. The VPN    |  |  to 8 hours..."   |      |                   |
|  employees..."    |  |  gateway is..."   |  |                   |      |                   |
+-------------------+  +-------------------+  +-------------------+      +-------------------+
         |                      |                      |                          |
         | <--- 200 token ----> |                      |                          |
         |      overlap         | <--- 200 token ----> |                          |
         |                      |      overlap         |                          |
         v                      v                      v                          v
   [PII redact]          [PII redact]          [PII redact]                [PII redact]
         |                      |                      |                          |
         v                      v                      v                          v
   [Embed: 3072d]        [Embed: 3072d]        [Embed: 3072d]             [Embed: 3072d]
         |                      |                      |                          |
         v                      v                      v                          v
   [Push to AI Search]   [Push to AI Search]   [Push to AI Search]        [Push to AI Search]
```

#### Why These Specific Parameters

| Parameter | Value | Why This Value | What Happens If Too Low | What Happens If Too High |
|-----------|-------|---------------|------------------------|-------------------------|
| **Chunk size** | 1024 tokens | Optimal range for embedding-based retrieval (research: 512-1024). Large enough to hold a complete thought. Small enough for precise retrieval. | <256 tokens: Chunks are sentence fragments -- not enough context for the embedding to capture meaning. Retrieval returns fragments. | >2048 tokens: Chunks contain too many topics. Retrieval returns large blocks where most content is irrelevant to the query. |
| **Overlap** | 200 tokens (~20%) | Captures context at chunk boundaries. A sentence split across two chunks appears in both, so neither chunk loses the thought. | 0 overlap: Sentences at boundaries are cut in half. Searching for a concept that happens to straddle a boundary misses both chunks. | >30% overlap: Excessive duplication. 50% more chunks than needed, 50% more embedding cost, 50% more storage. |
| **Tokenizer** | `cl100k_base` (tiktoken) | This is the exact tokenizer used by `text-embedding-3-large`. Using the same tokenizer guarantees our 1024-token chunks are exactly 1024 tokens in the embedding model's view -- no truncation, no wasted capacity. | Using a different tokenizer (e.g., word-based split): Token counts don't match. A "1024 word" chunk might be 1,500 tokens, causing truncation in the embedding model. | N/A |

#### Chunking Strategy Alternatives Considered

| Strategy | How It Works | Why Rejected |
|----------|-------------|--------------|
| **Sentence-based** | Split on sentence boundaries | Sentences vary wildly in length (5-500 tokens). Inconsistent chunk sizes degrade embedding quality. |
| **Paragraph-based** | Split on paragraph boundaries | Documents have inconsistent paragraph sizes. A 5-page single-paragraph policy doc produces one huge chunk. |
| **Semantic chunking** | Use an LLM to detect topic boundaries | Requires an API call per document BEFORE the main pipeline. Adds latency and cost. Quality varies. |
| **Recursive character split** | Split on `\n\n`, then `\n`, then `. `, then ` ` | Character-based, not token-based. Doesn't align with embedding model's tokenizer. Chunk sizes are unpredictable in token space. |
| **Fixed-size token (chosen)** | Split at exactly N tokens with M overlap | Predictable, consistent, aligns with tokenizer. Simple to reason about. Well-supported by research. |

### Indexing Strategy

> **One-liner:** We use push-based indexing via the AI Search SDK (`merge_or_upload` in batches of 100) with deterministic document IDs because it gives us full control over what enters the index, guarantees idempotent re-processing, and avoids dependency on Azure's pull-based indexer/skillset pipeline which cannot support custom PII redaction or local parsing.

#### How Indexing Works

```
After chunking, PII redaction, and embedding, each chunk becomes an index document:

Chunk 3 of "remote-access-policy.pdf"
  |
  |  Build index document:
  v
  {
    "id":              "c2hhcmVwb2ludC9pdC1wb2xpY3kvZG9jcy9yZW1vdGUtYWNjZXNzLnBkZl8z"
                        ^-- base64(file_path + "_" + chunk_index) -- deterministic, idempotent
    "chunk_content":   "[NAME REDACTED] approved the remote access policy requiring VPN..."
                        ^-- PII-redacted text (searchable via keyword)
    "content_vector":  [0.0123, -0.0456, 0.0789, ... (3072 floats)]
                        ^-- 3072-dim embedding of the redacted text (searchable via vector)
    "document_title":  "remote-access-policy.pdf"
    "source_type":     "sharepoint"
    "source_url":      "https://contoso.sharepoint.com/sites/it-policy/docs/remote-access.pdf"
    "file_name":       "remote-access-policy.pdf"
    "chunk_index":     3
    "total_chunks":    12
    "page_number":     4
    "last_modified":   "2026-02-15T10:30:00Z"
    "ingested_at":     "2026-02-23T14:22:00Z"
    "pii_redacted":    true
  }
  |
  |  Collected into batch (up to 100 documents)
  v
  SearchClient.merge_or_upload_documents(batch)
  |
  |  merge_or_upload behavior:
  |  - If document ID exists -> UPDATE (merge fields)
  |  - If document ID is new -> INSERT
  |  - Result: reprocessing the same file replaces old chunks, never creates duplicates
  v
  custom-kb-index (Azure AI Search)
```

#### Why Push-Based Instead of Pull-Based (Indexers)?

| Factor | Pull-Based: Indexers + Skillsets (Rejected) | Push-Based: SDK merge_or_upload (Chosen) | Why Push Wins |
|--------|-------------------------------------------|----------------------------------------|---------------|
| **PII redaction** | Cannot use Presidio. Skillsets only support built-in skills or Custom Web API. | Run Azure Language PII directly in pipeline code. No extra infrastructure. | Custom PII with zero extra infra. |
| **Chunking** | Built-in text split skill has limited control. No tiktoken, no token-aware splitting. | Any chunking strategy -- tiktoken for precise token counting, adjustable overlap. | Precise token-aligned chunks. |
| **Image verbalization** | Only via Custom Web API skill (host a web service). | Content Understanding handles figure analysis natively in the parse call. | No extra web service or separate vision call. |
| **Idempotency** | Indexer may re-index unchanged documents. Duplicate control depends on key generation. | Deterministic IDs: `base64(file_path + chunk_index)`. `merge_or_upload` is inherently idempotent. | Guaranteed no duplicates ever. |
| **Error handling** | Indexer gives "failed" with limited context. Retry timing is indexer-controlled. | Full error handling per document, per stage. Move failed docs to dead letter with error sidecar. | Full observability and control. |
| **Preview features** | Some skillsets (document cracking for certain formats) are in preview. Preview features are not recommended for production. | All SDKs are GA. No preview dependencies. | Production-safe. |

#### How the Index Supports Search

```
User query: "What is our remote access policy?"
                    |
         +----------+----------+
         |                     |
    Keyword Search        Vector Search
    (BM25 scoring)        (HNSW cosine)
         |                     |
    Matches "remote",     Matches semantic
    "access", "policy"    meaning even without
    exact keywords        exact keywords -- e.g.,
                          finds "VPN guidelines"
         |                     |
         +----------+----------+
                    |
             Hybrid Scoring
          (RRF: Reciprocal Rank
           Fusion combines both)
                    |
          Semantic Reranking
        (cross-encoder re-scores
         top results for precision)
                    |
              Top K results
         (chunks with text +
          source metadata)
                    |
           RAG Agent synthesizes
           answer with citations
```

| Search Mode | How It Works | What It Finds | Config |
|-------------|-------------|---------------|--------|
| **Keyword (BM25)** | Traditional full-text search with `en.microsoft` analyzer (stemming, lemmatization, stop words) | Exact and stemmed keyword matches. "remote access" finds "remotely accessing". | `chunk_content` field, `en.microsoft` analyzer |
| **Vector (HNSW)** | Approximate nearest neighbor search on 3072-dim vectors using cosine similarity | Semantically similar content. "remote access" finds "VPN from home" and "telework guidelines". | `content_vector` field, HNSW(m=4, efConstruction=400, efSearch=500) |
| **Hybrid** | Combines keyword and vector scores using Reciprocal Rank Fusion (RRF) | Best of both: exact matches AND semantic matches. | Automatic when both keyword and vector queries are provided |
| **Semantic reranking** | Cross-encoder model re-scores the top hybrid results using deeper language understanding | Improves precision by ~5-10% -- promotes truly relevant results, demotes false positives. | `custom-kb-semantic-config` on `chunk_content` |

---

## 4. Azure AI Foundry Services

All AI services are accessed through a single Azure AI Foundry endpoint: `<foundry-account>.cognitiveservices.azure.com`. This is a multi-service Cognitive Services resource that hosts multiple AI capabilities under one endpoint.

### Why Foundry Instead of Standalone Azure AI Resources?

| Factor | Standalone Resources (Rejected) | Foundry Endpoint (Chosen) | Why |
|--------|-------------------------------|--------------------------|-----|
| **Provisioning** | Each service needs its own resource (Content Understanding, Language, OpenAI) | Single resource, multiple capabilities | Foundry access is already available — no need to provision separate AI resources. |
| **Authentication** | Separate endpoints, separate keys, separate Managed Identity roles | One endpoint, one Managed Identity role (`Cognitive Services User`) | Simpler RBAC. One role grants access to Content Understanding, embeddings, Language PII, and GPT models. |
| **Billing** | Separate cost centers per service | Consolidated billing under one resource | Easier cost tracking and forecasting. |
| **Endpoint management** | 3-4 separate endpoints in app settings | One `FOUNDRY_ENDPOINT` env var | Fewer configuration points = fewer misconfigurations. |
| **Availability** | Would need to request, justify, and wait for each resource | Already deployed and available (`<foundry-account>`) | Zero provisioning lead time. |

### Service Definitions

| Service | What It Is | How This Pipeline Uses It | SDK | When Used |
|---------|-----------|--------------------------|-----|-----------|
| **Azure AI Content Understanding** | A multimodal AI service that extracts semantic content from documents. It goes beyond OCR -- it understands document structure, extracts text, tables, figures, and generates structured markdown optimized for RAG. Supports PDF, DOCX, XLSX, PPTX, images, and 100+ other formats. Image verbalization (describing figures/diagrams) is handled natively in the same API call. | **Stage 2 (Parse).** The `prebuilt-documentSearch` analyzer processes the full document. Returns `result.contents[0].markdown` (structured markdown with tables and figure descriptions included). Single API call replaces all per-format custom parsers AND the separate GPT-4o Vision image verbalization step. | `azure-ai-contentunderstanding` -> `ContentUnderstandingClient` | Stage 2 |
| **Azure AI Language (PII Detection)** | A natural language processing service that identifies and categorizes personally identifiable information in text. It detects 50+ entity types and returns both entity positions and a pre-redacted version of the text. Trained on a larger corpus than open-source NER models, with higher accuracy for financial and medical PII. | **Stage 4 (PII).** `TextAnalyticsClient.recognize_pii_entities()` scans each text chunk. Returns `redacted_text` directly (no manual redaction logic needed) plus entity positions and categories. Batches up to 5 documents per API call for throughput. | `azure-ai-textanalytics` -> `TextAnalyticsClient` | Stage 4 |
| **Azure OpenAI Embeddings (`text-embedding-3-large`)** | A text embedding model that converts text strings into dense numerical vectors (3072 dimensions). These vectors capture semantic meaning -- similar texts produce similar vectors, enabling semantic search even when exact keywords don't match. `text-embedding-3-large` scores 64.6% on the MTEB benchmark. | **Stage 5 (Embed).** Generates 3072-dimensional vectors for each chunk. Batches of 16 texts per API call. Exponential backoff with jitter for rate limit handling. | `openai` -> `AzureOpenAI` (embeddings endpoint) | Stage 5 |

### What is Azure AI Foundry?

Azure AI Foundry (formerly Azure AI Studio) is Microsoft's unified platform for building AI applications. It consolidates access to Azure OpenAI models, Content Understanding, Language services, Content Safety, and other AI capabilities under a single resource and endpoint.

For this pipeline, the Foundry resource `<foundry-account>` exposes:
- **Embedding deployment:** `text-embedding-3-large` (3072 dimensions, production model)
- **Content Understanding:** `prebuilt-documentSearch` analyzer (parsing, tables, figures, image verbalization)
- **Model deployments required:** `gpt-4.1-mini` + `text-embedding-3-large` (used by Content Understanding analyzers)
- **Language PII:** Text analytics PII entity recognition

All authenticated via Managed Identity (`DefaultAzureCredential`) -- no API keys in code.

---

## 5. AI Foundry Processing Path

### What It Does

Processes documents using Azure AI services accessed via the Foundry endpoint. Content Understanding parses all file types with a single API call — extracting text, tables, figures, and image descriptions as structured markdown optimized for RAG. Azure Language PII detects and redacts sensitive data with high accuracy across 50+ entity types.

### Why This Is the Production Primary Path

| Reason | Detail | Why It Matters for Production |
|--------|--------|------------------------------|
| **Image verbalization** | PPT slides with architecture diagrams, PDF pages with flowcharts, screenshots of UIs, org charts -- all become searchable natural language text via Content Understanding's built-in figure analysis. | Organizations have architecture decks and training materials with critical information encoded ONLY in diagrams. Without image verbalization, that information is invisible to search. |
| **Universal parser** | One API call handles PDF, DOCX, XLSX, PPTX, images, scanned documents, and 100+ other formats. | Eliminates per-format parser maintenance. When you add `.msg` (Outlook) or `.html` files, no code changes needed. |
| **Built-in OCR** | Scanned PDFs and image-only pages are automatically OCR'd by Content Understanding. Zero additional configuration. | Legacy policies scanned as image PDFs cannot be processed without OCR. The Foundry path makes them searchable. |
| **Superior table extraction** | Content Understanding preserves table structure with row/column relationships, cell spans, and headers in the markdown output. | Financial institution documents often contain complex tables (rate sheets, compliance matrices, audit findings). Accurate table extraction directly impacts search relevance. |
| **Higher PII accuracy** | Azure Language PII is trained on a significantly larger corpus. Detects 50+ entity types with higher confidence, especially for person names, addresses, and financial data. | For a financial institution, PII detection accuracy is a compliance requirement. Higher accuracy means fewer PII leaks into the search index. |
| **Reduced code maintenance** | Stages 2 and 4 are API calls, not custom code. API improvements (better OCR, more PII entity types) come automatically. | Custom parsers require ongoing maintenance as file formats evolve and edge cases are discovered. Azure AI services are continuously improved by Microsoft. |

### Limitations -- With Honest Assessment

| Limitation | Impact | Severity | Mitigation |
|-----------|--------|----------|------------|
| **API latency per document** | Content Understanding: 3-8s per document (single call handles parsing + figure analysis). | **Medium** | Acceptable for event-driven processing (documents arrive one at a time). Becomes a bottleneck only during initial bulk load. |
| **API cost per transaction** | Content Understanding pricing is comparable to Document Intelligence. Azure Language PII: ~$0.001/1000 chars. | **High for bulk loads** | Use Custom path for initial full load (free parsing, see [../CUSTOM_README.md](../CUSTOM_README.md)). Switch to Foundry for daily deltas where volume is low and image verbalization adds value. |
| **Rate limits** | Content Understanding rate limits depend on Foundry resource tier. Becomes the bottleneck during high-volume processing. | **High for bulk loads** | Built-in retry with exponential backoff. Queue intermediary (Section 6) provides concurrency throttling. |
| **Network dependency** | Every Stage 2 and Stage 4 call goes to Foundry endpoint. Network outage or Foundry throttling halts parsing and PII. | **Medium** | If Content Understanding fails for a specific document, the Foundry path falls back to custom parsers (PyMuPDF etc. are included for wiki `.md` file support and error recovery). PII scan failure results in text passing through unredacted with a warning log -- this is monitored. |

### Dependency Stack

```
# Foundry path -- NO presidio, NO spacy (saves ~100 MB)
azure-ai-contentunderstanding>=1.0.0b1  # Content Understanding (universal parsing + figure analysis)
azure-ai-textanalytics>=5.3.0         # Azure Language PII detection
PyMuPDF>=1.24.0                       # Fallback parser for wiki .md and error recovery
python-docx>=1.1.0                    # Fallback parser
openpyxl>=3.1.0                       # Fallback parser
python-pptx>=0.6.23                   # Fallback parser
```

**Total deploy size:** 68 MB (no spaCy model)

**Why keep PyMuPDF/python-docx in the Foundry path?** Wiki `.md` files are plain text -- sending them to Content Understanding is wasteful (API cost for a file that's already text). The fallback parsers handle wiki sync and provide error recovery if Content Understanding fails for a specific document.

---

## 6. Event Routing: Event Grid vs Queue vs Blob (Decision Matrix)

### Deployed Architecture: Triple Trigger Mode

All three trigger paths are deployed and operational. The `TRIGGER_MODE` env var controls which path is active:

```
TRIGGER_MODE=EVENTGRID_QUEUE (production default):
  ADLS BlobCreated -> Event Grid -> doc-processing-queue -> Function App (queue trigger)

TRIGGER_MODE=EVENTGRID_DIRECT (lower latency):
  ADLS BlobCreated -> Event Grid -> Function App (direct invoke)

TRIGGER_MODE=BLOB (simplest, fewest moving parts):
  ADLS BlobCreated -> Function App (blob trigger polls storage directly)
```

The queue (`doc-processing-queue`) lives on the same `<adls-account>` storage account as the ADLS containers. The Function App has all three trigger functions registered -- the inactive triggers are disabled via `AzureWebJobs.<function_name>.Disabled` app settings. Switching between modes is a re-run of `deploy.sh` with the desired `TRIGGER_MODE` value.

### Comprehensive Comparison: Event Grid vs Queue vs Blob vs Service Bus

| Factor | Event Grid Direct (`EVENTGRID_DIRECT`) | Event Grid + Queue (`EVENTGRID_QUEUE`) | Blob Trigger (`BLOB`) | Azure Service Bus (Not Used) | Why It Matters |
|--------|---------------------------|----------------------------------|-----------------------------------|------------------------------|----------------|
| **Trigger latency** | Sub-second (immediate) | 0-30 seconds (queue polling interval) | Up to 10 min on Consumption; ~1s on Flex/Premium with Event Grid-based extension | 0-30 seconds | Event Grid is fastest. Blob trigger latency depends on hosting plan. |
| **Retry control** | Event Grid retries delivery for 24 hours with exponential backoff. No per-message retry count. | **Configurable per message.** Dequeue count tracks retries per message. After N failures (e.g., 5), message moves to poison queue. | Limited. `poisonBlobThreshold` in host.json (default: 3). Failed blobs are logged but not moved to a poison queue. Pipeline's `move_to_failed` + timer retry covers this. | Same as Queue, plus dead-letter sub-queue with metadata. | **Critical for production.** Queue provides the best retry semantics. Blob trigger relies on the pipeline's own dead-letter logic. |
| **Dead letter** | Event Grid has a dead-letter destination (blob container), but it captures the EVENT, not the processing failure context. | **Poison queue** automatically captures messages that fail N times. Easy to inspect, reprocess, or alert on. | No built-in dead letter. The pipeline's `move_to_failed` container + `reprocess_failed` timer provides equivalent behavior. | Dead-letter sub-queue with failure reason, exception info. | Queue is best here. Blob trigger depends on application-level dead-lettering (already built). |
| **Batch processing** | One event -> one function invocation. No batching. 10,000 events = 10,000 separate invocations. | **Batch dequeue.** Configure `batchSize=16` to process 16 documents per invocation. Reduces cold starts, improves throughput. | One blob -> one function invocation. No batching. | Same batching capability. | Queue is best for initial bulk load. |
| **Visibility timeout** | No concept. Event is delivered and either succeeds or fails. | **Configurable.** If a function takes the message but crashes before completing, message becomes visible again after timeout (e.g., 10 min). Another instance picks it up. | No equivalent concept. Blob receipts tracked internally. | Same mechanism. | Queue and Service Bus protect against mid-processing crashes. |
| **Concurrency control** | No throttling. Event Grid delivers as fast as events arrive. Function auto-scales to match. | **`maxConcurrentCalls` setting.** Limit to N concurrent processing threads. Prevents overwhelming downstream APIs (Foundry, AI Search). | **`maxDegreeOfParallelism` in host.json.** Configured to `1` (serial processing). Tunable for higher throughput. | Same concurrency control. | All three modes support concurrency control. |
| **Infrastructure required** | Event Grid system topic + subscription | Event Grid system topic + subscription + Queue | **None** -- blob trigger polls storage directly. No Event Grid or Queue needed. | Separate Service Bus namespace | **Blob is simplest.** Ideal for dev/POC or minimal-infra environments. |
| **Cost** | $0.60 per million events | $0.004 per 10,000 operations | **$0** -- no additional cost beyond Function App compute. | $0.05 per million operations (Basic tier) | Blob trigger has zero infrastructure cost. |
| **Duplicate processing** | Event Grid guarantees at-least-once. Rare duplicates possible. | Queue guarantees at-least-once. Rare duplicates possible. | Blob trigger can fire multiple times for the same blob in edge cases. | At-least-once or exactly-once (sessions). | All modes mitigated by pipeline's idempotent `merge_or_upload_documents()`. |

### Why Queue Storage over Service Bus?

| Factor | Azure Queue Storage | Azure Service Bus |
|--------|-------------------|-------------------|
| **Purpose** | Simple, reliable message queuing | Enterprise messaging with sessions, topics, transactions |
| **Cost** | ~$0.004/10K operations | ~$0.05/million (Basic), ~$10/month (Standard) |
| **Complexity** | Low -- just a queue on existing storage account | Medium -- requires separate namespace resource |
| **What we need** | Retry control, batch dequeue, concurrency throttle | We don't need sessions, topics, or transactions |

**Verdict:** Azure Queue Storage provides everything this pipeline needs without the complexity or cost of Service Bus.

### Switching Between Trigger Modes

```bash
# From repo root — switch to queue mode (production)
TRIGGER_MODE=EVENTGRID_QUEUE ./deploy.sh

# From repo root — switch to direct Event Grid mode
TRIGGER_MODE=EVENTGRID_DIRECT ./deploy.sh

# From repo root — switch to blob trigger mode (simplest, no Event Grid/Queue needed)
TRIGGER_MODE=BLOB ./deploy.sh
```

**What changes:** The `deploy.sh` script toggles `AzureWebJobs.<function_name>.Disabled` app settings to enable only the active trigger and disable the others. For QUEUE and EVENT_GRID modes, the Event Grid subscription endpoint type also switches. For BLOB mode, no Event Grid subscription or Queue is needed -- the blob trigger polls storage directly. No pipeline code changes -- only the trigger mechanism changes.

### Queue Configuration (host.json)

```json
"queues": {
  "batchSize": 1,
  "maxDequeueCount": 5,
  "visibilityTimeout": "00:10:00"
}
```

| Setting | Value | Why |
|---------|-------|-----|
| `batchSize` | `1` | Process one message at a time (safe default). Tune to `16` for initial bulk load to reduce cold starts. |
| `maxDequeueCount` | `5` | After 5 failed processing attempts, message moves to `doc-processing-queue-poison`. Prevents a corrupt document from retrying infinitely. |
| `visibilityTimeout` | `00:10:00` | Matches the `functionTimeout` (10 min). If a function crashes mid-processing, the message becomes visible again after 10 minutes for another instance to pick up. |

**Connection:** Identity-based authentication via `ADLS_QUEUE_CONNECTION__queueServiceUri` (Managed Identity -- no storage account keys). The Function App's system-assigned identity has the `Storage Queue Data Contributor` role on `<adls-account>`.

### Blob Trigger Configuration (host.json)

```json
"blobs": {
  "maxDegreeOfParallelism": 1,
  "poisonBlobThreshold": 3
}
```

| Setting | Value | Why |
|---------|-------|-----|
| `maxDegreeOfParallelism` | `1` | Process one blob at a time (safe default). Tune higher for parallel processing during bulk loads. Prevents overwhelming Foundry rate limits. |
| `poisonBlobThreshold` | `3` | After 3 failed processing attempts, the blob trigger stops retrying that blob. The pipeline's own `move_to_failed` + `reprocess_failed` timer provides application-level dead-lettering. |

**Connection:** Identity-based authentication via `ADLS_BLOB_CONNECTION__blobServiceUri` (Managed Identity -- no storage account keys). The Function App's system-assigned identity has the `Storage Blob Data Contributor` role on `<adls-account>`.

---

## 7. Trigger Design & Scheduling

### Six Functions Per App

| Function | Trigger | Schedule / Config | Purpose | Concurrency | Why This Design |
|----------|---------|-------------------|---------|-------------|-----------------|
| `process_new_document` | **Event Grid** (BlobCreated) | Immediate (sub-second) | Process documents via direct Event Grid invocation | Parallel -- one invocation per blob event | Active when `TRIGGER_MODE=EVENTGRID_DIRECT`. Disabled in other modes. |
| `process_queue_document` | **Queue** | Queue: `QUEUE_NAME` (default: `doc-processing-queue`) | Process documents via Event Grid -> Queue -> Function | Parallel -- configurable `maxConcurrentCalls` and `batchSize` | Active when `TRIGGER_MODE=EVENTGRID_QUEUE` (production default). Event Grid routes through a Queue for retry control, poison queue, batch dequeue, concurrency throttling. |
| `process_blob_document` | **Blob** (BlobCreated) | Polls `ADLS_CONTAINER_RAW` container | Process documents directly from blob storage changes | Configurable -- `maxDegreeOfParallelism` in host.json (default: `1`) | Active when `TRIGGER_MODE=BLOB`. Simplest mode -- no Event Grid or Queue infrastructure required. |
| `process_wiki_sync` | **Timer** | Cron: `WIKI_SYNC_SCHEDULE` (default: `0 */15 * * * *`) | Scan `<wiki-storage>/devops-wiki-store` for new/modified wiki `.md` files | Serial -- one invocation, processes all new files sequentially | Timer because we have read-only access to `<wiki-storage>` and cannot create an Event Grid topic on it. 15 min balances freshness with efficiency. |
| `reprocess_failed` | **Timer** | Cron: `REPROCESS_FAILED_SCHEDULE` (default: `0 0 * * * *`), Batch: `REPROCESS_BATCH_SIZE` (default: `10`) | Retry documents from `raw-documents-failed` container | Serial -- max `REPROCESS_BATCH_SIZE` docs per run | 60 min gives transient issues time to resolve. Batch limit prevents timeout. |
| `health_check` | **HTTP GET** `/api/health` | On-demand | Returns health status, processing path, and trigger mode | N/A | Enables monitoring, alerting, and load balancer health probes. |

> **All schedules and queue names are configurable via environment variables.** The `%VAR_NAME%` syntax in Azure Functions decorators resolves from app settings at runtime. See [Section 14](#14-environment-variables) for the full configuration reference.

### Wiki Sync: Why 15-Minute Timer Instead of Event Grid?

**Q: Why not use Event Grid for wiki files too?**

**A:** Wiki files live in `<wiki-storage>` -- a shared, existing storage account in resource group `<shared-rg>`. Creating an Event Grid system topic on this storage account requires `Microsoft.EventGrid/systemTopics/write` permission. Our Managed Identity has **Storage Blob Data Reader** only -- read-only access.

A timer trigger with watermark-based change detection is the pragmatic solution:
- Every 15 minutes, the function lists all `.md` files in `devops-wiki-store`
- Compares each file's `last_modified` timestamp against the stored watermark
- Processes only files modified since the last run
- Updates the watermark after successful processing

### Failed Document Retry: Configurable Schedule and Batch Size

Failed documents are moved to `raw-documents-failed/` with an `.error.json` sidecar containing the failure reason, timestamp, and stack trace. The retry logic:

1. List up to `REPROCESS_BATCH_SIZE` (default: **10**) failed documents (not all -- prevents timeout)
2. Re-run each through the full pipeline
3. On success: delete from failed container + delete error sidecar
4. On failure: leave in place for next retry cycle (error sidecar updated)

**Why 10?** The Consumption Plan has a 10-minute function timeout. Processing 10 documents x ~60 seconds each = ~10 minutes. Adjust `REPROCESS_BATCH_SIZE` if using a Premium plan with longer timeouts.

**Why 60 minutes?** Most failures are transient: API throttling (Foundry rate limits, 429 responses), temporary network issues, or AI Search being briefly unavailable. 60 minutes gives these issues time to resolve. Adjust `REPROCESS_FAILED_SCHEDULE` to retry more or less frequently.

### Failure Flow: 3 Layers of Protection

```
Document Upload → Event Grid → Queue → Function App
                                          |
                                    [Pipeline Stages]
                                          |
                   +----------------------+---------------------+
                   |                      |                     |
              Stage succeeds         Stage fails           Queue retry
                   |                      |                (automatic)
                   v                      v                     |
              Next stage          move_to_failed()         maxDequeueCount=5
                   |               + .error.json           (host.json)
                   v                      |                     |
              AI Search              Failed container      Poison queue
             (searchable)                 |              (dead letter)
                                          v
                                   reprocess_failed timer
                                   (REPROCESS_FAILED_SCHEDULE)
                                          |
                                   Re-runs full pipeline
                                          |
                                  Success → delete from failed
                                  Failure → stays for next cycle
```

| Layer | Mechanism | Config | When It Helps |
|-------|-----------|--------|--------------|
| **1. Queue retry** | Azure Queue automatic retry | `maxDequeueCount` in host.json (default: 5) | Transient errors (network blip, 429 throttle). Retries with `visibilityTimeout` delay. |
| **2. Move to failed** | `pipeline.py` catches stage exceptions | `ADLS_CONTAINER_FAILED` env var | Persistent errors (corrupt file, unsupported format). Prevents infinite retry loops. |
| **3. Timer reprocess** | `reprocess_failed` timer trigger | `REPROCESS_FAILED_SCHEDULE` + `REPROCESS_BATCH_SIZE` | Transient issues that resolved after time (API outage, rate limit reset). |

---

## 8. Scale & Performance Analysis

### Per-Stage Throughput (AI Foundry Path)

| Stage | Throughput | Bottleneck | Notes |
|-------|-----------|------------|-------|
| **1. Read (ADLS)** | ~200 MB/s | Network | ADLS Gen2 Standard_LRS: 60 Gbps egress. Not a bottleneck. |
| **2. Parse** | ~0.2-0.5 docs/sec (~3-8s/doc) | API rate limit | Content Understanding: single call per document (parsing + figure analysis combined). |
| **3. Chunk** | ~100 docs/sec | CPU (trivial) | In-memory text splitting. Never the bottleneck. |
| **4. PII** | ~100 chunks/sec | API | Azure Language batches efficiently. |
| **5. Embed** | ~1,200 chunks/min | **API rate limit (120K TPM)** | **This is the pipeline bottleneck.** 120K TPM / ~100 tokens per chunk = 1,200 chunks/min max. |
| **6. Push** | ~10,000 chunks/min | Network + Search indexing | Batch of 100 per request. AI Search Standard2 handles this easily. |

**Pipeline bottleneck:** Embedding (Stage 5) at 1,200 chunks/minute with `text-embedding-3-large`. Content Understanding (Stage 2) is the secondary bottleneck during bulk loads.

### Scenario 1: Initial Full Load -- 10,000 SharePoint Documents

**Assumptions:** Average document: 15 pages, ~10,000 tokens. Average chunks per document: 10. Total chunks: 100,000.

| Stage | AI Foundry Path |
|-------|----------------|
| **Parse (Stage 2)** | 10K docs x 5s = **~14 hours** (Content Understanding single-call per document) |
| **PII (Stage 4)** | 100K chunks / 100/sec = **17 min** |
| **Embed (Stage 5)** | **83 min** (120K TPM bottleneck) |
| **Push (Stage 6)** | 100K chunks / 10K per min = **10 min** |
| **Total elapsed** | **~15 hours** |
| **API cost** | Content Understanding (comparable to Doc Intelligence pricing) + ~$320 (PII) + $1.30 (embed) = **~$1,821** |

> **Cost optimization:** Use the [Custom Libraries path](../custom-processing/README.md) for the initial bulk load (~3 hours, ~$1.30 total cost). Then run a targeted Foundry reprocessing pass on image-heavy document libraries (architecture, design, training) to add image verbalization.

### Scenario 2: Initial Full Load -- 10,000 Wiki Documents

**Assumptions:** Average wiki page: ~700 tokens. Average chunks per document: 1-2. Total chunks: ~15,000. All `.md` files.

| Stage | AI Foundry Path |
|-------|----------------|
| **Parse** | 10K files x 5ms (plain text read) = **< 1 min** (uses fallback parser, not Content Understanding) |
| **PII** | 15K chunks / 100/sec = **2.5 min** |
| **Embed** | 1.5M tokens -> **12.5 min** |
| **Total** | **~20-25 min** |
| **API cost** | ~$48 (PII) + $0.20 (embed) = **~$48.40** |

> **Note:** Wiki `.md` files use the fallback parser (plain text read), not Content Understanding. Markdown is already text -- Content Understanding adds zero value for wiki pages.

### Scenario 3: Day-to-Day Delta -- 50-200 Documents/Day (Production Steady-State)

**Assumptions:** 100 SharePoint documents/day (avg 15 pages, 10 chunks each). 50 wiki pages/day (avg 1.5 chunks each). Total daily: ~1,075 chunks.

| Metric | AI Foundry Path |
|--------|----------------|
| **SP parse time** | 100 docs x 3s = **5 min** |
| **Wiki parse time** | 50 files x 5ms = **< 1 sec** (fallback parser) |
| **PII time** | 1,075 chunks / 100/sec = **11 sec** |
| **Embed time** | 107.5K tokens -> **< 1 min** |
| **Total processing/day** | **~7-10 minutes** |
| **Daily API cost** | $1.50 (Doc Intel) + $3.44 (PII) + $0.014 (embed) = **~$5** |
| **Monthly API cost** | **~$150** |
| **End-to-end per document** | Document searchable in **< 2 minutes** |

The Foundry path is recommended for daily deltas. The per-day cost (~$5) is modest, and the benefits -- image verbalization for new documents, OCR for scanned uploads, superior table extraction -- provide incrementally better search quality for every new document.

### Combined Load Profile Summary

| Scenario | Recommended Path | Total Time | Total Cost | Reasoning |
|----------|-----------------|-----------|-----------|-----------|
| **Initial SP load (10K)** | Custom (see [../custom-processing/README.md](../custom-processing/README.md)) | ~3 hours | ~$1.30 | Bulk speed, minimal cost. Accept image loss for first pass. |
| **Initial Wiki load (10K)** | Either (both use fallback parser for `.md`) | ~25 min | ~$0.20-$48 | Wiki is plain text. Content Understanding adds zero value. |
| **SP image-heavy reprocess** | Foundry | ~2-3 hours | ~$300-500 | Targeted run on architecture/design doc libraries only. |
| **Daily SP deltas (100/day)** | Foundry | ~5 min/day | ~$5/day | Image verbalization for every new document. |
| **Daily Wiki deltas (50/day)** | Either | ~1 min/day | ~$0.01-$1/day | Wiki is text. |
| **Monthly steady-state** | Foundry | ~3 hours/month | ~$100-150/month | Production primary for maximum search quality. |

### Consumption Plan Constraints

| Constraint | Value | Impact | Mitigation |
|-----------|-------|--------|------------|
| **Function timeout** | 10 minutes max | Very large documents (500+ pages) may timeout. | Error -> move to failed container -> retry at 60 min. Documents over 500 pages are rare. |
| **Memory** | 1.5 GB max | Foundry path at 68 MB fits easily. | Not a concern for the Foundry path. |
| **Max instances** | 200 (default) | During initial load with Event Grid, could spawn too many instances overwhelming Foundry rate limits. | Queue intermediary (Section 6) with concurrency control. |
| **Cold start** | 3-5 seconds (Foundry path, no spaCy) | First request after idle period is slower. | Acceptable for event-driven processing. |

---

## 9. Embedding Model Selection

### Decision: `text-embedding-3-large` (3072d) -- Production Model

| Factor | text-embedding-3-small (1536d) | text-embedding-3-large (3072d) | Decision |
|--------|-------------------------------|-------------------------------|----------|
| **Dimensions** | 1,536 | 3,072 | **3-large.** Higher dimensionality = more nuanced semantic representation. |
| **MTEB Benchmark Quality** | 62.3% | 64.6% (+2.3%) | **3-large.** 2.3% improvement in retrieval quality directly translates to better RAG answers. |
| **Storage per chunk** | 6 KB | 12 KB | 3-large uses 2x storage. For 100K chunks: 600 MB vs 1.2 GB. Both within AI Search Standard2 capacity. |
| **API cost** | $0.02/1M tokens | $0.13/1M tokens (6.5x more) | For 160K chunks initial: $0.32 vs $2.08. Monthly delta: $0.03 vs $0.19. Absolute cost difference is negligible. |
| **Latency per batch** | ~100ms | ~150ms | Negligible -- embedding is bottlenecked by TPM rate limit, not per-request latency. |
| **Context window** | 8,191 tokens | 8,191 tokens | Same. Both handle 1,024-token chunks easily. |

### Why `text-embedding-3-large` for Production

This is a production knowledge base serving end users who ask questions about policies, procedures, architecture, and compliance. Every search query that returns an irrelevant result or misses a relevant document erodes trust in the system. The 2.3% quality improvement means:

- **Better semantic matching:** "What is our policy on remote access?" correctly finds "Telework VPN guidelines"
- **Better disambiguation:** "interest rates" distinguishes between mortgage rates, auto loan rates, and credit card APR documents
- **Better cross-lingual matching:** Technical docs mixing acronyms, jargon, and plain English are better represented

The cost difference is negligible: $2/month vs $0.30/month.

### Configuration

```
FOUNDRY_EMBEDDING_DEPLOYMENT=text-embedding-3-large
FOUNDRY_EMBEDDING_DIMENSIONS=3072
```

The AI Search index `custom-kb-index` must be configured with `"dimensions": 3072` in the `content_vector` field and HNSW vector search profile.

> **Deployment Note:** The current dev deployment uses `text-embedding-3-small` (1536d) because the search index was initially created with 1536 dimensions. Upgrading to `text-embedding-3-large` requires: (1) verify `text-embedding-3-large` model deployment exists on `<foundry-account>`, (2) update `deploy.sh` settings to `FOUNDRY_EMBEDDING_DEPLOYMENT=text-embedding-3-large` and `FOUNDRY_EMBEDDING_DIMENSIONS=3072`, (3) recreate the search index with `"dimensions": 3072`, and (4) re-embed all existing documents. This is a planned production cutover -- not a code change.

---

## 10. ARB Q&A -- Architecture Review Board Ready

> 20 questions covering runtime behavior, metadata storage, vector persistence, failure handling, scaling, billing, and architectural decisions. Each answer is direct and includes example data where applicable.

---

### Q1: How long can a Function App run?

| Plan | Max Timeout | Our Usage |
|------|------------|-----------|
| **Consumption (current)** | **10 minutes** | 15-60 seconds per document |
| Premium (EP1) | 60 minutes | Not needed currently |
| Dedicated (App Service) | Unlimited | Overkill for this workload |

A single document processes in 15-60 seconds. The 10-minute limit is 10-40x our need. A 500-page PDF with 100 images (extreme case) takes ~8.4 minutes -- still within the limit. If a document does timeout, it moves to the failed container and retries automatically.

---

### Q2: Can we use a Container App instead of a Function App?

**Function Apps are the correct choice for this pipeline.** Container Apps are better when you need unlimited execution time, GPU, or sidecar containers.

| Factor | Function App (Consumption) | Container App |
|--------|---------------------------|---------------|
| **Triggers** | Native: Event Grid, Queue, Timer, HTTP -- zero config | Must configure KEDA scalers manually |
| **Scale-to-zero** | Automatic, $0 when idle | Requires KEDA configuration |
| **Cold start** | 3-5 seconds | 10-30+ seconds (container pull) |
| **Max execution** | 10 min (Consumption) / 60 min (Premium) | Unlimited |
| **Deploy complexity** | `func azure functionapp publish` | Dockerfile + container registry + KEDA + ingress |
| **Cost when idle** | $0 | $0 with KEDA, but more config overhead |

---

### Q3: When the pipeline is executing, where is the metadata getting stored?

Metadata flows through three locations during execution:

| Phase | Where Metadata Lives | What Is Stored |
|-------|---------------------|----------------|
| **In-flight (during processing)** | **Function App memory** (Python dict) | `file_name`, `source_url`, `source_type`, `content_type`, `file_size_bytes`, `chunk_index`, `page_number` |
| **At-rest (after indexing)** | **Azure AI Search** (`custom-kb-index`) | All metadata fields stored alongside chunk text and vector (see Q4 below) |
| **On failure** | **ADLS Gen2** (`raw-documents-failed/`) | `.error.json` sidecar: failure stage, error message, timestamp, stack trace |
| **Wiki sync state** | **ADLS Gen2** (`processing-state/`) | `last_modified` watermark timestamp for incremental sync |

During pipeline execution, metadata is built up incrementally in memory -- Stage 1 adds file info, Stage 2 adds page numbers, Stage 3 adds chunk indices -- and is written to AI Search only at Stage 6. If the function crashes, in-flight metadata is lost (no disk persistence needed -- the document retries from scratch).

---

### Q4: What does a single record look like in AI Search? (Metadata Table)

Every chunk stored in `custom-kb-index` is one record. Example for chunk 3 of a 12-chunk PDF:

| Field | Type | Example Value |
|-------|------|---------------|
| `id` | String (key) | `c2hhcmVwb2ludC9pdC1wb2xpY3kvZG9jcy9yZW1vdGUtYWNjZXNzLnBkZl8z` |
| `chunk_content` | String | `"[NAME REDACTED] approved the remote access policy requiring VPN for all external connections. MFA is mandatory..."` |
| `content_vector` | Collection(Single) | `[0.0123, -0.0456, 0.0789, ... ]` (3,072 floats) |
| `document_title` | String | `"remote-access-policy.pdf"` |
| `source_url` | String | `"https://contoso.sharepoint.com/sites/it-policy/docs/remote-access.pdf"` |
| `source_type` | String | `"sharepoint"` |
| `file_name` | String | `"remote-access-policy.pdf"` |
| `chunk_index` | Int32 | `3` |
| `total_chunks` | Int32 | `12` |
| `page_number` | Int32 | `4` |
| `last_modified` | DateTimeOffset | `"2026-02-15T10:30:00Z"` |
| `ingested_at` | DateTimeOffset | `"2026-02-24T14:22:00Z"` |
| `pii_redacted` | Boolean | `true` |

The `id` is deterministic: `base64("sharepoint/it-policy/docs/remote-access.pdf_3")`. Reprocessing the same file overwrites the same IDs -- no duplicates.

---

### Q5: How does the vector database persist? Where are vectors stored?

**Azure AI Search IS the vector database.** There is no separate vector store (no Pinecone, no Qdrant, no FAISS).

| Aspect | Detail |
|--------|--------|
| **Storage engine** | Azure AI Search Standard2 (`<search-service>`) -- managed service |
| **Vector field** | `content_vector` -- `Collection(Edm.Single)`, 3,072 dimensions |
| **Index algorithm** | HNSW (Hierarchical Navigable Small World) -- approximate nearest neighbor |
| **HNSW config** | `m=4`, `efConstruction=400`, `efSearch=500`, `metric=cosine` |
| **Persistence** | Fully managed. Azure AI Search stores vectors on SSD-backed storage with automatic replication. Data survives restarts, redeployments, and failovers. |
| **Backup** | Azure AI Search does not support native backup. To recover, re-run the pipeline -- ADLS raw documents are the source of truth. Index is rebuildable. |
| **Capacity** | Standard2 tier: up to 200 indexes, ~100 GB per partition. 100K chunks x 12 KB/vector = ~1.2 GB. Well within limits. |

```
Persistence model:

  ADLS Gen2 (source of truth)          AI Search (derived, rebuildable)
  <adls-account>                      <search-service> / custom-kb-index
  +----------------------+             +------------------------------+
  | /raw-documents/      |  pipeline   | chunk_content (text)         |
  |   original files     | --------->  | content_vector (3072 floats) |
  |   (preserved, w/PII) |  6 stages   | metadata fields              |
  +----------------------+             +------------------------------+
       Never modified                   Rebuildable from ADLS
```

---

### Q6: When there are failures in the pipeline, how does reprocessing occur?

Three-tier failure handling:

| Tier | Mechanism | When It Fires | Behavior |
|------|-----------|---------------|----------|
| **1. Queue retry** | `maxDequeueCount=5` | Transient failure (API 429, timeout) | Same message retried up to 5 times with visibility timeout between attempts |
| **2. Poison queue** | `doc-processing-queue-poison` | After 5 consecutive failures | Message moved to poison queue. Alerts via Application Insights. |
| **3. Failed container retry** | `reprocess_failed` timer (every 60 min) | Document moved to `raw-documents-failed` | Timer picks up 10 failed docs per run, retries from Stage 1. On success, deletes from failed container. |

```
Document fails at Stage 4 (PII):
  [1] Queue message retry (attempt 2 of 5) -> same message, immediate
  [2] Fails again -> retry (attempt 3 of 5)
  [3] Fails 5 times -> message moves to poison queue
  [4] Pipeline moves document to raw-documents-failed/ + writes .error.json
  [5] reprocess_failed timer (every 60 min) picks it up
  [6] Retries full pipeline from Stage 1
  [7] On success: deletes from failed container, indexes in AI Search
```

Reprocessing always restarts from **Stage 1** (no mid-pipeline checkpoints). Total pipeline time per document is 15-60 seconds, making full replay cheaper than checkpoint management.

---

### Q7: How many Function App instances will start?

| Scenario | Instances | Why |
|----------|-----------|-----|
| **Idle (no documents)** | **0** | Consumption Plan scales to zero when no events |
| **Single document upload** | **1** | One queue message = one invocation |
| **Steady-state (100 docs/day)** | **1-3** | Documents arrive sporadically; rarely concurrent |
| **Bulk load (10K docs at once)** | **15-20** | Queue scale controller detects backlog, scales out. Limited by `maxConcurrentCalls` to avoid overwhelming Foundry rate limits (15 concurrent DI requests). |
| **Theoretical max** | **200** | Consumption Plan hard limit. Would only hit this with unthrottled Event Grid mode. |

The Functions runtime scale controller monitors queue depth and adjusts instance count automatically. With `batchSize=1`, each instance processes one document at a time.

---

### Q8: How does Event Grid route to the queue?

```
[1] File uploaded to ADLS Gen2 (<adls-account>/raw-documents/)
     |
[2] ADLS fires Microsoft.Storage.BlobCreated event
     |
[3] Event Grid system topic (evgt-ingest-storage) receives event
     |
[4] Event Grid subscription (evgs-blob-to-function) routes event:
     |
     +-- TRIGGER_MODE=EVENTGRID_QUEUE:       -> doc-processing-queue (on <adls-account>)
     |                               Event serialized as JSON queue message
     |                               Function App polls queue -> process_queue_document()
     |
     +-- TRIGGER_MODE=EVENTGRID_DIRECT:  -> Function App directly (webhook)
     |                               Event delivered via HTTP -> process_new_document()
     |
     +-- TRIGGER_MODE=BLOB:        -> No Event Grid subscription needed
                                     Blob trigger polls storage directly -> process_blob_document()
```

For QUEUE and EVENT_GRID modes, the Event Grid subscription's `--endpoint-type` setting determines routing: `storagequeue` (queue mode) or `azurefunction` (direct mode). For BLOB mode, no Event Grid subscription is needed at all -- the blob trigger polls the container directly. Switching is a single `deploy.sh` re-run.

---

### Q9: Is the Function App invoked for each queue message, or does one instance handle multiple?

**One invocation per queue message** (with `batchSize=1`, the production default). Each queue message = one document = one function execution.

| Setting | Behavior |
|---------|----------|
| `batchSize=1` (production) | 1 message -> 1 invocation. Each document processed in isolation. |
| `batchSize=16` (bulk load) | 16 messages -> 1 invocation. Function receives an array of 16 messages and processes them sequentially within a single execution. Reduces cold start overhead for bulk loads. |

Multiple instances can run in parallel -- if 50 messages are in the queue, the runtime may spin up 10 instances each processing 1 message concurrently (with `batchSize=1`) or 4 instances each processing 16 messages (with `batchSize=16`).

---

### Q10: How are Function Apps billed?

| Component | Free Tier | Our Usage | Monthly Cost |
|-----------|-----------|-----------|-------------|
| **Executions** | 1M/month free | ~4,500/month (daily deltas) | **$0** |
| **Compute (GB-seconds)** | 400K GB-s/month free | ~67,500 GB-s/month | **$0** |
| **Storage** | N/A | `<func-storage>` backing store | **~$0.10** |
| **Total Function App hosting** | | | **~$0/month** |

The real cost is **Foundry API calls**, not Function App compute:

| API | Daily Delta Cost | Monthly Cost |
|-----|-----------------|-------------|
| Content Understanding (100 docs x 15 pages) | ~$2.00 | ~$60 |
| Azure Language PII (1,000 chunks) | ~$3.44 | ~$103 |
| Embeddings (107K tokens) | ~$0.01 | ~$0.40 |
| **Total API** | **~$5/day** | **~$150/month** |

---

### Q11: What happens if the Foundry API (Content Understanding, PII) is down?

| Failure | Impact | Auto-Recovery |
|---------|--------|---------------|
| **Foundry throttling (429)** | Pipeline retries with exponential backoff (built into SDK). Queue visibility timeout protects against duplicate processing. | Yes -- SDK retries + queue retries |
| **Foundry outage (500/503)** | Document fails -> moves to `raw-documents-failed`. `.error.json` records "Service unavailable." | Yes -- `reprocess_failed` timer retries every 60 min |
| **Content Understanding failure** | Parsing falls back to custom parsers (PyMuPDF, python-docx, etc.) for error recovery. Image verbalization is skipped in fallback. Log warning emitted. | Partial -- text indexed without image descriptions |
| **Language PII failure** | **Text passes through unredacted** with a warning log and `pii_redacted=false` flag. This is monitored via Application Insights alerts. | No -- requires investigation. Unredacted text in index is a compliance concern. |

---

### Q12: Does the LLM ever see raw PII?

**No.** The pipeline order is: Parse -> Chunk -> **PII Redact** -> Embed. The embedding model (`text-embedding-3-large`) receives only the PII-redacted text. Content Understanding receives only document binary bytes for parsing (no separate PII text processing). Raw PII exists only in ADLS Gen2 raw files and in-memory during Stages 1-3.

```
Stage 1-3 (in memory):   "John Smith (SSN: 123-45-6789) approved the loan for jane@example.org"
Stage 4 (PII redact):    "[NAME REDACTED] (SSN: [SSN REDACTED]) approved the loan for [EMAIL REDACTED]"
Stage 5 (embedding):     LLM receives -> "[NAME REDACTED] (SSN: [SSN REDACTED])..."  <- no raw PII
Stage 6 (AI Search):     Index stores  -> "[NAME REDACTED] (SSN: [SSN REDACTED])..."  <- no raw PII
```

---

### Q13: What is the cold start time? How does it affect user experience?

| Path | Cold Start | Why |
|------|-----------|-----|
| AI Foundry (production) | **3-5 seconds** | 68 MB deploy, no heavy ML models to load |
| Custom Libraries | **8-12 seconds** | 166 MB deploy, spaCy NER model loads on first call |

Cold start only affects the **first document** after the Function App has been idle. Subsequent documents process immediately (warm instance). For event-driven processing (documents arrive asynchronously), a 3-5 second cold start is invisible -- the document is still searchable within 30-60 seconds total.

---

### Q14: What is the total end-to-end latency for a single document?

| Stage | Time | Cumulative |
|-------|------|-----------|
| Event Grid + Queue delivery | 0-30 sec | 0-30 sec |
| Stage 1: Download from ADLS | <1 sec | ~30 sec |
| Stage 2: Content Understanding (parse + figures) | 3-8 sec | ~38 sec |
| Stage 3: Chunking | <1 sec | ~38 sec |
| Stage 4: PII detection | 1-3 sec | ~41 sec |
| Stage 5: Embedding | 1-2 sec | ~43 sec |
| Stage 6: Push to AI Search | 1-2 sec | ~45 sec |
| **Total (typical)** | | **30-50 seconds** |

From the moment a document is uploaded to ADLS, it is searchable in AI Search within 30-50 seconds.

---

### Q15: How do we monitor pipeline health and detect failures?

| Signal | Where | What It Shows |
|--------|-------|---------------|
| **Health endpoint** | `GET /api/health` on each Function App | Processing path, trigger mode, status |
| **Application Insights** | `<app-insights>` | Logs, exceptions, invocation counts, latency per stage |
| **Poison queue** | `doc-processing-queue-poison` | Documents that failed 5 consecutive times |
| **Failed container** | `raw-documents-failed/` | Documents with `.error.json` sidecars |
| **AI Search document count** | `custom-kb-index` | Total indexed chunks (should grow as docs are processed) |

---

### Q16: What happens if the same document is processed twice? (Idempotency)

**No duplicates.** Each chunk has a deterministic ID: `base64(file_path + "_" + chunk_index)`. The `merge_or_upload` operation in Stage 6 performs an upsert -- if the ID exists, it overwrites; if new, it inserts. Reprocessing the same file replaces the same chunk IDs. The index never contains duplicate chunks for the same document.

Example: `remote-access-policy.pdf` produces 12 chunks with IDs `base64("sharepoint/.../remote-access.pdf_0")` through `_11`. Reprocessing produces the same 12 IDs -> same 12 records overwritten.

---

### Q17: Why ADLS Gen2 instead of standard Blob Storage?

ADLS Gen2 adds hierarchical namespace (HNS) at the same price: atomic directory operations, POSIX ACLs for per-team access (`/raw-documents/sharepoint/hr-site/` restricted to HR), and faster directory listing for 10K+ files. The `azure-storage-blob` SDK works identically on both.

---

### Q18: Why a code-based pipeline instead of portal-configured indexers + skillsets?

The previous portal-configured pipeline used Azure AI Search indexers with skillsets. Three limitations drove the switch:

1. **No custom PII.** Skillsets only support built-in skills or Custom Web API skills (requires hosting a separate web service).
2. **Opaque errors.** Indexer failures give generic messages -- no per-document stack traces, no dead letter.
3. **Preview dependencies.** Integrated vectorization and some document cracking features are in preview. Preview features are not recommended for production.

The code pipeline gives explicit control over every stage, custom PII configuration, per-document error handling, and zero preview dependencies.

---

### Q19: Do we need LangChain? How does this connect to the LangGraph RAG Agent?

**LangChain is not used and not needed.** This is a document ingestion pipeline, not an LLM application. The pipeline makes direct SDK calls (`ContentUnderstandingClient`, `TextAnalyticsClient`, `AzureOpenAI`) -- LangChain would add an abstraction layer with no benefit.

The pipeline connects to the RAG Agent through the **AI Search index** -- the only integration point:

```
Ingestion Pipeline                    LangGraph RAG Agent
  Documents -> 6 stages ->               User question ->
    push to custom-kb-index               query custom-kb-index ->
              |                              retrieve top-K chunks ->
              +---- custom-kb-index --------+  synthesize answer
                    (shared contract)
```

The RAG Agent's retriever tool queries `<search-service>/custom-kb-index`. The index schema (`chunk_content`, `content_vector`, metadata fields) is the contract. The agent does not need to know how documents were ingested.

---

### Q20: Why not use Azure Data Factory (ADF) instead of Function Apps?

ADF is designed for data movement and ETL orchestration -- not document-level AI processing. Key mismatches: (1) no native SDK activities for Content Understanding + PII + embedding chains, (2) per-pipeline-run billing overhead exceeds Function App free tier, (3) 10-30 second orchestration latency per run is unacceptable for event-driven processing. ADF is better suited as a higher-level orchestrator (triggering bulk loads, scheduling Logic Apps) while Function Apps handle per-document processing.

---

## 11. Resource Inventory

### New Resources (Created by deploy.sh)

| # | Resource | Name | Type | Purpose |
|---|----------|------|------|---------|
| 1 | Resource Group | `<resource-group>` | Management | Isolation, cost tracking, clean teardown |
| 2 | ADLS Gen2 | `<adls-account>` | Storage (HNS) | Landing zone for all documents |
| 3 | Container | `raw-documents` | Blob Container | Incoming documents from SharePoint/Wiki |
| 4 | Container | `raw-documents-failed` | Blob Container | Dead letter for failed processing |
| 5 | Container | `processing-state` | Blob Container | Watermarks and checkpoints |
| 6 | Foundry Function App | `<func-foundry-app>` | Compute | AI Foundry pipeline (production primary) |
| 7 | Custom Function App | `<func-custom-app>` | Compute | Custom Libraries pipeline (bulk load / fallback). See [../custom-processing/README.md](../custom-processing/README.md). |
| 8 | Foundry FA Storage | `<func-storage>` | Storage | Internal storage for Foundry Function App |
| 9 | Custom FA Storage | `<func-custom-storage>` | Storage | Internal storage for Custom Function App |
| 10 | Application Insights | `<app-insights>` | Monitoring | Logging and telemetry for both Function Apps |
| 11 | AI Search Index | `custom-kb-index` on `<search-service>` | Search | Vector + keyword + semantic search index |
| 12 | Queue | `doc-processing-queue` on `<adls-account>` | Queue Storage | Event Grid -> Queue -> Function App (production trigger path) |
| 13 | Event Grid Topic | `evgt-ingest-storage` | Events | System topic on ADLS storage account |
| 14 | Event Grid Subscription | `evgs-blob-to-function` | Events | Routes BlobCreated to queue or Function App |

### Shared Resources (Consumed, Not Modified)

| Resource | What We Consume | Access Level |
|----------|----------------|-------------|
| `<foundry-account>` (Foundry) | `text-embedding-3-large` + Content Understanding + Language PII | Cognitive Services User (RBAC) |
| `<search-service>` (Standard2) | Host for `custom-kb-index` | Search Index Data Contributor (RBAC) |
| `<wiki-storage>` | Read `devops-wiki-store` container (wiki .md files) | Storage Blob Data Reader (RBAC) |

---

## 12. RBAC & Security

### Role Assignments (Per Function App)

Both Function Apps receive the same 5 RBAC roles via System Assigned Managed Identity:

| Target Resource | Role | Why This Role |
|----------------|------|--------------|
| `<adls-account>` | Storage Blob Data Contributor | Read raw docs, write failed docs, manage state blobs |
| `<adls-account>` | Storage Queue Data Contributor | Read/delete messages from `doc-processing-queue`, write to poison queue on failure |
| `<wiki-storage>` | Storage Blob Data Reader | Read wiki .md files from `devops-wiki-store` |
| `<foundry-account>` | Cognitive Services User | Content Understanding + Embeddings + PII |
| `<search-service>` | Search Index Data Contributor | Push chunks to `custom-kb-index` |

### Security Design

| Concern | Approach | Reasoning |
|---------|----------|-----------|
| **Authentication** | Managed Identity everywhere. `DefaultAzureCredential`. No API keys in code. | API keys can be leaked. Managed Identity is automatic, rotated by Azure, and cannot be leaked. |
| **Search auth** | Managed Identity (`DefaultAzureCredential`). No API keys. | RBAC roles: `Search Index Data Contributor` + `Search Service Contributor`. Zero keys to manage or rotate. |
| **PII at rest** | Raw files in ADLS contain PII. Redacted text in AI Search does not. | Two-tier access: compliance team reads raw ADLS files, general users search the PII-redacted index. |
| **PII in transit** | PII sent to Foundry endpoint over HTTPS (TLS 1.2, within Azure backbone). | Encrypted within Azure's network. No cross-region transfer. |
| **Network** | All resources in East US. HTTPS-only. Traffic stays within Azure backbone. | No public internet exposure for AI service calls. |

---

## 13. AI Search Index -- How the Vector Database Stores Data

### It's NOT a SQL Database

`custom-kb-index` is a **vector database** (Azure AI Search). There are no tables, no rows, no SQL. Each chunk is stored as a **single JSON document** -- the text, the vector, and all metadata live together in the same document. There is no separate metadata table.

```
+---------------------------------------------------------------------+
|                   ONE DOCUMENT IN custom-kb-index                     |
|                  (this is what ONE chunk looks like)                   |
+---------------------------------------------------------------------+
|                                                                       |
|  id              "c2hhcmVwb2ludC9pdC1zZWN1cml0eS..."  (base64 key)   |
|                                                                       |
|  +-- TEXT -----------------------------------------------------------+|
|  | chunk_content  "All remote access to the internal            ||
|  |                 network MUST use the approved VPN..."              ||
|  +-------------------------------------------------------------------+|
|                                                                       |
|  +-- VECTOR (3072 floats, not human-readable) -----------------------+|
|  | content_vector  [0.0123, -0.0456, 0.0789, ... 3072 dims]         ||
|  +-------------------------------------------------------------------+|
|                                                                       |
|  +-- METADATA (stored RIGHT HERE, not in a separate table) ----------+|
|  | document_title  "ps-remote-access-policy.md"                    ||
|  | source_url      "https://<adls-account>.blob.core..."            ||
|  | source_type     "sharepoint"                                      ||
|  | file_name       "ps-remote-access-policy.md"                    ||
|  | chunk_index     2           (this is chunk 2 of 8)                ||
|  | total_chunks    8           (the original doc made 8)             ||
|  | page_number     1           (came from page 1 of the PDF)         ||
|  | last_modified   2026-02-24T10:30:00Z                              ||
|  | ingested_at     2026-02-24T05:13:21Z                              ||
|  | pii_redacted    false       (no PII was found here)               ||
|  +-------------------------------------------------------------------+|
|                                                                       |
+-----------------------------------------------------------------------+

One source document (e.g., vpn-policy.pdf) becomes MULTIPLE documents in the index:

  vpn-policy.pdf (8 pages)
       |
       +-- chunk 0  { chunk_content: "...", content_vector: [...], chunk_index: 0, ... }
       +-- chunk 1  { chunk_content: "...", content_vector: [...], chunk_index: 1, ... }
       +-- chunk 2  { chunk_content: "...", content_vector: [...], chunk_index: 2, ... }
       |   ...
       +-- chunk 7  { chunk_content: "...", content_vector: [...], chunk_index: 7, ... }

  Total: 8 documents in the index for 1 source file.
  Currently: 25 documents in the index from 4 source files.
```

### Field Reference

| Field | Type | What it stores |
|-------|------|----------------|
| `id` | String (key) | Deterministic ID: `base64(file_path + chunk_index)` -- same file always produces same ID (enables retry without duplicates) |
| `chunk_content` | String | The actual text (PII-redacted). This is what the LLM reads and what keyword search matches against. |
| `content_vector` | 3072 floats | The embedding vector. Used for "find similar meaning" searches. Not human-readable. |
| `document_title` | String | Source file name |
| `source_url` | String | Full ADLS path back to the original file |
| `source_type` | String | `sharepoint` or `wiki` -- which source system |
| `file_name` | String | File name with extension |
| `chunk_index` | Int32 | Which chunk this is (0-based). Chunk 0 = start of document. |
| `total_chunks` | Int32 | How many chunks the source document produced |
| `page_number` | Int32 | Which page of the PDF/DOCX this chunk came from |
| `last_modified` | DateTime | When the source file was last modified |
| `ingested_at` | DateTime | When this chunk was pushed into the index |
| `pii_redacted` | Boolean | `true` if PII was found and replaced with `[NAME REDACTED]`, `[EMAIL REDACTED]`, etc. |

### How to Query (OData, Not SQL)

Azure AI Search uses **OData** query syntax, not SQL. Here are copy-paste commands:

```bash
# Set your search key once
SEARCH_KEY=$(az search admin-key show --service-name <search-service> \
  --resource-group <shared-rg> --query primaryKey -o tsv)

# -- Get everything (like SELECT * FROM table) --
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "*", "top": 5, "count": true}'

# -- Filter by field (like WHERE pii_redacted = true) --
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "*", "filter": "pii_redacted eq true", "select": "chunk_content,file_name,pii_redacted", "count": true}'

# -- Keyword search (like WHERE chunk_content LIKE '%VPN%') --
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "VPN remote access", "select": "chunk_content,file_name,chunk_index", "top": 3}'

# -- Semantic search (AI-ranked -- understands meaning, not just keywords) --
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "how do I connect remotely", "queryType": "semantic", "semanticConfiguration": "custom-kb-semantic-config", "top": 3}'

# -- Count by source type (like GROUP BY source_type) --
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "*", "facets": ["source_type"], "top": 0, "count": true}'

# -- Sort by ingestion time (like ORDER BY ingested_at DESC) --
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "*", "orderby": "ingested_at desc", "select": "file_name,chunk_index,ingested_at", "top": 5}'
```

### State Management & Retry

Processing state is NOT stored in the vector database. It's stored in **3 separate ADLS containers** on `<adls-account>`:

```
<adls-account> (ADLS Gen2)
|
+-- raw-documents/              <- Landing zone (source files arrive here)
|   +-- sharepoint/IT-KB/docs/vpn-policy.pdf
|
+-- raw-documents-failed/       <- Dead letter (failed files move here)
|   +-- sharepoint/IT-KB/docs/broken-file.xlsx          <- the file
|   +-- sharepoint/IT-KB/docs/broken-file.xlsx.error.json  <- why it failed
|       {
|         "error": "Parse error: openpyxl cannot read this file",
|         "timestamp": "2026-02-24T05:15:00Z"
|       }
|
+-- processing-state/           <- Watermarks (tracks what's been processed)
    +-- wiki-sync/watermark.json
        {
          "last_modified": "2026-02-24T04:00:00Z"
        }
```

**How retry works:**

```
Document lands in raw-documents/
        |
        v
  Pipeline runs (parse -> chunk -> PII -> embed -> push)
        |
   +----+----+
   |         |
SUCCESS    FAILURE
   |         |
   v         v
Chunks      File copied to raw-documents-failed/
pushed to   + .error.json written with failure reason
custom-kb-  |
index       |  Every 60 minutes (reprocess_failed timer):
            |    1. List files in raw-documents-failed/
            |    2. Retry up to 10 files per run
            |    3. On success: delete from failed container
            |    4. On failure: leave for next retry cycle
            v
         Retried -> success -> chunks in index
                 -> failure -> stays in failed container for next cycle
```

**Key points for retry:**
- The `id` field is deterministic (`base64(file_path + chunk_index)`). Re-uploading the same document produces the same chunk IDs, so retries **overwrite** existing chunks instead of creating duplicates.
- Failed files are never lost -- they sit in `raw-documents-failed/` until they succeed or are manually removed.
- The `.error.json` sidecar tells you exactly which pipeline stage failed and why.
- Wiki sync uses a watermark (`processing-state/wiki-sync/watermark.json`) to track the last successful sync time, so it only processes new/modified files.

---

## 14. Environment Variables

### `.env` File Setup (Production Deployment)

The `.env` file is the **single source of truth** for all environment variables during Azure deployment. The deploy scripts (`deploy.sh` in this folder, or `../deploy.sh` orchestrator) read this file and push all settings to the Function App.

```bash
# 1. Copy the template
cp .env.example .env

# 2. Fill in the only secret (all other values have working defaults)
#    Get the AI Search admin key:
az search admin-key show --service-name <search-service> \
  --resource-group <shared-rg> --query primaryKey -o tsv

# 3. Edit .env and replace <replace-with-actual-key> with the actual key
vi .env

# 4. Deploy (the script reads .env automatically)
IS_ACTIVE=true ./deploy.sh
```

> **Important:** `.env` files contain secrets and are excluded from git via `.gitignore`. Never commit `.env` to the repository. The `.env.example` template is safe to commit.

### `local.settings.json` (Local Development)

For local development with `func start`, environment variables are read from `local.settings.json` (also git-ignored). This file mirrors the `.env` values but wraps them in the Azure Functions JSON format. Update both files when adding new variables.

### AI Foundry-Specific Settings

| Variable | Value | Purpose |
|----------|-------|---------|
| `DOC_PROCESSING` | `AI_FOUNDRY_SERVICES` | Identifies this app's processing path |
| `FOUNDRY_ANALYZER_ID` | `prebuilt-documentSearch` | Content Understanding analyzer (RAG-optimized: layout + tables + figures + image descriptions) |

### Common Settings (Both Function Apps)

| Variable | Value | Purpose |
|----------|-------|---------|
| `ADLS_ACCOUNT_NAME` | `<adls-account>` | ADLS Gen2 account for document storage |
| `ADLS_CONTAINER_RAW` | `raw-documents` | Landing zone container |
| `ADLS_CONTAINER_FAILED` | `raw-documents-failed` | Dead letter container |
| `ADLS_CONTAINER_STATE` | `processing-state` | Watermark/checkpoint container |
| `ADLS_QUEUE_CONNECTION__queueServiceUri` | `https://<adls-account>.queue.core.windows.net` | Identity-based queue connection (Managed Identity, no keys) |
| `ADLS_BLOB_CONNECTION__blobServiceUri` | `https://<adls-account>.blob.core.windows.net` | Identity-based blob connection for blob trigger (Managed Identity, no keys) |
| `TRIGGER_MODE` | `EVENTGRID_QUEUE`, `EVENTGRID_DIRECT`, or `BLOB` | Controls which trigger function is active (default: `EVENTGRID_QUEUE`) |
| `WIKI_STORAGE_ACCOUNT_NAME` | `<wiki-storage>` | Wiki blob storage (cross-account read) |
| `WIKI_CONTAINER_NAME` | `devops-wiki-store` | Wiki .md files container |
| `FOUNDRY_ENDPOINT` | `https://<foundry-account>.cognitiveservices.azure.com` | Foundry multi-service endpoint |
| `FOUNDRY_EMBEDDING_DEPLOYMENT` | `text-embedding-3-large` | Production embedding model (3072d) |
| `FOUNDRY_EMBEDDING_DIMENSIONS` | `3072` | Embedding vector dimensions |
| `FOUNDRY_API_VERSION` | `2024-06-01` | Azure OpenAI API version |
| `SEARCH_ENDPOINT` | `https://<search-service>.search.windows.net` | AI Search endpoint |
| `SEARCH_INDEX_NAME` | `nfcu-rag-index` | Target search index |
| `CHUNK_SIZE_TOKENS` | `1024` | Tokens per chunk |
| `CHUNK_OVERLAP_TOKENS` | `200` | Token overlap between chunks |
| `PII_ENABLED` | `true` | Enable/disable PII scanning |
| `PII_CONFIDENCE_THRESHOLD` | `0.5` | Minimum confidence for PII detection |
| `BATCH_SIZE` | `100` | AI Search push batch size |
| `LOG_LEVEL` | `INFO` | Logging level |
| `AzureWebJobsFeatureFlags` | `EnableWorkerIndexing` | Required for Python v2 programming model |

### Trigger Configuration (Schedules, Queue, Retry)

All trigger behavior is configurable via environment variables. The `%VAR_NAME%` syntax in Azure Functions decorators resolves these at runtime.

| Variable | Default | Purpose |
|----------|---------|---------|
| `QUEUE_NAME` | `doc-processing-queue` | Queue name for Event Grid → Queue → Function trigger |
| `WIKI_SYNC_SCHEDULE` | `0 */15 * * * *` | Wiki sync timer (NCRONTAB: every 15 min) |
| `REPROCESS_FAILED_SCHEDULE` | `0 0 * * * *` | Failed doc retry timer (NCRONTAB: every 60 min) |
| `REPROCESS_BATCH_SIZE` | `10` | Max documents to retry per reprocess run (prevents timeout) |

**NCRONTAB format:** `{second} {minute} {hour} {day} {month} {day-of-week}` — Azure Functions uses 6-field cron expressions (includes seconds). Examples:
- `0 */15 * * * *` — every 15 minutes
- `0 0 * * * *` — every hour (top of hour)
- `0 */5 * * * *` — every 5 minutes
- `0 0 */6 * * *` — every 6 hours
- `0 30 9 * * 1-5` — weekdays at 9:30 AM

### Trigger Enable/Disable Settings (Auto-Configured by deploy.sh)

When the app is **active**, document triggers follow `TRIGGER_MODE` and timers are enabled.
When the app is **inactive**, ALL triggers are disabled (no processing, no wiki sync, no retries).

| Variable | Active (EVENTGRID_QUEUE) | Active (EVENTGRID_DIRECT) | Active (BLOB) | Inactive |
|----------|---------------|-------------------|--------------|----------|
| `AzureWebJobs.process_new_document.Disabled` | `true` | `false` | `true` | `true` |
| `AzureWebJobs.process_queue_document.Disabled` | `false` | `true` | `true` | `true` |
| `AzureWebJobs.process_blob_document.Disabled` | `true` | `true` | `false` | `true` |
| `AzureWebJobs.process_wiki_sync.Disabled` | `false` | `false` | `false` | `true` |
| `AzureWebJobs.reprocess_failed.Disabled` | `false` | `false` | `false` | `true` |

---

## 15. Project Structure

```
ai-foundry-processing/
  README.md                    # This file
  .env                         # Environment variables for deployment (git-ignored, contains secrets)
  .env.example                 # Template — copy to .env and fill in endpoints
  local.settings.json          # Local dev settings for `func start` (git-ignored)
  function_app.py              # 6 triggers (EventGrid, Queue, Blob, 2 Timers, HTTP)
  host.json                    # 10-min timeout, queue config, blob config
  requirements.txt             # azure-ai-* SDKs + fallback parsers (NO presidio/spaCy)
  modules/
    pipeline.py                # FoundryDocPipeline (6-stage orchestrator)
    foundry_parser.py          # Content Understanding (parse + figures + image verbalization)
    foundry_pii_scanner.py     # Azure Language PII (via Foundry endpoint)
    adls_reader.py             # ADLS read/write/state/move-to-failed
    chunker.py                 # TokenChunker + MarkdownChunker (tiktoken)
    embedder.py                # Foundry LLM text-embedding-3-large
    search_pusher.py           # AI Search merge_or_upload (batch=100)
    parsers/
      parser_factory.py        # Extension -> parser routing (fallback)
      pdf_parser.py            # PyMuPDF (fallback for wiki/error recovery)
      docx_parser.py           # python-docx (fallback)
      xlsx_parser.py           # openpyxl (fallback)
      pptx_parser.py           # python-pptx (fallback)
      markdown_parser.py       # Plain text (wiki .md files)
      txt_parser.py            # Plain text
```

---

## 16. Deployment & Infrastructure

### Infrastructure Scripts

The infrastructure is split into modular scripts. Each can be run standalone or via the orchestrator.

| Script | What It Does | When to Use |
|--------|-------------|-------------|
| `../deploy.sh` | Full orchestrator: shared infra + both apps + Event Grid routing + verify | First-time setup, or switching active path |
| `./deploy.sh` | This Function App only (create, RBAC, app settings, publish code) | Redeploying this app after code changes |
| `../custom-processing/deploy.sh` | Custom Processing Function App only | Redeploying the Custom app |
| `../teardown.sh` | Full teardown (RBAC cleanup + AI Search index delete + RG delete) | Removing everything |

#### deploy.sh (this folder): 5 Steps

| Step | What |
|------|------|
| 1 | Create Function App + storage account (`<func-storage>`) |
| 2 | Enable Managed Identity + assign 5 RBAC roles |
| 3 | Configure app settings from `.env` file + dynamic trigger overrides |
| 4 | Publish function code (`func azure functionapp publish`) |
| 5 | Verify + restart |

#### ../deploy.sh Full Orchestration (5 Phases)

| Phase | Script Called | What Happens |
|-------|-------------|-------------|
| 1 | (inline) | RG, ADLS Gen2, containers, queue, App Insights, Event Grid topic, AI Search index |
| 2 | `custom-processing/deploy.sh` | Custom Function App (active or inactive based on `DOC_PROCESSING`) |
| 3 | `ai-foundry-processing/deploy.sh` | AI Foundry Function App (active or inactive based on `DOC_PROCESSING`) |
| 4 | (inline) | Event Grid subscription routing to active app |
| 5 | (inline) | Final verification of all resources |

#### teardown.sh: 4 Steps

| Step | What | Why |
|------|------|-----|
| 1 | Get Managed Identity principal IDs | Must capture before RG deletion |
| 2 | Remove RBAC on shared resources | Orphaned role assignments would remain |
| 3 | Delete AI Search index | Index is on shared `<search-service>` |
| 4 | Delete Resource Group | Cascading delete removes everything inside |

### Monthly Cost Estimate (Production Steady-State)

| Resource | Monthly Cost | Notes |
|----------|-------------|-------|
| Foundry: Content Understanding | ~$60 | 100 docs/day — parsing + figure analysis in one call |
| Foundry: Language PII | ~$103 | 1,000 chunks/day x $0.001/1000 chars |
| Foundry: Embeddings | ~$0.40 | ~300K tokens/month |
| Function App (Consumption) | ~$0-5 | Well under free tier |
| ADLS Gen2 + Queue + Event Grid | ~$5-20 | Storage + negligible messaging |
| Application Insights | ~$0-10 | 5 GB/month free tier |
| **Total (AI Foundry path)** | **~$110-190/month** | Higher AI API cost for maximum search quality |

---

## 17. Data Sources — How Documents Reach ADLS

> **Pipeline scope:** ADLS `raw-documents/` container -> AI Search `custom-kb-index`. Everything upstream (how documents land in ADLS) is a separate deployment concern.

### SharePoint Documents

Documents from SharePoint must be copied into `<adls-account>/raw-documents/sharepoint/` as raw files. Options:

- **Logic App** (recommended) -- Sliding-window trigger polls SharePoint, copies raw files + metadata sidecar to ADLS. BlobCreated event triggers the pipeline.
- **AzCopy / manual upload** -- Batch copy for initial loads.
- **Power Automate** -- Alternative to Logic App for organizations already using Power Platform.

The pipeline does not care how files arrive — it triggers on any `BlobCreated` event in `raw-documents/`.

### Metadata Sidecar Pattern

When uploading files, an optional `.metadata.json` sidecar can accompany each file:

```json
{
  "source_url": "/sites/IT-KnowledgeBase/Shared Documents/vpn-policy.pdf",
  "source_type": "sharepoint",
  "file_name": "vpn-policy.pdf",
  "last_modified": "2026-02-24T10:30:00Z"
}
```

Sidecar files are automatically excluded from processing (Event Grid filter: `StringNotContains .metadata.json`). The pipeline reads the sidecar to enrich chunk metadata.

### Wiki Documents

The wiki pipeline (`Syncer-LogicApp-B` -> `<wiki-storage>/devops-wiki-store`) already works. The ingestion Function App cross-reads from that container via Managed Identity (Storage Blob Data Reader role). Zero touch required.

---

## 18. Future Roadmap

### Near-Term: Production Hardening

| Item | Description | Priority |
|------|------------|----------|
| ~~**Queue intermediary**~~ | **DONE** -- deployed as `doc-processing-queue`. Active when `TRIGGER_MODE=EVENTGRID_QUEUE`. | **Done** |
| **SharePoint-to-ADLS ingestion** | Out of scope for this pipeline. Documents must land in `raw-documents/` via Logic App, AzCopy, or manual upload. See [Section 17](#17-data-sources--how-documents-reach-adls). | **Separate** |
| **Embedding model upgrade** | Switch from `text-embedding-3-small` (1536d) to `text-embedding-3-large` (3072d). Requires index recreation and re-embedding. | **High** |

### Medium-Term: Scale & Quality

| Item | Description |
|------|------------|
| **Batch processing tuning** | Tune `batchSize` and `maxConcurrentCalls` in `host.json` for initial full load of 10K+ docs. Stay within Foundry rate limits. |
| **Multi-language support** | Currently English-only. Add multilingual analyzers for member communications or regulatory docs. |
| **Incremental reindexing** | Detect document updates via ETag/last_modified. Reprocess only changed chunks. |
| **Private endpoints** | VNet integration + private endpoints for ADLS, AI Search, Foundry. Network isolation for production security. |

### Implemented: Azure AI Content Understanding

Azure AI Content Understanding (GA, API version `2025-11-01`) replaced the previous Document Intelligence + GPT-4o Vision two-step flow with a single API call. This was a **Stage 2 only** change — the `prebuilt-documentSearch` analyzer handles text extraction, table detection, figure analysis, and image verbalization in one call.

```
Previous (2 separate API calls per document):
  [Stage 2a] Doc Intelligence -> extract text, tables, detect figures
  [Stage 2b] GPT-4o Vision   -> verbalize each detected figure

Current (1 API call per document):
  [Stage 2] Content Understanding (prebuilt-documentSearch) -> structured markdown
             with text, tables, figures, and image descriptions
```

**Stage 4 (PII)** remains unchanged — Azure Language PII (`TextAnalyticsClient`) continues to handle PII detection and redaction separately.

### Future: Content Understanding for PII

Content Understanding may eventually support PII detection as part of its analysis, which could consolidate Stages 2 and 4 into a single call. Migration would replace the PII scanner code with no architectural changes.

### Video Processing (Future Vision)

The modular pipeline design supports video processing without restructuring:
1. **Transcription:** Azure AI Speech (speech-to-text) -> timestamped text
2. **Frame extraction:** Sample key frames at intervals
3. **Frame verbalization:** Content Understanding's `prebuilt-videoSearch` analyzer
4. **Combined indexing:** Merge transcript + frame descriptions into searchable chunks

Content Understanding already supports native video analysis via `prebuilt-videoSearch`.

---

## Appendix: Verified End-to-End Test Results

### AI Foundry Services Path (2026-02-21)

```
Input:  test-foundry-e2e.md (markdown with PII: name, email, IP address)
Result: 4 chunks indexed in custom-kb-index
PII:    NAME -> [REDACTED], EMAIL -> [REDACTED], IP -> [REDACTED]
Path:   AI_FOUNDRY_SERVICES
Time:   ~20 seconds end-to-end (ADLS upload -> searchable in AI Search)
```

### Queue Trigger Test (2026-02-23)

```
Input:  report.md (markdown with PII: names, email, phone, SSN, address)
Flow:   ADLS -> Event Grid -> doc-processing-queue -> <func-foundry-app> (queue trigger)
Result: 5 chunks indexed in custom-kb-index
PII:    Names -> [NAME REDACTED], Email -> [EMAIL REDACTED], Phone -> [PHONE REDACTED]
Path:   AI_FOUNDRY_SERVICES (via TRIGGER_MODE=EVENTGRID_QUEUE)
Time:   ~30 seconds end-to-end (ADLS upload -> searchable in AI Search)
```

Both processing paths and both trigger modes produce documents that coexist in the same `custom-kb-index` and are searchable via the same hybrid search queries. PII redaction verified -- sensitive data never enters the search index.

---

## 19. Deployment Instructions — Two Options

There are two deployment scenarios depending on your organization's infrastructure setup.

### Option A: Configure Only — Pre-Provisioned Infrastructure

In this scenario your organization's cloud team has already provisioned the Azure resources (resource group, ADLS Gen2, AI Foundry, AI Search, etc.) and hands you the resource names, endpoints, and keys. You only need to configure the `.env` file and deploy the Function App code.

#### Values You Need From Your Cloud Team

Ask your organization's cloud/platform team for the following values. These map directly to the `.env` file:

| # | What to Ask For | `.env` Variable | Example (Dev) |
|---|----------------|-----------------|---------------|
| 1 | **Azure Subscription ID** | `DEPLOY_SUBSCRIPTION_ID` | `<subscription-id>` |
| 2 | **Resource Group name** (where Function App will live) | `DEPLOY_RG_NAME` | `<resource-group>` |
| 3 | **Azure region** | `DEPLOY_LOCATION` | `eastus` |
| 4 | **ADLS Gen2 storage account name** | `ADLS_ACCOUNT_NAME` | `<adls-account>` |
| 5 | **ADLS container names** (raw, failed, state) | `ADLS_CONTAINER_RAW`, `ADLS_CONTAINER_FAILED`, `ADLS_CONTAINER_STATE` | `raw-documents`, `raw-documents-failed`, `processing-state` |
| 6 | **Queue name** on the ADLS storage account | `QUEUE_NAME` | `doc-processing-queue` |
| 7 | **AI Foundry endpoint** (Cognitive Services multi-service resource) | `FOUNDRY_ENDPOINT` | `https://<foundry-account>.cognitiveservices.azure.com` |
| 8 | **AI Foundry resource name** (for RBAC) | `DEPLOY_FOUNDRY_ACCOUNT` | `<foundry-account>` |
| 9 | **AI Foundry resource group** (for RBAC) | `DEPLOY_FOUNDRY_RG` | `<shared-rg>` |
| 10 | **Embedding model deployment name** | `FOUNDRY_EMBEDDING_DEPLOYMENT` | `text-embedding-3-large` |
| 11 | **Embedding dimensions** (must match model) | `FOUNDRY_EMBEDDING_DIMENSIONS` | `3072` (for 3-large) or `1536` (for 3-small) |
| 12 | **Content Understanding analyzer** | `FOUNDRY_ANALYZER_ID` | `prebuilt-documentSearch` (requires gpt-4.1-mini + text-embedding-3-large deployed in Foundry) |
| 13 | **AI Search service name** | `DEPLOY_SEARCH_SERVICE` | `<search-service>` |
| 14 | **AI Search resource group** (for RBAC) | `DEPLOY_SEARCH_RG` | `<shared-rg>` |
| 15 | **AI Search index name** | `SEARCH_INDEX_NAME` | `nfcu-rag-index` |
| 17 | **Wiki blob storage account** (if wiki sync is needed) | `WIKI_STORAGE_ACCOUNT_NAME` | `<wiki-storage>` |
| 18 | **Wiki container name** | `WIKI_CONTAINER_NAME` | `devops-wiki-store` |
| 19 | **Wiki storage resource group** (for RBAC) | `DEPLOY_WIKI_RG` | `<shared-rg>` |
| 20 | **Application Insights name** (existing or new) | `DEPLOY_APP_INSIGHTS` | `<app-insights>` |
| 21 | **Function App name** to create | `DEPLOY_FUNC_APP_NAME` | `<func-foundry-app>` |
| 22 | **Function App backing storage account** to create | `DEPLOY_FUNC_STORAGE_ACCOUNT` | `<func-storage>` |

#### Step-by-Step: Configure and Deploy

```bash
# 1. Navigate to the Function App directory
cd ai-foundry-processing

# 2. Copy the template
cp .env.example .env

# 3. Open .env and replace ALL values with your actual resource names
#    The DEPLOY_* variables control infrastructure targeting.
#    The remaining variables become Function App settings.
vi .env

# -- Minimum required edits (replace right-hand side with your actual values) --
#
# DEPLOY_SUBSCRIPTION_ID=<ps-subscription-id>
# DEPLOY_LOCATION=<ps-region>
# DEPLOY_RG_NAME=<ps-resource-group>
# DEPLOY_FUNC_APP_NAME=<ps-function-app-name>
# DEPLOY_FUNC_STORAGE_ACCOUNT=<ps-func-storage-name>
# DEPLOY_APP_INSIGHTS=<ps-app-insights-name>
# DEPLOY_SEARCH_SERVICE=<ps-search-service>
# DEPLOY_SEARCH_RG=<ps-search-rg>
# DEPLOY_FOUNDRY_ACCOUNT=<ps-foundry-resource>
# DEPLOY_FOUNDRY_RG=<ps-foundry-rg>
# DEPLOY_WIKI_RG=<ps-wiki-rg>
#
# ADLS_ACCOUNT_NAME=<ps-adls-account>
# ADLS_QUEUE_CONNECTION__queueServiceUri=https://<ps-adls-account>.queue.core.windows.net
# FOUNDRY_ENDPOINT=https://<ps-foundry-resource>.cognitiveservices.azure.com
# FOUNDRY_EMBEDDING_DEPLOYMENT=<ps-embedding-model-name>
# FOUNDRY_EMBEDDING_DIMENSIONS=<1536-or-3072>
# FOUNDRY_ANALYZER_ID=prebuilt-documentSearch
# SEARCH_ENDPOINT=https://<ps-search-service>.search.windows.net
# SEARCH_INDEX_NAME=<ps-index-name>
# WIKI_STORAGE_ACCOUNT_NAME=<ps-wiki-storage>
# WIKI_CONTAINER_NAME=<ps-wiki-container>

# 4. Authenticate to the correct Azure subscription
az login
az account set --subscription <ps-subscription-id>

# 5. Deploy the Function App (creates the app, assigns RBAC, pushes code)
IS_ACTIVE=true ./deploy.sh

# 6. Verify health
curl https://<ps-function-app-name>.azurewebsites.net/api/health

# 7. Check logs
func azure functionapp logstream <ps-function-app-name>
```

#### What `deploy.sh` Does

| Step | What Happens | Resources Touched |
|------|-------------|----------------------|
| **Step 1** | Creates Function App + backing storage account | Creates `DEPLOY_FUNC_APP_NAME` and `DEPLOY_FUNC_STORAGE_ACCOUNT` in `DEPLOY_RG_NAME` |
| **Step 2** | Enables Managed Identity on the Function App and assigns 5 RBAC roles | Assigns roles on your organization's ADLS, AI Search, Foundry, and Wiki storage |
| **Step 3** | Pushes all `.env` settings (except `DEPLOY_*`) as Function App app settings | Configures the Function App to point to your organization's resources |
| **Step 4** | Publishes function code via `func azure functionapp publish` | Deploys Python code to the Function App |
| **Step 5** | Restarts and verifies | Ensures the app is running |

> **RBAC note:** The deploying user (or service principal) must have sufficient permissions to create role assignments on your organization's shared resources (ADLS, AI Search, Foundry, ). If your cloud team pre-assigns RBAC, skip Step 2 by commenting out the role assignment block in `deploy.sh`, or ask them to assign the 4 roles listed in [Section 12](#12-rbac--security) to the Function App's Managed Identity principal ID.

#### After Deployment: End-to-End Validation

```bash
# Upload a test document to your organization's ADLS — triggers the full pipeline
echo "Test document for validation." > /tmp/test-deploy.md
az storage blob upload \
  --account-name <ps-adls-account> \
  --container-name raw-documents \
  --name sharepoint/test-site/docs/test-deploy.md \
  --file /tmp/test-deploy.md --auth-mode login

# Wait ~30s, then verify the document was indexed
SEARCH_KEY="<ps-search-admin-key>"
curl -s "https://<ps-search-service>.search.windows.net/indexes/<ps-index-name>/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "validation", "top": 3, "select": "chunk_content,file_name"}'
```

---

### Option B: Full Provisioning — Deploy Everything Into a New Subscription

In this scenario you deploy the entire infrastructure from scratch: resource group, ADLS Gen2, containers, queue, Event Grid, Application Insights, AI Search index, and both Function Apps. Use this when deploying to a new Azure subscription where none of the shared resources exist yet.

#### Prerequisites

| Prerequisite | How to Verify | Why |
|-------------|--------------|-----|
| Azure CLI authenticated | `az account show` | All infrastructure created via `az` commands |
| Correct subscription set | `az account set --subscription <subscription-id>` | Resources created in the target subscription |
| Azure Functions Core Tools v4 | `func --version` | Publishes function code |
| Python 3.11 | `python3 --version` | Function App runtime |
| Permissions: **Owner** or **Contributor + User Access Administrator** on the subscription | `az role assignment list --assignee $(az ad signed-in-user show --query id -o tsv) --scope /subscriptions/<sub-id>` | Script creates resource groups, storage accounts, RBAC role assignments, Event Grid topics |

#### Pre-Existing Shared Resources Required

The full deploy script creates new infrastructure but expects these shared resources to already exist (they are consumed, not created):

| Resource | Expected Name (Default) | What It Provides | Who Creates It |
|----------|------------------------|-----------------|----------------|
| **Azure AI Foundry** | `<foundry-account>` in `<shared-rg>` | Content Understanding, Embeddings, Language PII. Requires gpt-4.1-mini + text-embedding-3-large model deployments. | Your AI/ML team (or via Azure AI Studio) |
| **Azure AI Search** | `<search-service>` (Standard2) in `<shared-rg>` | Hosts `custom-kb-index` for vector + keyword search | Your platform team (or `az search service create`) |
| **Wiki Blob Storage** | `<wiki-storage>` in `<shared-rg>` | ADO Wiki `.md` files in `devops-wiki-store` container | Already exists (ADO pipeline) |

> **If these don't exist yet**, provision them first:
> ```bash
> # Create AI Search (Standard2 tier for semantic ranking + vector search)
> az search service create \
>   --name <search-service> \
>   --resource-group <shared-rg> \
>   --sku standard2 \
>   --location eastus
>
> # AI Foundry: Create via Azure AI Studio portal (https://ai.azure.com)
> #   -> New project -> Deploy models: gpt-4.1-mini, text-embedding-3-large
> #   -> Run sample_update_defaults.py to configure Content Understanding model mappings
> #   -> Enable Language PII on the Foundry resource
> ```

#### Step-by-Step: Full Deployment

```bash
# 1. Clone the repo and navigate to the pipeline root
cd ai-azure-foundry-ingestion-pipeline

# 2. (Optional) Override defaults if deploying to a different subscription/region/naming
#    All values have sensible defaults — only override what differs.
#    See the full variable list in deploy.sh (lines 32-57).
export SUBSCRIPTION_ID="<your-subscription-id>"
export LOCATION="<your-region>"                            # default: eastus
export RG_NAME="<your-resource-group>"                     # default: <resource-group>
export ADLS_ACCOUNT="<your-adls-name>"                     # default: <adls-account>
export FUNC_FOUNDRY_APP="<your-foundry-func-name>"         # default: <func-foundry-app>
export FUNC_FOUNDRY_STORAGE="<your-func-storage>"          # default: <func-storage>
export FUNC_CUSTOM_APP="<your-custom-func-name>"           # default: <func-custom-app>
export FUNC_CUSTOM_STORAGE="<your-custom-storage>"         # default: <func-custom-storage>
export SEARCH_SERVICE="<your-search-service>"              # default: <search-service>
export SEARCH_RG="<your-search-rg>"                        # default: <shared-rg>
export FOUNDRY_ACCOUNT="<your-foundry-resource>"           # default: <foundry-account>
export FOUNDRY_RG="<your-foundry-rg>"                      # default: <shared-rg>
export WIKI_STORAGE="<your-wiki-storage>"                  # default: <wiki-storage>
export WIKI_RG="<your-wiki-rg>"                            # default: <shared-rg>

# 3. Configure the per-app .env files (the deploy scripts read these for app settings)
cd ai-foundry-processing
cp .env.example .env
vi .env   # Update FOUNDRY_ENDPOINT, SEARCH_ENDPOINT, etc. to match your resources
cd ..

cd custom-processing
cp .env.example .env
vi .env   # Same — update endpoints and keys
cd ..

# 4. Authenticate
az login
az account set --subscription "$SUBSCRIPTION_ID"

# 5. Make scripts executable
chmod +x deploy.sh ai-foundry-processing/deploy.sh custom-processing/deploy.sh

# 6. Run the full orchestrator — AI Foundry path, Queue trigger mode
DOC_PROCESSING=AI_FOUNDRY_SERVICES TRIGGER_MODE=EVENTGRID_QUEUE ./deploy.sh
```

#### What the Full Orchestrator Creates (5 Phases)

```
Phase 1: Shared Infrastructure
  ├── Resource Group .................. <resource-group>
  ├── ADLS Gen2 (HNS enabled) ........ <adls-account>
  ├── Containers ...................... raw-documents, raw-documents-failed, processing-state
  ├── Queue ........................... doc-processing-queue (on <adls-account>)
  ├── Application Insights ............ <app-insights>
  ├── Event Grid System Topic ......... evgt-ingest-storage
  └── AI Search Index ................. custom-kb-index (on <search-service>)

Phase 2: Custom Processing Function App
  ├── Function App .................... <func-custom-app>
  ├── Backing Storage ................. <func-custom-storage>
  ├── Managed Identity + 5 RBAC roles
  ├── App Settings from .env
  └── Code publish

Phase 3: AI Foundry Processing Function App
  ├── Function App .................... <func-foundry-app>
  ├── Backing Storage ................. <func-storage>
  ├── Managed Identity + 5 RBAC roles
  ├── App Settings from .env
  └── Code publish

Phase 4: Event Grid Subscription
  └── evgs-blob-to-function routes BlobCreated events to active app
      (Queue mode: Event Grid -> doc-processing-queue -> Function App)
      (Event Grid mode: Event Grid -> Function App direct)

Phase 5: Verification
  └── Checks all resources exist and are running
```

#### Configurable Variables for Full Provisioning

All variables have defaults. Override only what differs from the dev environment:

| Variable | Default | What It Controls |
|----------|---------|-----------------|
| `SUBSCRIPTION_ID` | `<subscription-id>` | Target Azure subscription |
| `LOCATION` | `eastus` | Azure region for new resources |
| `RG_NAME` | `<resource-group>` | Resource group for pipeline infrastructure |
| `ADLS_ACCOUNT` | `<adls-account>` | ADLS Gen2 account name (must be globally unique, 3-24 lowercase alphanumeric) |
| `FUNC_FOUNDRY_APP` | `<func-foundry-app>` | AI Foundry Function App name (must be globally unique) |
| `FUNC_FOUNDRY_STORAGE` | `<func-storage>` | Backing storage for Foundry Function App |
| `FUNC_CUSTOM_APP` | `<func-custom-app>` | Custom Processing Function App name |
| `FUNC_CUSTOM_STORAGE` | `<func-custom-storage>` | Backing storage for Custom Function App |
| `APP_INSIGHTS` | `<app-insights>` | Application Insights instance |
| `EVENT_GRID_TOPIC` | `evgt-ingest-storage` | Event Grid system topic name |
| `EVENT_GRID_SUB` | `evgs-blob-to-function` | Event Grid subscription name |
| `QUEUE_NAME` | `doc-processing-queue` | Queue for Event Grid -> Queue -> Function flow |
| `SEARCH_SERVICE` | `<search-service>` | AI Search service (must exist, see prerequisites) |
| `SEARCH_RG` | `<shared-rg>` | Resource group of AI Search service |
| `SEARCH_INDEX` | `custom-kb-index` | Index name to create |
| `FOUNDRY_ACCOUNT` | `<foundry-account>` | AI Foundry resource (must exist, see prerequisites) |
| `FOUNDRY_RG` | `<shared-rg>` | Resource group of Foundry resource |
| `WIKI_STORAGE` | `<wiki-storage>` | Wiki blob storage (must exist, see prerequisites) |
| `WIKI_RG` | `<shared-rg>` | Resource group of wiki storage |
| `DOC_PROCESSING` | `CUSTOM_LIBRARIES` | Which Function App is active: `AI_FOUNDRY_SERVICES` or `CUSTOM_LIBRARIES` |
| `TRIGGER_MODE` | `EVENTGRID_QUEUE` | Event delivery: `EVENTGRID_QUEUE` (production), `EVENTGRID_DIRECT` (low-latency), or `BLOB` (simplest) |

#### After Full Provisioning: Verify and Test

```bash
# Verify all resources
az group show --name <resource-group> --query "properties.provisioningState" -o tsv
curl https://<func-foundry-app>.azurewebsites.net/api/health

# End-to-end test: upload a document and verify it appears in search
echo "Full provisioning test. Contact john@ps.org for details." > /tmp/provision-test.md
az storage blob upload \
  --account-name <adls-account> \
  --container-name raw-documents \
  --name sharepoint/test-site/docs/provision-test.md \
  --file /tmp/provision-test.md --auth-mode login

# Wait ~30s, then check AI Search
SEARCH_KEY=$(az search admin-key show --service-name <search-service> \
  --resource-group <shared-rg> --query primaryKey -o tsv)
curl -s "https://<search-service>.search.windows.net/indexes/custom-kb-index/docs/search?api-version=2024-07-01" \
  -H "Content-Type: application/json" -H "api-key: $SEARCH_KEY" \
  -d '{"search": "provisioning test", "top": 3, "select": "chunk_content,file_name,pii_redacted"}'

# Expected: chunk_content shows "[EMAIL REDACTED]" (PII redacted), pii_redacted: true
```

#### Teardown (Full Cleanup)

```bash
# From repo root — removes everything: RBAC on shared resources, AI Search index, resource group
./teardown.sh
```

---

### Option A vs Option B — Decision Matrix

| Factor | Option A: Configure Only | Option B: Full Provisioning |
|--------|-------------------------|---------------------------|
| **When to use** | Pre-provisioned resources available | Deploying to a new/empty subscription |
| **What you run** | `cd ai-foundry-processing && IS_ACTIVE=true ./deploy.sh` | `DOC_PROCESSING=AI_FOUNDRY_SERVICES ./deploy.sh` (from repo root) |
| **What gets created** | 1 Function App + 1 storage account | Resource group, ADLS, containers, queue, Event Grid, App Insights, AI Search index, 2 Function Apps |
| **Permissions needed** | Contributor on the resource group + ability to assign RBAC on shared resources | Owner or Contributor + User Access Administrator on the subscription |
| **Coordination** | Need resource names, endpoints, keys from your cloud team | Need Foundry + AI Search resources to exist beforehand |
| **Teardown** | Delete the Function App and its backing storage manually | `./teardown.sh` removes everything |

---

## Further Reading

- [../custom-processing/README.md](../custom-processing/README.md) -- Custom Processing deployment guide
- [../CUSTOM_README.md](../CUSTOM_README.md) -- Detailed Custom vs Foundry comparison, scale analysis, PII accuracy breakdown
