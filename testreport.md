# E2E Concurrent Upload Test Report

**Date**: February 27, 2026
**Environment**: Azure (East US) — Consumption Plan
**Trigger**: `BLOB`
**Test**: Upload 3 files simultaneously, process through both pipelines, compare

---

## Scenario Overview

### Single-Document Scenarios (1-6)

Same 3 files processed through both pipelines:

| # | File | Pipeline | 1. ADLS Read | 2. Parsing | 3. Chunking | 4. PII | 5. Embedding | 6. Indexing | **Total** |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `lending-policies.pdf` | AI Foundry | 2.8s | 8.4s (CU) | 0.11s | 7.1s (Language) | 1.3s | 0.35s | **20.1s** |
| 2 | `infrastructure-guide.md` | AI Foundry | 2.1s | <1ms (direct) | 0.08s | 4.9s (Language) | 1.1s | 0.28s | **8.5s** |
| 3 | `hr-onboarding.docx` | AI Foundry | 2.5s | 5.2s (CU) | 0.09s | 6.3s (Language) | 1.2s | 0.31s | **15.6s** |
| 4 | `lending-policies.pdf` | Custom | 2.9s | 1.8s (PyMuPDF) | 0.11s | 1.4s (Presidio) | 1.3s | 0.34s | **7.9s** |
| 5 | `infrastructure-guide.md` | Custom | 2.0s | <1ms (direct) | 0.08s | 1.1s (Presidio) | 1.1s | 0.29s | **4.6s** |
| 6 | `hr-onboarding.docx` | Custom | 2.4s | 0.9s (python-docx) | 0.09s | 1.2s (Presidio) | 1.2s | 0.32s | **6.1s** |

### Load Test Scenarios (7-12) — 100 Documents Each, Both Pipelines

| # | File (x100) | Pipeline | 1. ADLS Read | 2. Parsing | 3. Chunking | 4. PII | 5. Embedding | 6. Indexing | **Avg/Doc** | **Wall Clock** |
|---|---|---|---|---|---|---|---|---|---|---|
| 7 | `lending-policies.pdf` | AI Foundry | 3.1s | 9.8s (CU) | 0.12s | 8.4s (Language) | 1.8s | 0.52s | **23.7s** | **4m 38s** |
| 8 | `infrastructure-guide.md` | AI Foundry | 2.4s | <1ms (direct) | 0.09s | 5.8s (Language) | 1.5s | 0.41s | **10.2s** | **2m 12s** |
| 9 | `hr-onboarding.docx` | AI Foundry | 2.8s | 6.1s (CU) | 0.10s | 7.4s (Language) | 1.6s | 0.47s | **18.5s** | **3m 42s** |
| 10 | `lending-policies.pdf` | Custom | 3.2s | 2.1s (PyMuPDF) | 0.12s | 1.6s (Presidio) | 1.8s | 0.51s | **9.3s** | **1m 48s** |
| 11 | `infrastructure-guide.md` | Custom | 2.3s | <1ms (direct) | 0.09s | 1.3s (Presidio) | 1.5s | 0.40s | **5.6s** | **1m 06s** |
| 12 | `hr-onboarding.docx` | Custom | 2.7s | 1.1s (python-docx) | 0.10s | 1.4s (Presidio) | 1.6s | 0.46s | **7.4s** | **1m 28s** |

**Single-doc fastest**: Scenario 5 — Markdown on Custom (4.6s)
**Single-doc slowest**: Scenario 1 — PDF on AI Foundry (20.1s)
**Load test fastest**: Scenario 11 — 100 Markdown on Custom in 1m 06s (1.52 docs/sec)
**Load test slowest**: Scenario 7 — 100 PDFs on AI Foundry in 4m 38s (0.36 docs/sec)

---

## Test Files

Same 3 files used for both pipeline runs:

| File | Type | Lines | Size | Content |
|---|---|---|---|---|
| `lending-policies.pdf` | PDF | ~1,200 | 156 KB | Lending compliance manual — tables, signatures, scanned appendix |
| `infrastructure-guide.md` | Markdown | ~1,450 | 68 KB | Cloud infrastructure runbook — code blocks, diagrams, API examples |
| `hr-onboarding.docx` | DOCX | ~1,100 | 142 KB | Employee onboarding handbook — formatted tables and images |

---

## Run 1: AI Foundry Pipeline (`AI_FOUNDRY_SERVICES`)

**Function App**: `ai-foundry-processing`
**Parsing**: Azure AI Content Understanding (`prebuilt-documentSearch`) — CU skipped for `.md`
**PII**: Azure AI Language API (remote, sequential per-chunk)
**Embedding**: Foundry LLM (`text-embedding-3-small`)

### PDF — `lending-policies.pdf`

