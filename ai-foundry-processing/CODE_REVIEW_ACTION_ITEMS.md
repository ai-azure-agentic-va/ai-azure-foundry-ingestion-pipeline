# Python Code Review Action Items

**Date:** 2026-03-29
**Reviewer:** Claude Code
**Status:** Approve with warnings

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 0 |
| HIGH | 2 |
| MEDIUM | 5 |
| LOW | 2 |

---

## Action Items

### HIGH Priority

- [ ] **Fix ambiguous variable name `l`**
  - **File:** `modules/parsers/markdown_parser.py:156-158`
  - **Issue:** Using `l` as variable name is ambiguous (looks like `1`)
  - **Fix:** Rename to `old_level` and `lvl`
  ```python
  # Before
  for l in [k for k in hierarchy if k > level]:
      del hierarchy[l]
  context_path = " > ".join(hierarchy[l] for l in sorted(hierarchy))

  # After
  for old_level in [k for k in hierarchy if k > level]:
      del hierarchy[old_level]
  context_path = " > ".join(hierarchy[lvl] for lvl in sorted(hierarchy))
  ```

- [ ] **Refactor global mutable state pattern**
  - **File:** `modules/foundry_pii_scanner.py:12-38`
  - **Issue:** Global `_text_client` with lazy init creates shared mutable state
  - **Risk:** Thread-safety issues under concurrent invocations in serverless
  - **Fix:** Move client initialization into `FoundryPiiScanner.__init__` or use thread-safe singleton

---

### MEDIUM Priority

- [ ] **Replace bare exception in SearchPusher**
  - **File:** `modules/search_pusher.py:138`
  - **Issue:** Catching broad `Exception` when checking index existence
  - **Fix:** Catch `azure.core.exceptions.ResourceNotFoundError` specifically
  ```python
  from azure.core.exceptions import ResourceNotFoundError

  except ResourceNotFoundError:
      logger.info(f"[SearchPusher] Index '{self.index_name}' not found — creating...")
  ```

- [ ] **Replace bare exception in AdlsReader**
  - **File:** `modules/adls_reader.py:89`
  - **Issue:** `except Exception: return {}` hides real errors
  - **Fix:** Catch `ResourceNotFoundError` to distinguish "not found" from failures

- [ ] **Add missing return type hints**
  - `modules/chunker.py:143` - `get_chunker()` -> `TokenChunker | MarkdownChunker`
  - `modules/foundry_parser.py:64` - `_get_client()` -> `ContentUnderstandingClient`
  - `modules/foundry_parser.py:84` - `parse()` -> `ParseResult`



---

### MISSING Feature

- [ ] **Track TODO items in issue tracker**
  - **File:** `modules/chunker.py:151-156`
  - `SheetChunker` for xlsx row/sheet-based splitting
  - `SemanticChunker` for page-boundary-aware splitting

---

## Formatting Required

Run these commands to fix formatting issues:

```bash
black .
ruff check --fix .
```

**Files needing reformatting:** 15

---

## Static Analysis Tools

| Tool | Status | Action |
|------|--------|--------|
| ruff | 2 errors | Run `ruff check --fix .` |
| black | 15 files need formatting | Run `black .` |
| mypy | not installed | Consider adding to dev dependencies |
| bandit | not installed | Consider adding for security scanning |

---

## Positive Observations

- Good use of type hints on most public interfaces
- Proper use of context managers
- Good defensive coding (null checks, fallback handling)
- Lazy loading for cold start optimization
- Domain allowlist for PII avoids false positives
- Idempotent upserts via `merge_or_upload_documents`
