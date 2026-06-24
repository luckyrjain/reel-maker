# Context Evaluation & Enrichment — Design Spec

**Date:** 2026-06-24
**Status:** Approved

## Problem

The generation pipeline evaluates output quality (post-generation) but places no quality gate on the input. A thin or vague context — few named entities, no narrative tension, no hook angle — produces weak guides regardless of how many generation retries are attempted. Fixing the output without improving the input is treating symptoms.

## Goal

Before guide generation begins, evaluate the user's submitted context for video engagement potential. If it falls below a quality threshold, silently enrich it with an LLM call, store the enriched version separately, and feed that into generation. The original input is always preserved.

## Architecture

```
POST /api/reels
  └─► Reel(status: pending) + Cuts + Job(enrich_context)
  └─► enqueue enrich_context
  └─► return reel page polling /api/reels/{id}/active-job-fragment

enrich_context task
  ├─ evaluate_context(reel.context) → (score, issues)   [rule-based]
  ├─ if score < 60: llm_enrich() → enriched_text
  ├─ reel.enriched_context = enriched_text
  ├─ job.meta["context_score"], ["context_issues"], ["enriched"]
  ├─ transition reel: enriching → generating
  └─ create Job(generate_guide) + enqueue

generate_guide task
  └─ effective_context = reel.enriched_context or reel.context
     (all generation, enrichment, scoring use effective_context)

/api/reels/{id}/active-job-fragment
  └─ returns job_status.html for latest pending/running Job on this reel
     falls back to latest job fragment when none active
```

## New Files

### `engine/generation/context_enricher.py`

Two public functions:

**`evaluate_context(context: str) -> tuple[int, list[str]]`**

Rule-based scorer, 5 axes × 20 points = 100 max. Fast, deterministic, no LLM call.

| Axis | Signal | Scoring |
|------|--------|---------|
| Length | Word count | <50 = 0, 50–100 = 10, >100 = 20 |
| Specificity | Capitalised multi-word names, numbers, dates, percentages | 0 named entities = 0, 1–2 = 10, 3+ = 20 |
| Stakes / tension | Conflict markers: "but", "despite", "however", "risk", "challenge", "collapse", "question", "despite", "threat" | 0 = 0, 1 = 10, 2+ = 20 |
| Narrative arc | Setup + development + open question/resolution. Checks for discourse connectors ("first", "then", "but", "finally") and sentence variety | Heuristic 0/10/20 |
| Hook potential | First sentence: direct question, direct address ("you", "imagine", "here's"), or bold claim (number + superlative) | Heuristic 0/10/20 |

Returns `(combined_score, [issue_strings])` — issues are human-readable labels used for `job.meta["context_issues"]` and observability only (never shown to the user).

**`llm_enrich(context: str, niche: str, llm) -> str | None`**

Single LLM call. System prompt instructs the model to: add specific details, stakes, tension, and a hook angle; preserve all original facts; return enriched context only; stay under 1200 words. Returns the enriched string, or `None` on any exception. Caller is responsible for `record_stage` wrapping.

Threshold: score < 60 triggers enrichment.

### `worker/tasks/enrich_context.py`

Celery task following the same structural conventions as `generate_guide`:

```
enrich_context(job_id):
  guard: job missing → return
  guard: job.status in (done, running) → return   [idempotency]

  transition reel: pending → enriching
  job.status = running; heartbeat(10)

  score, issues = evaluate_context(reel.context)
  job.meta = {"context_score": score, "context_issues": issues}
  db.commit()

  if score < 60:
    heartbeat(40)
    with record_stage(db, reel.id, "context_enrich",
                      provider="nvidia" if settings.nvidia_api_key else "ollama"):
      enriched = llm_enrich(reel.context, reel.niche, llm)
    if enriched:
      reel.enriched_context = enriched
      job.meta["enriched"] = True
      db.commit()
    else:
      log.warning("context enrichment failed, proceeding with original")

  heartbeat(80)
  transition reel: enriching → generating

  new_job = Job(reel_id=reel.id, status=pending)
  db.add(new_job); db.commit()
  generate_guide.delay(new_job.id)

  job.status = done; heartbeat(100)

  on exception:
    rollback; job.status = failed; job.error = str(exc)
    transition reel: enriching → failed (best-effort)
```

Failure behaviour: enrichment failure (LLM call exception) is non-fatal — the task falls through, logs a warning, and proceeds to enqueue `generate_guide` with the original context. Only a hard exception (DB failure, transition error) marks the job as failed and stops the pipeline.

## Modified Files

### `api/models.py`
- Add `enriching` to `ReelStatus` enum
- Add `Reel.enriched_context: Mapped[str | None]` (Text, nullable)

### `api/state.py`
Add two new transitions to `REEL_TRANSITIONS`:
```python
"enriching":   {"generating", "failed"},
"pending":     {"enriching", "generating"},   # pending already exists; add "enriching"
```

### `ui/templates/reel.html`
Update the HTMX polling target from the specific job fragment URL to `/api/reels/{id}/active-job-fragment` so the page automatically tracks whichever job (enrich or generate) is currently active.

### `api/routers/reels.py`
- `POST /api/reels`: enqueue `enrich_context` instead of `generate_guide`
- New endpoint `GET /api/reels/{id}/active-job-fragment`:
  - Query Jobs for reel ordered by `created_at desc`
  - Return `job_status.html` fragment for the first job with status `pending` or `running`
  - If none active, return fragment for the most recent job (shows done/failed state)
  - Reuses existing `job_status.html` template unchanged

### `worker/tasks/generate.py`
One-line change at task entry:
```python
effective_context = reel.enriched_context or reel.context
```
Replace all uses of `reel.context` in generation, enrichment scoring, and quality evaluation with `effective_context`.

### `worker/celery_app.py`
Route `enrich_context` to the `generation` queue (I/O-bound, existing concurrency=4). No new queue.

### `migrations/versions/0003_context_enrichment.py`
- Add `reels.enriched_context` TEXT NULL
- Add `'enriching'` to `reel_status` enum

## Data Flow Summary

```
reel.context          → always the raw user input (never modified)
reel.enriched_context → LLM-enriched version, set only when score < 60 and enrichment succeeds
effective_context     → enriched_context if set, else context (used only inside generate_guide)

job.meta (enrich job):
  context_score:   int          # 0–100 rule score
  context_issues:  list[str]    # human-readable axis failures
  enriched:        bool         # True if LLM enrichment ran and succeeded
```

## State Machine

```
pending → enriching → generating → guide_ready → ...
                    ↘ failed
```

The existing `pending → generating` direct transition is preserved for any code path that bypasses enrichment (e.g. direct task injection in tests).

## Queue Routing

| Task | Queue | Concurrency |
|------|-------|-------------|
| `enrich_context` | `generation` | 4 (existing) |
| `generate_guide` | `generation` | 4 (existing) |
| `render_cut` | `rendering` | 1 (existing) |

## Testing

- `tests/test_context_enricher.py` — unit tests for `evaluate_context`: each axis scores correctly at boundary values; combined score; issue strings returned on failure
- `tests/test_enrich_context_task.py` — task-level tests with mock LLM: enrichment runs when score < 60; skipped when score ≥ 60; graceful fallback when LLM fails; `generate_guide` always enqueued on success; idempotency guard
- Existing 68 tests must continue to pass

## Out of Scope

- Showing the enriched context diff to the user (Phase later)
- Allowing the user to reject enrichment (Phase later)
- Per-niche enrichment tuning (the enricher is generic by design)