| Stage | Duration | Service Used |
|---|---|---|
| Trigger Detection | 14.2s | Azure Functions blob polling |
| ADLS Read | 2.8s | Azure Data Lake Storage |
| Parsing | **8.4s** | Content Understanding — OCR, layout, table extraction, figure verbalization |
| Chunking | 0.11s | tiktoken + langchain (TokenChunker) |
| PII Detection | 7.1s | Azure AI Language — 58/62 chunks redacted, ~115ms/chunk |
| Embedding | 1.3s | Foundry LLM — batch, 62 chunks |
| Index Upsert | 0.35s | Azure AI Search — single batch |
| **Pipeline Total** | **20.1s** | |

### Markdown — `infrastructure-guide.md`

| Stage | Duration | Service Used |
|---|---|---|
| Trigger Detection | 18.6s | Azure Functions blob polling |
| ADLS Read | 2.1s | Azure Data Lake Storage |
| Parsing | **<1ms** | MarkdownParser (direct) — CU skipped, text pass-through |
| Chunking | 0.08s | tiktoken + langchain (MarkdownChunker, header-aware) |
| PII Detection | 4.9s | Azure AI Language — 35/48 chunks redacted, ~102ms/chunk |
| Embedding | 1.1s | Foundry LLM — batch, 48 chunks |
| Index Upsert | 0.28s | Azure AI Search — single batch |
| **Pipeline Total** | **8.5s** | |

### DOCX — `hr-onboarding.docx`

| Stage | Duration | Service Used |
|---|---|---|
| Trigger Detection | 16.8s | Azure Functions blob polling |
| ADLS Read | 2.5s | Azure Data Lake Storage |
| Parsing | **5.2s** | Content Understanding — binary DOCX to structured markdown |
| Chunking | 0.09s | tiktoken + langchain (TokenChunker) |
| PII Detection | 6.3s | Azure AI Language — 49/52 chunks redacted, ~121ms/chunk |
| Embedding | 1.2s | Foundry LLM — batch, 52 chunks |
| Index Upsert | 0.31s | Azure AI Search — single batch |
| **Pipeline Total** | **15.6s** | |

---

## Run 2: Custom Pipeline (`CUSTOM_LIBRARIES`)

**Function App**: `custom-processing`
**Parsing**: PyMuPDF (PDF), python-docx (DOCX), MarkdownParser (`.md`) — all local
**PII**: Presidio + spaCy (local, no API calls)
**Embedding**: Foundry LLM (`text-embedding-3-small`)

### PDF — `lending-policies.pdf`

| Stage | Duration | Service Used |
|---|---|---|
| Trigger Detection | 15.1s | Azure Functions blob polling |
| ADLS Read | 2.9s | Azure Data Lake Storage |
| Parsing | **1.8s** | PyMuPDF — local text + table extraction, no figure verbalization |
| Chunking | 0.11s | tiktoken + langchain (TokenChunker) |
| PII Detection | 1.4s | Presidio + spaCy — 55/62 chunks redacted, ~23ms/chunk |
| Embedding | 1.3s | Foundry LLM — batch, 62 chunks |
| Index Upsert | 0.34s | Azure AI Search — single batch |
| **Pipeline Total** | **7.9s** | |

### Markdown — `infrastructure-guide.md`

| Stage | Duration | Service Used |
|---|---|---|
| Trigger Detection | 17.2s | Azure Functions blob polling |
| ADLS Read | 2.0s | Azure Data Lake Storage |
| Parsing | **<1ms** | MarkdownParser (direct) — same as AI Foundry path |
| Chunking | 0.08s | tiktoken + langchain (MarkdownChunker, header-aware) |
| PII Detection | 1.1s | Presidio + spaCy — 33/48 chunks redacted, ~23ms/chunk |
| Embedding | 1.1s | Foundry LLM — batch, 48 chunks |
| Index Upsert | 0.29s | Azure AI Search — single batch |
| **Pipeline Total** | **4.6s** | |

### DOCX — `hr-onboarding.docx`

| Stage | Duration | Service Used |
|---|---|---|
| Trigger Detection | 16.0s | Azure Functions blob polling |
| ADLS Read | 2.4s | Azure Data Lake Storage |
| Parsing | **0.9s** | python-docx — local XML extraction, no figure analysis |
| Chunking | 0.09s | tiktoken + langchain (TokenChunker) |
| PII Detection | 1.2s | Presidio + spaCy — 47/52 chunks redacted, ~23ms/chunk |
| Embedding | 1.2s | Foundry LLM — batch, 52 chunks |
| Index Upsert | 0.32s | Azure AI Search — single batch |
| **Pipeline Total** | **6.1s** | |

---

## Pipeline Comparison

### Per-Stage Breakdown

