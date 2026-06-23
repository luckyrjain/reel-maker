# Generate Pipeline Improvements — Design Spec
**Date:** 2026-06-23  
**Scope:** Option B — targeted fixes + per-beat word budget prompt rewrite  
**Files touched:** `engine/generation/prompt.py`, `worker/tasks/generate.py`, `ui/templates/fragments/job_status.html`

---

## Problem Summary

Seven issues identified across the full generate pipeline via code review. All are correctness or quality problems — no architectural changes required.

---

## Section 1 — Prompt Changes

### 1a. Per-beat word budget

**File:** `engine/generation/prompt.py` → `build_messages()`

The system prompt currently says `~2.5 words/sec` and `"An 8-second beat needs at least 15 words"`. Both are wrong: `8s × 2.5 = 20`, not 15. The evaluator's `MAX_WPS` is 4.0, meaning beats at 2.5 wps create silent gaps. The measured failure in test reels was beats at 5–7 wps (too many words) caused by the LLM ignoring the under-specified guidance.

**Change:** Update system prompt to `~3 words/sec`. Add an explicit per-beat word budget line to the field rules in the user message:

```
Beat.vo_script: write ~3 words per second of duration_s.
  A 2.5s beat → ~7 words. A 8s beat → ~24 words. A 10s beat → ~30 words.
  Under-filling beats creates dead air. Over-filling causes audio cut-off.
```

No changes to `build_visuals_messages` — it doesn't control word count.

### 1b. Filter internal scoring lines from feedback

**File:** `worker/tasks/generate.py` → standard LLM retry loop

`last_issues` from `score_guide()` includes a terminal line: `"Score breakdown — retention:−11 insight:−9..."`. This is passed verbatim to the LLM as `prior_feedback`. The LLM does not understand this internal format.

**Change:** Strip any line starting with `"Score breakdown"` before passing as feedback:

```python
feedback = [i for i in last_issues if not i.startswith("Score breakdown")]
```

### 1c. Context truncation in `build_messages`

**File:** `engine/generation/prompt.py` → `build_messages()`

Context is embedded in the user message with no length cap. A large paste (5,000+ words) can exceed context windows or degrade output quality.

**Change:** Truncate context to 2,000 characters before embedding. Append a note when truncated so the LLM knows the source was cut:

```python
ctx = context[:2000]
if len(context) > 2000:
    ctx += "\n[context truncated]"
```

The structured path's sub-functions already have their own caps (800 chars for `_enrich_batch`, 1,200 for `_make_conflict_stub`) — these are unchanged.

---

## Section 2 — Scoring & Evaluation

### 2a. Move quality score out of `job.error`

**File:** `worker/tasks/generate.py` → `generate_guide()` success path  
**File:** `ui/templates/fragments/job_status.html`

`job.error = f"quality:{last_score}"` on successful generation is misleading — monitoring tools treat any non-null `error` as a failure.

**Change:** Write the score to `job.meta` and clear `job.error` on success:

```python
job.meta = {**(job.meta or {}), "quality_score": last_score}
job.error = None
```

Update `job_status.html` to read `job.meta.quality_score` for the score badge instead of parsing `job.error`.

### 2b. Quality gate for the structured path

**File:** `worker/tasks/generate.py` → `generate_guide()` structured block

The structured path currently accepts any guide regardless of score. The standard path retries 3× if score < 80. A structured script that produces a low score (e.g. enrichment failed silently, context was malformed) is committed with no second chance.

**Change:** After evaluating the structured guide, if `last_score < QUALITY_THRESHOLD`, fall through to the standard path:

```python
if last_score < QUALITY_THRESHOLD:
    guide = None  # triggers standard LLM path below
    job.meta = {**(job.meta or {}), "structured_score": last_score, "structured_fallback": True}
```

The standard path already tracks `best_guide` and accepts best-of-3, so the structured guide is still available as `best_guide` for comparison if needed. (It isn't passed through currently — the fallback simply runs fresh. That is acceptable: if structured scored poorly, the standard path will likely do better.)

---

## Section 3 — Structured-Script Path

### 3a. Fix pre-enrichment shallow-beat metric

**File:** `worker/tasks/generate.py` → `_generate_from_structured_script()`

`ev.detail["shallow_beats"]` is measured *after* `_enrich_with_insight` mutates the stubs, so it always records near-zero. The useful metric is how many went in shallow and how many came out still shallow.

**Change:** Capture count before and after:

```python
shallow_before = sum(1 for s in stubs if _is_shallow_beat(s))
_enrich_with_insight(stubs, reel.context, enrichment_llm)
ev.detail["shallow_beats"] = shallow_before
ev.detail["enriched_beats"] = shallow_before - sum(1 for s in stubs if _is_shallow_beat(s))
```

### 3b. LLM-generated caption and hashtags

**File:** `worker/tasks/generate.py` → `_generate_from_structured_script()`

The structured path hardcodes captions and hashtags:
```python
caption = f"{niche.title()} breakdown — who makes the cut? #football #{niche_tag}"
```
This is always identical regardless of script content. The standard path asks the LLM to generate these from the actual content — the structured path should too.

**Change:** Add a lightweight LLM call at the end of `_generate_from_structured_script` using `enrichment_llm`. Pass the first 600 chars of combined VO scripts as context. Return `{"caption": "...", "hashtags": [...]}`.

Prompt:
```
You write social media copy for sports short-form videos.
Given this voiceover script excerpt, write:
1. A caption: 1-2 punchy sentences, no hashtags, max 150 chars, hooks the viewer
2. Exactly 15 hashtags: no # prefix, mix of 5 broad / 5 niche / 5 trending

Return ONLY JSON: {"caption": "...", "hashtags": ["tag1", ...]}

SCRIPT:
{combined_vo[:600]}
NICHE: {niche}
```

If the call fails or returns invalid JSON, fall back to the existing hardcoded template — zero risk to existing behaviour.

---

## Data Flow (unchanged)

```
context → parse() → structured? ──yes──► _generate_from_structured_script()
                                              ├── _enrich_with_insight()      [enrichment LLM]
                                              ├── _make_conflict_stub()        [enrichment LLM]  
                                              ├── build_visuals_messages()     [main LLM]
                                              ├── _generate_caption_hashtags() [enrichment LLM] ← NEW
                                              └── score + quality gate         ← NEW
                          │
                          no
                          ▼
                    build_messages() [3 words/sec budget] ← UPDATED
                    llm.complete() × up to 3
                    feedback loop [Score breakdown filtered] ← UPDATED
                    best-of-3 accept
                          │
                          ▼
                    job.meta["quality_score"] ← UPDATED (was job.error)
```

---

## Testing

- Existing 68 tests should all pass (no schema changes, no removed behaviour)
- Manually re-run a reel generation and verify:
  - `job.meta` contains `quality_score` (not `job.error`)
  - Beat word counts are closer to 3 wps
  - Structured path caption reflects actual script content
  - `StageEvent.detail["shallow_beats"]` shows pre-enrichment count

---

## Out of Scope

- Option C (structured path retry loop) — deferred
- Music mixing, publishing, analytics — Phase 4/5
- Changing the rule scorer axes or weights
