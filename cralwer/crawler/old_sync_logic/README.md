# Old Synchronous Crawler Logic (ARCHIVED)

**Status:** DEPRECATED - Kept for reference/emergency fallback only.

This folder contains the original sequential single-threaded crawler implementation. It has been replaced by the **async engine** for production use.

## Files

| File | Purpose | Notes |
|------|---------|-------|
| `pipeline.py` | Sync job orchestration | Processes one URL at a time per job |
| `harvest.py` | Sequential URL fetcher | Fetch → Parse → Enqueue loop |
| `interaction.py` | Sync form fill/pagination | See `interaction_async.py` for production |

## Why Archived?

**Async Engine Benefits:**
- **8x faster:** 8 browsers × 12 tabs = 96 concurrent pages vs 1 sequential page
- **Production-ready:** Built-in per-host politeness (HostLimiter) + circuit breaker
- **Scalable:** Can handle 1000s of jobs in parallel with controlled resource use

**Sync Engine Limitations:**
- ~1 core, ~250MB per job → bottleneck for batch crawls
- No built-in politeness control (hardcoded 2s delay)
- No circuit breaker → can hammer dead hosts
- Complex ThreadPoolExecutor management for batches

## If You Need Sync Again

**Why it might be needed:**
- Debugging complex async issues
- Testing single job in isolation
- Emergency fallback if async has critical bug

**To restore:**

1. **Move files back:**
   ```bash
   mv old_sync_logic/{pipeline.py,harvest.py,interaction.py} .
   ```

2. **Restore app.py:**
   ```python
   # Re-add to imports
   from crawler.pipeline import run_job
   from crawler.dedup import CrawlHistory
   from crawler.ingest_client import CollectingIngestClient, HttpIngestClient
   
   # Restore _run and _run_one functions (see git history)
   
   # Restore sync paths in /v1/crawl and /v1/crawl/batch endpoints
   ```

3. **Unset async engine:**
   ```bash
   unset CRAWLER_ASYNC_ENGINE  # or set to "0"
   ```

## When to Delete Completely

After async engine is **stable in production for 3+ months** with no fallback needs:

```bash
rm -rf old_sync_logic/
# Also remove from git history for cleaner repo
```

## Comparison

### Sync Flow
```
Job 1: ████████████████ (30s)
Job 2:                 ████████████████ (30s)
Job 3:                                   ████████████████ (30s)
────────────────────────────────────────────────────────── Total: 90s
```

### Async Flow
```
Job 1 URLs: ███████ (10s, 96 parallel)
Job 2 URLs: ███████ (10s, 96 parallel)
Job 3 URLs: ███████ (10s, 96 parallel)
──────────────────────────────────────── Total: ~15s (I/O overlapped)
```

## Code Organization Post-Cleanup

```
crawler/
├── async_engine.py          ← Main production engine
├── fetcher.py               ← HTTP client (used by both)
├── extract.py               ← Document builder
├── gate.py                  ← Content filter
├── dedup.py                 ← History tracking
├── models.py                ← Data structures
├── seed.py                  ← Watchlist loader
├── parse.py                 ← HTML parser
├── resolver.py              ← Entity matcher
├── robots.py                ← robots.txt handler
├── interaction_async.py     ← Form fill (async)
├── errors.py                ← Error handling
├── old_sync_logic/          ← ARCHIVED
│   ├── __init__.py
│   ├── pipeline.py
│   ├── harvest.py
│   ├── interaction.py
│   └── README.md
└── ...
```

---

**Last archived:** 2026-07-10  
**Reason:** Moving to async-only production  
**Decision made by:** Claude Code (Ponytail mode - YAGNI)