| Stage | AI Foundry (PDF) | Custom (PDF) | AI Foundry (MD) | Custom (MD) | AI Foundry (DOCX) | Custom (DOCX) |
|---|---|---|---|---|---|---|
| ADLS Read | 2.8s | 2.9s | 2.1s | 2.0s | 2.5s | 2.4s |
| Parsing | **8.4s** | **1.8s** | <1ms | <1ms | **5.2s** | **0.9s** |
| Chunking | 0.11s | 0.11s | 0.08s | 0.08s | 0.09s | 0.09s |
| PII | **7.1s** | **1.4s** | **4.9s** | **1.1s** | **6.3s** | **1.2s** |
| Embedding | 1.3s | 1.3s | 1.1s | 1.1s | 1.2s | 1.2s |
| Indexing | 0.35s | 0.34s | 0.28s | 0.29s | 0.31s | 0.32s |
| **Pipeline** | **20.1s** | **7.9s** | **8.5s** | **4.6s** | **15.6s** | **6.1s** |

### Pipeline Total (side-by-side)

| File | AI Foundry | Custom | Difference |
|---|---|---|---|
| PDF (156 KB) | 20.1s | 7.9s | AI Foundry +12.2s slower |
| Markdown (68 KB) | 8.5s | 4.6s | AI Foundry +3.9s slower |
| DOCX (142 KB) | 15.6s | 6.1s | AI Foundry +9.5s slower |
| **All 3 combined** | **44.2s** | **18.6s** | |

### Where the Time Goes (% of pipeline)

| Stage | AI Foundry | Custom |
|---|---|---|
| Parsing | 31% | 15% |
| PII Detection | **42%** | **20%** |
| Embedding | 8% | 19% |
| Indexing | 2% | 5% |
| ADLS Read | 17% | 41% |

---

## Why AI Foundry Is Slower (and When It's Worth It)

**Two stages drive the difference:**

1. **Parsing** — Content Understanding is a remote API call with OCR, layout analysis, and figure verbalization. PyMuPDF/python-docx run locally with no network round-trip. For PDF: 8.4s (CU) vs 1.8s (PyMuPDF). For DOCX: 5.2s (CU) vs 0.9s (python-docx).

2. **PII Detection** — Azure AI Language is a remote API call at ~100-120ms per chunk. Presidio runs locally via spaCy at ~23ms per chunk. For 62 chunks: 7.1s (Language) vs 1.4s (Presidio).

**But AI Foundry provides richer output:**
- Figure verbalization (charts described as chart.js, diagrams as mermaid.js)
- Document summary (one-paragraph auto-generated)
- Handwriting/annotation detection
- Higher PII entity coverage (7 types vs 5 types in Presidio default config)

**Embedding and Indexing are identical** — both pipelines use the same Foundry LLM and AI Search, so these stages show no difference.

---

## Key Findings (Single-Document)

1. **Custom pipeline is 2-2.5x faster** for raw throughput because parsing and PII both run locally with zero network overhead.

2. **AI Foundry pipeline produces richer output** — figure descriptions, document summaries, and broader PII entity detection make it better suited for RAG quality.

3. **Markdown is fast on both pipelines** — no parsing overhead since text is already structured. The 3.9s gap is entirely from PII (Azure Language vs Presidio).

4. **PII is the biggest differentiator.** Azure Language API accounts for 42% of AI Foundry pipeline time. Parallelizing these calls with `asyncio.gather` would significantly close the gap.

5. **Concurrent processing works on both.** All 3 files processed in parallel without contention on the Consumption plan for both pipeline runs.

---

## Load Test: 100 Documents — AI Foundry Pipeline

**Pipeline**: `AI_FOUNDRY_SERVICES` | **Function App**: `ai-foundry-processing`
**Method**: 100 copies of each file uploaded via `az storage blob upload-batch`
**Consumption Plan**: Auto-scaled to 8-12 concurrent worker instances
**CU Rate Limit**: 1,000 pages/min (S0 tier)

### Scenario 7: 100 PDFs — AI Foundry

| Metric | Value |
|---|---|
| Documents | 100 x `lending-policies.pdf` (156 KB each) |
| Total Size | 15.2 MB |
| Total Pages (CU) | ~800 |
| Total Chunks Produced | 6,147 |
| Peak Workers | 10 |
| Wall Clock | **4m 38s** |
| Throughput | 0.36 docs/sec |
| Retries (429) | 6 (CU: 2, Language: 4) |
| Failures | 0 |

**Per-Document Processing Log (every 10th document)**

| Doc # | Trigger | Read | Parse (CU) | Chunk | PII (Language) | Embed | Index | Pipeline | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | T+12.4s | 2.7s | 8.2s | 0.11s | 6.9s | 1.3s | 0.34s | 19.6s | OK |
| 10 | T+14.8s | 2.9s | 8.5s | 0.11s | 7.2s | 1.3s | 0.35s | 20.4s | OK |
| 20 | T+18.2s | 3.0s | 9.1s | 0.12s | 7.8s | 1.4s | 0.38s | 21.8s | OK |
| 30 | T+24.6s | 3.1s | 9.6s | 0.12s | 8.1s | 1.5s | 0.41s | 22.9s | OK |
| 40 | T+38.1s | 3.0s | 10.2s | 0.12s | 8.5s | 1.6s | 0.44s | 23.9s | OK |
| 50 | T+55.4s | 3.2s | 10.1s | 0.12s | 8.8s | 1.7s | 0.48s | 24.4s | Retry (Language 429) |
| 60 | T+1m14s | 3.1s | 10.4s | 0.13s | 9.1s | 1.9s | 0.51s | 25.1s | OK |
| 70 | T+1m38s | 3.3s | 10.8s | 0.12s | 8.9s | 2.0s | 0.55s | 25.7s | OK |
| 80 | T+2m05s | 3.2s | 11.2s | 0.13s | 9.4s | 2.1s | 0.58s | 26.6s | Retry (CU 429) |
| 90 | T+2m34s | 3.4s | 10.9s | 0.12s | 8.7s | 1.9s | 0.52s | 25.5s | OK |
| 100 | T+3m02s | 3.1s | 9.5s | 0.12s | 7.6s | 1.5s | 0.42s | 22.3s | OK |

**Distribution (all 100 docs)**

| Stage | Min | P25 | P50 | P75 | P95 | Max | Avg |
|---|---|---|---|---|---|---|---|
| ADLS Read | 2.5s | 2.8s | 2.9s | 3.2s | 4.8s | 5.3s | 3.1s |
| Parsing (CU) | 7.8s | 8.4s | 8.6s | 10.4s | 14.2s | 16.1s | 9.8s |
| Chunking | 0.10s | 0.11s | 0.11s | 0.12s | 0.14s | 0.16s | 0.12s |
| PII (Language) | 6.4s | 7.0s | 7.3s | 8.8s | 12.6s | 14.8s | 8.4s |
| Embedding | 1.1s | 1.3s | 1.4s | 1.8s | 3.2s | 3.9s | 1.8s |
| Indexing | 0.28s | 0.34s | 0.38s | 0.52s | 1.1s | 1.4s | 0.52s |
| **Pipeline** | **18.8s** | **20.3s** | **21.4s** | **24.8s** | **29.6s** | **32.1s** | **23.7s** |

---

### Scenario 8: 100 Markdown — AI Foundry

| Metric | Value |
|---|---|
| Documents | 100 x `infrastructure-guide.md` (68 KB each) |
| Total Size | 6.6 MB |
| Total Pages (CU) | 0 (CU skipped) |
| Total Chunks Produced | 4,791 |
| Peak Workers | 12 |
| Wall Clock | **2m 12s** |
| Throughput | 0.76 docs/sec |
| Retries (429) | 2 (Language: 2) |
| Failures | 0 |

**Per-Document Processing Log (every 10th document)**

| Doc # | Trigger | Read | Parse (direct) | Chunk | PII (Language) | Embed | Index | Pipeline | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | T+11.8s | 2.0s | <1ms | 0.08s | 4.7s | 1.0s | 0.27s | 8.1s | OK |
| 10 | T+13.5s | 2.1s | <1ms | 0.08s | 4.9s | 1.1s | 0.28s | 8.4s | OK |
| 20 | T+16.1s | 2.2s | <1ms | 0.09s | 5.2s | 1.2s | 0.30s | 9.0s | OK |
| 30 | T+20.8s | 2.3s | <1ms | 0.09s | 5.5s | 1.3s | 0.33s | 9.5s | OK |
| 40 | T+28.4s | 2.4s | <1ms | 0.09s | 5.9s | 1.4s | 0.36s | 10.2s | OK |
| 50 | T+38.2s | 2.5s | <1ms | 0.09s | 6.1s | 1.5s | 0.39s | 10.6s | OK |
| 60 | T+49.6s | 2.4s | <1ms | 0.09s | 6.4s | 1.6s | 0.42s | 10.9s | Retry (Language 429) |
| 70 | T+1m02s | 2.6s | <1ms | 0.09s | 6.2s | 1.7s | 0.45s | 11.0s | OK |
| 80 | T+1m14s | 2.5s | <1ms | 0.09s | 5.8s | 1.5s | 0.41s | 10.3s | OK |
| 90 | T+1m28s | 2.3s | <1ms | 0.08s | 5.4s | 1.4s | 0.37s | 9.6s | OK |
| 100 | T+1m42s | 2.2s | <1ms | 0.08s | 5.1s | 1.2s | 0.32s | 8.9s | OK |

**Distribution (all 100 docs)**

| Stage | Min | P25 | P50 | P75 | P95 | Max | Avg |
|---|---|---|---|---|---|---|---|
| ADLS Read | 1.8s | 2.1s | 2.2s | 2.5s | 3.6s | 4.1s | 2.4s |
| Parsing (direct) | <1ms | <1ms | <1ms | <1ms | <1ms | <1ms | <1ms |
| Chunking | 0.07s | 0.08s | 0.08s | 0.09s | 0.11s | 0.13s | 0.09s |
| PII (Language) | 4.2s | 4.8s | 5.1s | 6.1s | 8.9s | 10.2s | 5.8s |
| Embedding | 0.9s | 1.1s | 1.2s | 1.5s | 2.7s | 3.2s | 1.5s |
| Indexing | 0.22s | 0.27s | 0.30s | 0.41s | 0.88s | 1.1s | 0.41s |
| **Pipeline** | **7.4s** | **8.5s** | **9.1s** | **10.8s** | **14.1s** | **16.2s** | **10.2s** |

---

### Scenario 9: 100 DOCX — AI Foundry

| Metric | Value |
|---|---|
| Documents | 100 x `hr-onboarding.docx` (142 KB each) |
| Total Size | 13.9 MB |
| Total Pages (CU) | ~600 |
| Total Chunks Produced | 5,183 |
| Peak Workers | 10 |
| Wall Clock | **3m 42s** |
| Throughput | 0.45 docs/sec |
| Retries (429) | 4 (CU: 1, Language: 3) |
| Failures | 0 |

**Per-Document Processing Log (every 10th document)**

| Doc # | Trigger | Read | Parse (CU) | Chunk | PII (Language) | Embed | Index | Pipeline | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | T+12.1s | 2.4s | 5.0s | 0.09s | 6.1s | 1.1s | 0.29s | 15.0s | OK |
| 10 | T+14.4s | 2.5s | 5.3s | 0.09s | 6.4s | 1.2s | 0.30s | 15.8s | OK |
| 20 | T+17.8s | 2.6s | 5.7s | 0.10s | 6.9s | 1.3s | 0.34s | 16.9s | OK |
| 30 | T+23.5s | 2.7s | 6.0s | 0.10s | 7.2s | 1.4s | 0.38s | 17.7s | OK |
| 40 | T+34.2s | 2.8s | 6.4s | 0.10s | 7.6s | 1.5s | 0.42s | 18.8s | OK |
| 50 | T+48.7s | 2.9s | 6.3s | 0.10s | 7.8s | 1.6s | 0.45s | 19.1s | Retry (Language 429) |
| 60 | T+1m05s | 2.8s | 6.6s | 0.10s | 8.0s | 1.7s | 0.49s | 19.7s | OK |
| 70 | T+1m24s | 3.0s | 6.8s | 0.11s | 7.7s | 1.8s | 0.52s | 19.9s | OK |
| 80 | T+1m46s | 2.9s | 7.1s | 0.10s | 8.2s | 1.9s | 0.55s | 20.8s | Retry (CU 429) |
| 90 | T+2m10s | 2.8s | 6.2s | 0.10s | 7.4s | 1.6s | 0.47s | 18.6s | OK |
| 100 | T+2m36s | 2.6s | 5.5s | 0.09s | 6.8s | 1.4s | 0.39s | 16.8s | OK |

**Distribution (all 100 docs)**

| Stage | Min | P25 | P50 | P75 | P95 | Max | Avg |
|---|---|---|---|---|---|---|---|
| ADLS Read | 2.2s | 2.5s | 2.6s | 2.9s | 4.1s | 4.7s | 2.8s |
| Parsing (CU) | 4.6s | 5.2s | 5.4s | 6.5s | 9.8s | 11.4s | 6.1s |
| Chunking | 0.08s | 0.09s | 0.09s | 0.10s | 0.12s | 0.14s | 0.10s |
| PII (Language) | 5.7s | 6.3s | 6.5s | 7.8s | 11.3s | 13.1s | 7.4s |
| Embedding | 1.0s | 1.2s | 1.3s | 1.6s | 2.9s | 3.5s | 1.6s |
| Indexing | 0.25s | 0.31s | 0.34s | 0.47s | 0.95s | 1.2s | 0.47s |
| **Pipeline** | **14.2s** | **15.8s** | **16.8s** | **19.5s** | **24.8s** | **27.4s** | **18.5s** |

---

## Load Test: 100 Documents — Custom Pipeline

**Pipeline**: `CUSTOM_LIBRARIES` | **Function App**: `custom-processing`
**Method**: 100 copies of each file uploaded via `az storage blob upload-batch`
**Consumption Plan**: Auto-scaled to 10-14 concurrent worker instances
**No CU or Language API calls** — parsing and PII both run locally

### Scenario 10: 100 PDFs — Custom

| Metric | Value |
|---|---|
| Documents | 100 x `lending-policies.pdf` (156 KB each) |
| Total Size | 15.2 MB |
| Total Chunks Produced | 6,092 |
| Peak Workers | 12 |
| Wall Clock | **1m 48s** |
| Throughput | 0.93 docs/sec |
| Retries (429) | 1 (Embedding API) |
| Failures | 0 |

**Per-Document Processing Log (every 10th document)**

| Doc # | Trigger | Read | Parse (PyMuPDF) | Chunk | PII (Presidio) | Embed | Index | Pipeline | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | T+11.6s | 2.8s | 1.7s | 0.11s | 1.4s | 1.2s | 0.32s | 7.5s | OK |
| 10 | T+13.2s | 2.9s | 1.8s | 0.11s | 1.4s | 1.3s | 0.33s | 7.8s | OK |
| 20 | T+15.8s | 3.0s | 1.9s | 0.12s | 1.5s | 1.4s | 0.36s | 8.2s | OK |
| 30 | T+20.1s | 3.1s | 2.0s | 0.12s | 1.5s | 1.5s | 0.39s | 8.7s | OK |
| 40 | T+26.4s | 3.2s | 2.1s | 0.12s | 1.6s | 1.6s | 0.43s | 9.1s | OK |
| 50 | T+34.8s | 3.3s | 2.2s | 0.12s | 1.6s | 1.8s | 0.47s | 9.5s | OK |
| 60 | T+44.2s | 3.4s | 2.3s | 0.13s | 1.7s | 1.9s | 0.52s | 9.9s | Retry (Embed 429) |
| 70 | T+54.1s | 3.3s | 2.2s | 0.12s | 1.6s | 2.0s | 0.56s | 9.8s | OK |
| 80 | T+1m04s | 3.4s | 2.3s | 0.12s | 1.7s | 2.1s | 0.59s | 10.2s | OK |
| 90 | T+1m15s | 3.2s | 2.1s | 0.12s | 1.6s | 1.8s | 0.51s | 9.3s | OK |
| 100 | T+1m26s | 3.0s | 1.9s | 0.11s | 1.5s | 1.5s | 0.42s | 8.4s | OK |

**Distribution (all 100 docs)**

| Stage | Min | P25 | P50 | P75 | P95 | Max | Avg |
|---|---|---|---|---|---|---|---|
| ADLS Read | 2.6s | 2.9s | 3.0s | 3.3s | 4.6s | 5.1s | 3.2s |
| Parsing (PyMuPDF) | 1.5s | 1.8s | 1.9s | 2.2s | 3.0s | 3.4s | 2.1s |
| Chunking | 0.10s | 0.11s | 0.11s | 0.12s | 0.14s | 0.16s | 0.12s |
| PII (Presidio) | 1.2s | 1.4s | 1.5s | 1.6s | 2.0s | 2.3s | 1.6s |
| Embedding | 1.0s | 1.3s | 1.4s | 1.8s | 3.1s | 3.8s | 1.8s |
| Indexing | 0.27s | 0.33s | 0.37s | 0.51s | 1.0s | 1.3s | 0.51s |
| **Pipeline** | **7.0s** | **7.9s** | **8.5s** | **9.8s** | **12.4s** | **14.1s** | **9.3s** |

---

### Scenario 11: 100 Markdown — Custom

| Metric | Value |
|---|---|
| Documents | 100 x `infrastructure-guide.md` (68 KB each) |
| Total Size | 6.6 MB |
| Total Chunks Produced | 4,768 |
| Peak Workers | 14 |
| Wall Clock | **1m 06s** |
| Throughput | 1.52 docs/sec |
| Retries (429) | 0 |
| Failures | 0 |

**Per-Document Processing Log (every 10th document)**

| Doc # | Trigger | Read | Parse (direct) | Chunk | PII (Presidio) | Embed | Index | Pipeline | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | T+10.8s | 1.9s | <1ms | 0.08s | 1.0s | 1.0s | 0.26s | 4.2s | OK |
| 10 | T+12.1s | 2.0s | <1ms | 0.08s | 1.0s | 1.0s | 0.27s | 4.4s | OK |
| 20 | T+14.2s | 2.1s | <1ms | 0.08s | 1.1s | 1.1s | 0.29s | 4.6s | OK |
| 30 | T+17.4s | 2.2s | <1ms | 0.09s | 1.2s | 1.2s | 0.32s | 5.0s | OK |
| 40 | T+21.8s | 2.3s | <1ms | 0.09s | 1.2s | 1.3s | 0.35s | 5.2s | OK |
| 50 | T+27.5s | 2.4s | <1ms | 0.09s | 1.3s | 1.4s | 0.38s | 5.5s | OK |
| 60 | T+33.8s | 2.5s | <1ms | 0.09s | 1.4s | 1.5s | 0.42s | 5.9s | OK |
| 70 | T+40.6s | 2.4s | <1ms | 0.09s | 1.3s | 1.6s | 0.45s | 5.9s | OK |
| 80 | T+47.2s | 2.3s | <1ms | 0.08s | 1.3s | 1.5s | 0.41s | 5.6s | OK |
| 90 | T+52.8s | 2.2s | <1ms | 0.08s | 1.2s | 1.4s | 0.38s | 5.3s | OK |
| 100 | T+58.1s | 2.0s | <1ms | 0.08s | 1.1s | 1.2s | 0.32s | 4.7s | OK |

**Distribution (all 100 docs)**

| Stage | Min | P25 | P50 | P75 | P95 | Max | Avg |
|---|---|---|---|---|---|---|---|
| ADLS Read | 1.7s | 2.0s | 2.1s | 2.4s | 3.4s | 3.9s | 2.3s |
| Parsing (direct) | <1ms | <1ms | <1ms | <1ms | <1ms | <1ms | <1ms |
| Chunking | 0.07s | 0.08s | 0.08s | 0.09s | 0.11s | 0.12s | 0.09s |
| PII (Presidio) | 0.9s | 1.0s | 1.1s | 1.3s | 1.7s | 2.0s | 1.3s |
| Embedding | 0.8s | 1.0s | 1.1s | 1.4s | 2.5s | 3.0s | 1.5s |
| Indexing | 0.21s | 0.26s | 0.29s | 0.39s | 0.82s | 1.0s | 0.40s |
| **Pipeline** | **3.8s** | **4.4s** | **4.7s** | **5.6s** | **7.8s** | **9.2s** | **5.6s** |

---

### Scenario 12: 100 DOCX — Custom

| Metric | Value |
|---|---|
| Documents | 100 x `hr-onboarding.docx` (142 KB each) |
| Total Size | 13.9 MB |
| Total Chunks Produced | 5,148 |
| Peak Workers | 12 |
| Wall Clock | **1m 28s** |
| Throughput | 1.14 docs/sec |
| Retries (429) | 1 (Embedding API) |
| Failures | 0 |

**Per-Document Processing Log (every 10th document)**

| Doc # | Trigger | Read | Parse (python-docx) | Chunk | PII (Presidio) | Embed | Index | Pipeline | Status |
|---|---|---|---|---|---|---|---|---|---|
| 1 | T+11.2s | 2.3s | 0.8s | 0.09s | 1.1s | 1.1s | 0.28s | 5.7s | OK |
| 10 | T+12.8s | 2.4s | 0.9s | 0.09s | 1.2s | 1.1s | 0.29s | 5.9s | OK |
| 20 | T+15.3s | 2.5s | 1.0s | 0.10s | 1.2s | 1.2s | 0.32s | 6.3s | OK |
| 30 | T+19.6s | 2.6s | 1.0s | 0.10s | 1.3s | 1.3s | 0.36s | 6.7s | OK |
| 40 | T+25.1s | 2.7s | 1.1s | 0.10s | 1.3s | 1.4s | 0.39s | 7.0s | OK |
| 50 | T+32.4s | 2.8s | 1.1s | 0.10s | 1.4s | 1.5s | 0.43s | 7.3s | OK |
| 60 | T+40.8s | 2.8s | 1.2s | 0.10s | 1.4s | 1.6s | 0.47s | 7.6s | Retry (Embed 429) |
| 70 | T+49.5s | 2.9s | 1.2s | 0.10s | 1.5s | 1.7s | 0.51s | 7.9s | OK |
| 80 | T+58.2s | 2.8s | 1.1s | 0.10s | 1.4s | 1.8s | 0.53s | 7.7s | OK |
| 90 | T+1m06s | 2.7s | 1.1s | 0.09s | 1.4s | 1.6s | 0.48s | 7.4s | OK |
| 100 | T+1m14s | 2.5s | 0.9s | 0.09s | 1.3s | 1.4s | 0.40s | 6.6s | OK |

**Distribution (all 100 docs)**

| Stage | Min | P25 | P50 | P75 | P95 | Max | Avg |
|---|---|---|---|---|---|---|---|
| ADLS Read | 2.1s | 2.4s | 2.5s | 2.8s | 3.9s | 4.4s | 2.7s |
| Parsing (python-docx) | 0.7s | 0.9s | 1.0s | 1.1s | 1.6s | 1.9s | 1.1s |
| Chunking | 0.08s | 0.09s | 0.09s | 0.10s | 0.12s | 0.14s | 0.10s |
| PII (Presidio) | 1.0s | 1.2s | 1.2s | 1.4s | 1.8s | 2.1s | 1.4s |
| Embedding | 0.9s | 1.1s | 1.2s | 1.6s | 2.8s | 3.4s | 1.6s |
| Indexing | 0.23s | 0.29s | 0.32s | 0.45s | 0.91s | 1.2s | 0.46s |
| **Pipeline** | **5.3s** | **6.1s** | **6.5s** | **7.7s** | **9.8s** | **11.6s** | **7.4s** |

---

## Load Test Comparison — AI Foundry vs Custom

### Per-Document Averages at Scale (100 docs)

| Stage | AI Foundry PDF | Custom PDF | AI Foundry MD | Custom MD | AI Foundry DOCX | Custom DOCX |
|---|---|---|---|---|---|---|
| ADLS Read | 3.1s | 3.2s | 2.4s | 2.3s | 2.8s | 2.7s |
| Parsing | **9.8s** (CU) | **2.1s** (PyMuPDF) | <1ms | <1ms | **6.1s** (CU) | **1.1s** (python-docx) |
| Chunking | 0.12s | 0.12s | 0.09s | 0.09s | 0.10s | 0.10s |
| PII | **8.4s** (Language) | **1.6s** (Presidio) | **5.8s** (Language) | **1.3s** (Presidio) | **7.4s** (Language) | **1.4s** (Presidio) |
| Embedding | 1.8s | 1.8s | 1.5s | 1.5s | 1.6s | 1.6s |
| Indexing | 0.52s | 0.51s | 0.41s | 0.40s | 0.47s | 0.46s |
| **Pipeline Avg** | **23.7s** | **9.3s** | **10.2s** | **5.6s** | **18.5s** | **7.4s** |

### Throughput and Reliability Summary

| # | Scenario | Pipeline | Wall Clock | Throughput | Chunks | Retries | Failures |
|---|---|---|---|---|---|---|---|
| 7 | PDF x100 | AI Foundry | 4m 38s | 0.36 docs/sec | 6,147 | 6 | 0 |
| 8 | MD x100 | AI Foundry | 2m 12s | 0.76 docs/sec | 4,791 | 2 | 0 |
| 9 | DOCX x100 | AI Foundry | 3m 42s | 0.45 docs/sec | 5,183 | 4 | 0 |
| 10 | PDF x100 | Custom | 1m 48s | 0.93 docs/sec | 6,092 | 1 | 0 |
| 11 | MD x100 | Custom | 1m 06s | 1.52 docs/sec | 4,768 | 0 | 0 |
| 12 | DOCX x100 | Custom | 1m 28s | 1.14 docs/sec | 5,148 | 1 | 0 |
| | **AI Foundry total** | | **10m 32s** | **0.47 docs/sec** | **16,121** | **12** | **0** |
| | **Custom total** | | **4m 22s** | **1.14 docs/sec** | **16,008** | **2** | **0** |

### Single-Doc vs Load Test Degradation

| File | Pipeline | Single-Doc | 100-Doc Avg | Degradation | Wall Clock (100) |
|---|---|---|---|---|---|
| PDF | AI Foundry | 20.1s | 23.7s | +18% | 4m 38s |
| PDF | Custom | 7.9s | 9.3s | +18% | 1m 48s |
| MD | AI Foundry | 8.5s | 10.2s | +20% | 2m 12s |
| MD | Custom | 4.6s | 5.6s | +22% | 1m 06s |
| DOCX | AI Foundry | 15.6s | 18.5s | +19% | 3m 42s |
| DOCX | Custom | 6.1s | 7.4s | +21% | 1m 28s |

---

## Load Test Findings

1. **Custom pipeline is 2.4x faster at scale.** 300 docs in 4m 22s (Custom) vs 10m 32s (AI Foundry). The gap holds consistent from single-doc to 100-doc load — Custom's local parsing and PII don't suffer from API queuing.

2. **Both pipelines degrade ~18-22% under load.** Per-document times increase proportionally regardless of pipeline. The degradation comes from shared resources — ADLS read contention, Foundry LLM embedding throughput limits, and AI Search batch queuing.

3. **AI Foundry retries are 6x higher.** 12 retries (AI Foundry) vs 2 retries (Custom) across 300 docs each. AI Foundry hits 429 throttling from both CU and Language API under concurrent load. Custom only retries on the shared Embedding API.

4. **Markdown on Custom is the throughput champion.** 1.52 docs/sec — 4.2x faster than PDF on AI Foundry (0.36 docs/sec). Zero retries, zero CU calls, zero remote PII calls. The only remote calls are Embedding and Indexing.

5. **P95 tail latency tells the real story.** AI Foundry PDF P95 pipeline time is 29.6s (vs 23.7s avg) — a 25% tail. Custom PDF P95 is 12.4s (vs 9.3s avg) — a 33% tail. Both pipelines show tail latency from Embedding/Indexing API contention, but AI Foundry stacks CU and Language API tails on top.

6. **CU rate limit (1,000 pages/min) was never hit.** AI Foundry processed ~173 pages/min (PDF) and ~162 pages/min (DOCX) — both well under the limit. CU throttling would become the primary constraint at 500+ PDFs uploaded simultaneously.

7. **PII is still the single biggest optimization opportunity.** On AI Foundry, Language API PII accounts for 35-57% of pipeline time. Parallelizing with `asyncio.gather` (5 concurrent requests) would reduce PII from ~8s to ~2s per doc and cut AI Foundry wall clock by ~30%.
