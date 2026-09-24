# Design spec: quality↔engagement correlation + performance-informed feedback

**Status:** ready for implementation
**Closes:** the two remaining Phase 5 items in `docs/roadmap.md` / `docs/product-gap-analysis-and-roadmap-2026-08.md`:
> - Correlate `quality_score` against real engagement — this is the first time the scorer gets any ground truth at all
> - Feed high/low performers back into prompt `prior_feedback` and evaluator axis weights

**Author's note on scope:** the second bullet, read literally ("feed high/low performers back... automatically"), is a trap for this codebase specifically. Section 3 explains why, in detail, before proposing the version that's actually being built.

---

## 1. Current state (facts, with citations)

- `Job.meta["quality_score"]` (int, 0–100) is written once per successful `generate_guide` run, at a single shared line reached by **both** the structured and standard paths (`worker/tasks/generate.py:530`). There is no other place quality is recorded. This shared line matters for §3.5 — a naive implementation that only prepares its data inside the standard-path branch will `NameError` on every structured-path success.
- `Cut.views` / `.likes` / `.comments` / `.metrics_updated_at` are populated per-cut, independently, every 6h by `worker/tasks/metrics.py::pull_publish_metrics()` — only for cuts with `status == published` and a `platform_post_id`. `None` until the first successful pull for that cut.
- The *only* place quality and engagement are ever shown together today is `api/routers/reels.py::_reel_list_metrics()` (lines 97–123), which the reel-list page (`ui/templates/reels_list.html`) renders as two plain numeric columns, one row per reel, **no computed relationship between them** — an operator has to eyeball two columns across dozens of rows.
- `_reel_list_metrics()`'s quality-score lookup ("last non-null `quality_score` among a reel's jobs, ascending by `created_at`, so the latest overwrites earlier ones") is duplicated near-verbatim in `_pipeline_summary()` (`api/routers/reels.py:212–219`) for the single-reel detail page. Two copies today; a third copy for a new feature would repeat the exact anti-pattern this codebase's own conventions warn against (see `CLAUDE.md`'s `heartbeat()` note: "Three `heartbeat()` copies previously drifted apart").
- `engine/generation/prompt.py::build_messages(..., prior_feedback: list[str] | None = None)` — `prior_feedback` is a plain parameter, not persisted anywhere. Its only caller (`worker/tasks/generate.py:412–475`, standard LLM path only) builds it fresh per job: starts as `feedback: list[str] = []` (line 409), and on each failed attempt **replaces it wholesale**: `feedback = [i for i in last_issues if not i.startswith("Score breakdown")]` (line 475). Nothing survives across `generate_guide` runs, and nothing survives past attempt 1 of the *same* run except that attempt's own scorer issues.
- `engine/generation/evaluator.py::score_guide()` is a fixed 17-axis weighted-deduction sum starting at `score = 100` (line 322) with no weight/multiplier knob anywhere — not in the file, not in `api/config.py` (confirmed by grep: zero hits for `weight`/`axis` in `config.py`). `QUALITY_THRESHOLD` (80) / `QUALITY_THRESHOLD_LOCAL` (65) live in `worker/tasks/generate.py:38–39`, not the evaluator.
- The structured-script path (`worker/tasks/generate.py::_generate_from_structured_script`) never calls `build_messages()` at all — it only calls `build_visuals_messages()` for `visual_direction` text. `prior_feedback` has no reach into that path today, and this spec doesn't change that.
- No correlation/statistics library is used anywhere in the codebase today. `numpy` is already a dependency (render/audio arrays); `scipy`/`pandas` are not.
- Current migration head is `0006` (`migrations/versions/0006_variants.py`).

---

## 2. Feature A — Quality↔Engagement Correlation

### 2.1 What's being built

One computed number — Pearson's *r* — plus its sample size, shown on a new page, computed from data that already exists. No new Celery task, no cached/denormalized column, no chart library. This matches the "sortable table, not a dashboard" precedent already set by `_reel_list_metrics()`'s own docstring ("quality-vs-engagement at a glance, no separate dashboard needed") and by `estimate_generation()`'s honest `(None, 0)` "no history yet" return (`engine/generation/estimate.py:74-75, 85-86`) — the pattern this spec's "insufficient data" case copies directly.

### 2.2 Required refactor first

Extract the duplicated quality-score lookup into one function, then make both existing call sites use it — don't add a third copy:

```python
# engine/observability.py — alongside record_stage() / paid_call_count(), which
# already own "read StageEvent/Job rows for a reel and summarize them"

def latest_quality_scores(jobs: list[models.Job]) -> dict[int, int]:
    """Latest non-null quality_score per reel_id, from a list of Job rows spanning
    possibly multiple reels. 'Latest' = the last one in ascending created_at order —
    callers must pass jobs already ordered that way (or per-reel in reverse order;
    see the two call shapes below)."""
    by_reel: dict[int, int] = {}
    for j in jobs:
        if j.meta and j.meta.get("quality_score") is not None:
            by_reel[j.reel_id] = j.meta["quality_score"]
    return by_reel
```

- `_reel_list_metrics()` (`api/routers/reels.py:105-114`) replaces its inline loop with `latest_quality_scores(jobs)`. Behavior identical — same query, same order, same "last write wins" semantics, just factored out.
- `_pipeline_summary()` (`api/routers/reels.py:212-219`) is single-reel, so it can either call `latest_quality_scores(jobs).get(reel_id)` on its already-loaded (ascending) `jobs` list, or keep its own `reversed()` one-liner — **either is fine, they're equivalent**; the point of the extraction is Feature A's *new* all-reels query not becoming a third divergent implementation, not forcing a no-op diff on `_pipeline_summary`.

### 2.3 The all-reels query

New function, new module `engine/analytics/correlation.py` (new package — this is the first "insights" feature, `engine/observability.py` stays scoped to per-reel instrumentation, not cross-reel analysis):

```python
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import joinedload

from api import models
from engine.observability import latest_quality_scores

MIN_SAMPLE = 5  # below this, a Pearson r is more noise than signal — refuse to compute one

@dataclass
class CorrelationResult:
    r: float | None          # None when sample_size < MIN_SAMPLE or either series has zero variance
    sample_size: int
    insufficient_variance: bool  # True when r is None because scores (or views) were all identical


def quality_engagement_correlation(db) -> CorrelationResult:
    reels = db.query(models.Reel).options(joinedload(models.Reel.cuts)).all()
    reel_ids = [r.id for r in reels]
    jobs = (
        db.query(models.Job)
        .filter(models.Job.reel_id.in_(reel_ids))
        .order_by(models.Job.created_at)
        .all()
    )   # loads every job type (enrich/generate/render/publish), not just generate — matches
        # _reel_list_metrics()/_pipeline_summary()'s existing query shape exactly; harmless
        # since latest_quality_scores() only reads meta["quality_score"] (generate jobs only
        # ever set it), but a `Job.type == generate` filter would be a cheap follow-up if this
        # ever shows up in a query-count budget
    quality_by_reel = latest_quality_scores(jobs)

    pairs = []
    for r in reels:
        q = quality_by_reel.get(r.id)
        views = [c.views for c in r.cuts if c.views is not None]
        v = max(views) if views else None
        if q is not None and v is not None:
            pairs.append((q, v))

    n = len(pairs)
    if n < MIN_SAMPLE:
        return CorrelationResult(r=None, sample_size=n, insufficient_variance=False)

    qs = np.array([p[0] for p in pairs], dtype=float)
    vs = np.array([p[1] for p in pairs], dtype=float)
    if np.std(qs) == 0 or np.std(vs) == 0:
        # np.corrcoef on a zero-variance input returns NaN with a RuntimeWarning —
        # guard explicitly rather than let NaN leak into a formatted string as "nan".
        return CorrelationResult(r=None, sample_size=n, insufficient_variance=True)

    r = float(np.corrcoef(qs, vs)[0, 1])
    return CorrelationResult(r=r, sample_size=n, insufficient_variance=False)
```

**Why this reloads all reels instead of reusing `_reel_list_metrics()` directly:** that function takes an already-paginated `reels` list (`REEL_LIST_PAGE_SIZE` per page); correlation needs the *global* set. Both now share the extracted `latest_quality_scores()` core, so there's one definition of "this reel's quality score," used three ways (list page, detail page, correlation) — not three definitions.

**Why `views` (not likes/comments/a composite):** it's the one engagement number already surfaced today (`_reel_list_metrics`'s "Views" column) — correlating against a metric the operator has never seen before them would need its own justification for which composite/weights to use. Out of scope for v1; see §5.

### 2.4 Statistical honesty — the part a reviewer will actually push back on

Two things must be stated **in the UI**, not just this doc, because they're specific to how this pipeline generates guides, not generic correlation-101 caveats:

1. **Restriction of range.** `quality_score` isn't a free-floating measurement — generation retries up to 3 times specifically *to clear* `QUALITY_THRESHOLD` (80, or 65 local). Most accepted scores cluster near/above the threshold by construction, not by chance — but not all: `generate_guide`'s best-of-3 fallback (`worker/tasks/generate.py:479-481`) explicitly accepts the highest-scoring attempt even when *none* cleared the threshold, so some recorded scores legitimately sit below it. The bias is real but partial, not absolute — a correlation computed over this narrower-than-true *x*-range is still mechanically biased toward *weaker* apparent correlation than the true relationship (textbook restriction-of-range attenuation), just not as cleanly as "every score is ≥ threshold" would suggest. A low or near-zero *r* here is expected and does **not** mean quality doesn't matter; it means this specific sample can't measure it well. This is not a bug to fix in v1 — the fix (sampling below-threshold guides on purpose) would mean deliberately publishing low-quality content, a product decision, not an engineering one. State the limitation; don't hide it.
2. **Correlation is not causation**, and *n* will realistically be small (single operator, one niche) for a long time — show `sample_size` next to `r` always, never `r` alone.

UI copy (exact wording, so it can't be softened into a false-confidence number later):

> Quality ↔ views correlation: **r = 0.31** (n = 12)
> Below `MIN_SAMPLE` reels: *"Not enough published, measured reels yet (n = 2 of 5 needed)."*
> Zero-variance case: *"Every measured reel scored the same — no relationship computable yet."*
> Always shown: *"Correlation, not causation. Quality scores cluster near the acceptance threshold by design, which weakens any correlation this number can detect — see docs/specs/2026-09-phase5-quality-engagement-feedback.md §2.4."*

### 2.5 Where it's surfaced

New page, `GET /api/insights` (new router, `api/routers/insights.py` — this is a distinct concern from reel CRUD, matching the existing one-router-per-concern layout: `reels.py`, `cuts.py`, `jobs.py`, `credentials.py`). Template `ui/templates/insights.html`. Linked from `index.html` and `reels_list.html` next to the existing "All reels →" link, as a plain `<a href="/api/insights">Insights →</a>` — there's no shared nav/base template in this app (each page links to others directly; e.g. `index.html:14`), so this matches the existing pattern rather than introducing one.

This page also hosts Feature B's UI (§3.4) — one page, not two, since both are "look at past performance" concerns and the correlation number is the evidence an operator needs before writing a performance note.

### 2.6 Effort: **S**

No schema change for this half. One new module, one new router+template, two call sites refactored to share one helper.

---

## 3. Feature B — Performance-informed feedback

### 3.1 What NOT to build, and why (read this before the design)

The literal ask — "feed high/low performers back into `prior_feedback`" — most naturally reads as: automatically pick the best/worst past reels and inject their content into future prompts as few-shot examples. **Don't build that.** Two concrete failure modes, both already-documented lessons in this exact codebase:

- **Topic drift.** `CLAUDE.md` documents an explicit, repeated design principle: every enrichment/conflict-injection prompt in this pipeline carries a "topic fence" — *"Do not introduce matches, tournaments, scorelines, or players not mentioned in the beat/context"* (`engine/generation/beat_enrichment.py`, `build_visuals_messages()`). This exists because unconstrained prior context reliably leaks into unrelated generations. Auto-injecting a past reel's actual hook/beat text as a "here's what worked" example is exactly the injection vector that principle was written to prevent — a future reel about a completely different player/match would get contaminated with the old one's specifics.
- **Small-*n* overfitting.** A single-operator, single-niche tool will have a handful of published-and-measured reels for a long time (§2.4). Auto-selecting "the best 1" and generalizing from it is fitting noise, not signal — a genuinely bad decision to automate silently.

The responsible design keeps content curation **human**, and automates the *mechanical plumbing* once curated. That's not a cop-out — the codebase's own conventions already draw this line elsewhere (`api/routers/credentials.py`'s OAuth is user-initiated per connection; `job.meta["structured_fallback"]` is recorded, never auto-corrected; pricing fields stay `0.0` "until the operator sets a real rate" rather than guessing). Automating human judgment is not this codebase's house style.

### 3.2 What's actually being built

**A. `PerformanceNote`** — a small table of operator-written, plain-English notes ("Hooks phrased as a direct question outperform statement hooks — lean into that"), each toggleable active/inactive. The operator writes these *after* looking at Feature A's top/bottom performer table (§3.3) — the correlation number and the raw examples are the evidence; the note is the operator's synthesis. This is the human-in-the-loop step that avoids §3.1's failure modes: no raw past-reel content is ever auto-injected, only text the operator chose to write.

**B. Automatic plumbing**: every *active* `PerformanceNote` is seeded into `build_messages()`'s existing `prior_feedback` mechanism on **every** standard-path `generate_guide` run, from attempt 1 — not just on retries. This reuses the exact prompt-injection point that already exists (`engine/generation/prompt.py:199-207` appends `prior_feedback` as a second user message) rather than adding a new one.

**C. Evaluator axis weight multipliers** — a `Settings` field, default empty (no-op), that lets the operator scale a named axis's contribution once they have evidence (from §2's correlation, computed per-axis in a later iteration — see §5) that an axis doesn't track engagement well. No auto-tuning; this is a lever, not an algorithm.

### 3.3 Top/bottom performer report (feeds the human, not the LLM)

On the same `/api/insights` page: the same `(quality_score, views)` pairs from §2.3, sorted by `views`, top 3 and bottom 3 shown side by side — reel niche, quality_score, views, and **the hook beat's `vo_script`**, from whichever of the reel's cuts has the max views (the same cut that contributed the reel's `views` figure) — so the operator has the single highest-leverage line to eyeball, mirroring the existing product-gap reasoning for why thumbnails matter ("the single highest-leverage creative asset"). Per `CLAUDE.md`'s stated convention — "Always deserialize with `PlatformGuide(**cut.guide)` before using" — this must go through `PlatformGuide(**cut.guide).beats[0]`, not raw dict indexing (`cut.guide["beats"][0]["vo_script"]` skips Pydantic validation and would surface a confusing `KeyError`/`IndexError` instead of a clear one if a guide is ever malformed). Also check `.type == "hook"` before reading `.vo_script`, the same guard `worker/tasks/generate.py:500-501` already applies before treating a beat as the hook — `beats[0]` being the hook is a generation-time convention (`guide_schema.py`: "First beat type must be 'hook'"), not a schema-enforced guarantee, so don't assume it silently in a report that could otherwise mislabel a non-hook line as "the hook." Guard explicitly for `cut.guide is None` or `not beats` — a rendered, published cut is expected to have both, but this is a read-only report; degrade to "no hook text available" for that row rather than 500ing the whole page. If `n < 6` (top-3 and bottom-3 would overlap), show one combined sorted list instead of two — don't show the same reel in both boxes.

This is read-only. It does not write anything. It exists purely so the operator can write an informed `PerformanceNote`.

Signature, for §6's file list (implementation detail — the interesting logic is the prose above, this is just the shape):

```python
@dataclass
class Performer:
    reel_id: int
    niche: str | None
    quality_score: int
    views: int
    hook_vo: str | None   # None when the max-views cut has no guide/beats, or beats[0] isn't type "hook"

def top_bottom_performers(db, k: int = 3) -> tuple[list[Performer], list[Performer]] | list[Performer]:
    """Returns (top_k, bottom_k) sorted by views descending/ascending, or one combined
    views-descending list when n < 2*k (top/bottom would otherwise overlap)."""
```

### 3.4 `PerformanceNote` — schema, routes, UI

```python
# api/models.py — new model, alongside the other small standalone tables (Asset, StageEvent)
class PerformanceNote(Base):
    __tablename__ = "performance_notes"

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)
```

Routes (new `api/routers/insights.py`, alongside the `GET /api/insights` page route):

- `POST /api/insights/notes` — form field `text`; creates one, `active=True`; returns the re-rendered notes fragment.
- `POST /api/insights/notes/{id}/toggle` — flips `active`; returns the fragment.
- `DELETE /api/insights/notes/{id}` — hard delete (these are cheap, operator-owned free text — no soft-delete/undo needed, unlike a `Job`/`Cut` state machine).

UI: a small form (textarea + "Add note" button) and a list below it, each row with a checkbox (active toggle, `hx-post` the toggle route) and a delete `×` (`hx-delete`), all `hx-target`ing a `fragments/performance_notes.html` partial — same htmx-fragment-swap pattern as `cut_card.html`.

### 3.5 Wiring into generation — and the bug this spec is explicitly flagging

In `worker/tasks/generate.py`. `active_notes_rows` must be queried **unconditionally, at the top of `generate_guide`**, before the structured-vs-standard branch — not inside the `if guide is None:` (standard-path) block. `worker/tasks/generate.py:530`'s `job.meta = {...}` write is a single shared line reached by *both* paths (confirmed against the real function: when the structured path clears the threshold, the entire standard-path block including anything defined inside it is skipped). Querying inside that block and then referencing the result at line 530 is a `NameError` on every structured-path success — this is not a hypothetical, it's the actual control flow, so get the query placement right the first time:

```python
def generate_guide(self, db, job, reel):
    effective_context = reel.enriched_context or reel.context
    active_notes_rows = (
        db.query(models.PerformanceNote)
        .filter(models.PerformanceNote.active.is_(True)).all()
    )   # queried once, unconditionally — used by BOTH paths' job.meta write, and
        # by the standard path's prior_feedback seed below
    active_notes = [n.text for n in active_notes_rows]
    ...
    # ── Standard LLM path only — structured path never calls build_messages() ──
    if guide is None:
        ...
        feedback: list[str] = list(active_notes)   # seeded from attempt 1, not just retries
        ...
        for attempt in range(3):
            ...
            messages = build_messages(..., prior_feedback=feedback or None)
            ...
            if last_score >= quality_threshold:
                guide = candidate
                break
            ...
            # ⚠️ MUST NOT be a plain replace — the existing line is:
            #   feedback = [i for i in last_issues if not i.startswith("Score breakdown")]
            # which would silently drop the seeded performance notes on attempt 2 and 3.
            # It must become:
            feedback = active_notes + [i for i in last_issues if not i.startswith("Score breakdown")]
    ...
    # shared by both paths (existing line, worker/tasks/generate.py:530) — active_notes_rows
    # is always defined by now regardless of which path produced `guide`
    job.meta = {**(job.meta or {}), "quality_score": last_score,
                "performance_note_ids": [n.id for n in active_notes_rows]}
```

**This is the single most important section in this spec** — two independent correctness bugs live here if implemented carelessly: (a) the retry-replace wholesale-overwrite silently dropping notes after attempt 1 (the existing line at `worker/tasks/generate.py:475` is a correct wholesale replace *today* because there's nothing to lose from an empty starting list — seeding it breaks that assumption), and (b) the `NameError` from querying notes inside the wrong branch. Neither raises a loud, obvious error in the failure case that matters most (b does raise, but only on structured-path success, which a standard-path-only smoke test would never exercise; a does not raise at all). See §7 for the regression tests that must exist before this ships.

### 3.6 Evaluator axis weight multipliers

`api/config.py`, new field, following the exact honesty pattern already used for `huggingface_price_per_image` / `nvidia_price_per_1m_*_tokens` (default value that makes the feature a no-op until explicitly configured):

```python
# Per-axis scoring multipliers for score_guide()'s 17-axis rule scorer. Keys are axis
# names as used in evaluator.py's internal deduction tracking (see docs/evaluation.md
# for the full axis list); a multiplier scales that axis's deduction before the final
# sum. Empty by default — every axis behaves exactly as it does today until an axis
# name is added here. This is a manual lever informed by docs/specs/2026-09-phase5-
# quality-engagement-feedback.md §2's correlation data, not an auto-tuned weight — no
# code in this repo derives these values statistically.
evaluator_axis_weight_multipliers: dict[str, float] = {}
```

This is the **first dict-typed field in `api/config.py`** — every existing `Settings` field is a `str`/`int`/`float`/`bool` (confirmed: zero existing `dict`/`list` fields). `pydantic-settings` parses a dict-typed field from its env var as JSON (e.g. `EVALUATOR_AXIS_WEIGHT_MULTIPLIERS='{"insight": 0.5}'`), which is untested territory in this codebase — add one explicit test (`tests/test_config.py` if it exists, else a small addition wherever `Settings` is otherwise smoke-tested) asserting the env var round-trips to a real dict, not a string, before relying on it anywhere else.

`evaluator.py::score_guide()` needs one new optional parameter, `axis_multipliers: dict[str, float] | None = None`. **This is smaller than it first looks** — `score_guide()` already builds a `deductions: dict[str, int] = {}` (initialized `evaluator.py:323`) alongside the 23 inline `score -= X` sites, one named key per axis (`retention`, `narrative`, `context`, `insight`, `alignment`, `clip`, `editability`, `emotion`, `audio`, `variety`, `duration`, `caption_hashtag`, `cta`, `tone`, `throughline`, `specificity`, `repetition` — 17 keys, matching the 17 documented axes; a few axes accumulate from more than one code path into the same key, e.g. `emotion`/`duration`/`caption_hashtag`, which is fine — the multiplier only needs the *final* per-axis total). That dict exists today purely for the "Score breakdown — ..." issue-string reporting (stripped before being fed back to the LLM on retry, per `CLAUDE.md`). The 23 `score -=` sites do **not** need to change at all — add one small correction block immediately before the existing final `return max(0, min(100, score)), issues` (`evaluator.py:885`):

```python
if axis_multipliers:
    score += sum(
        deductions[k] * (1.0 - axis_multipliers.get(k, 1.0))
        for k in deductions
    )
return max(0, min(100, score)), issues
```

**Sign check, worked through explicitly** (a second review round caught this inverted in an earlier draft of this spec — the arithmetic is subtle enough to get backwards silently, so it's worth showing the derivation, not just the formula): each axis's deduction was already subtracted from `score` inline, once, unscaled — after all 23 sites run, `score = 100 - sum(deductions.values())`. The *goal* is for axis `k`'s deduction to count as `deductions[k] * axis_multipliers[k]` instead of the full `deductions[k]` it already contributed. The correction needed is `deductions[k] - deductions[k]*m_k = deductions[k]*(1 - m_k)`, **added back** to `score` (undo the excess deduction, apply the scaled one instead). Concretely, with `deductions["cta"] = 3` and a baseline `score = 97`: `m = 0.0` (multiplier's stated goal — "removes exactly that axis's deduction") → correction `= 3*(1-0) = 3` → `score = 100`, the CTA deduction fully undone, matching the intent. `m = 2.0` (double the deduction) → correction `= 3*(1-2) = -3` → `score = 94`, i.e. a 6-point total deduction, matching "double." `m = 1.0` (unconfigured, the default for every axis) → correction `= 0` regardless of `deductions[k]`'s value, so `score` is unchanged — the no-op default this spec requires in §8 falls out of the formula itself, not from a separate branch that could drift out of sync. An unknown axis name typed into `Settings` is simply never looked up (dict lookups only happen for keys `deductions` actually produced) — also a no-op, never a `KeyError`. No restructuring of the 17-axis scoring logic, no touching any of the 23 existing `score -=` lines, and no change to `test_evaluator.py`'s 29 existing cases' expected values (they all call `score_guide()` without `axis_multipliers`, which is still `None` by default).

This correction block goes immediately before the final `return` (`evaluator.py:885`), **after** the existing `score < 0` diagnostic-message block (`evaluator.py:880-884`) — note for the implementer: that diagnostic issue string is built from the pre-correction `score`/deduction total (it isn't stripped by the "Score breakdown" filter before being fed back to the LLM on retry, unlike the per-axis breakdown lines), so it can legitimately diverge from the final multiplier-corrected score when a multiplier is active. This is a cosmetic inconsistency in an edge-case diagnostic message, not a scoring bug — flagged here so it isn't mistaken for one during implementation, not because it needs a v1 fix.

Call site: `worker/tasks/generate.py` passes `settings.evaluator_axis_weight_multipliers or None` into every `score_guide()` call (both structured and standard paths — this one *does* apply to structured path, since it's the scorer, not the prompt).

### 3.7 Effort: **S**

Note CRUD + prompt-seeding plumbing, and the evaluator change (one small correction block reusing an already-existing `deductions` dict, §3.6) are both small in isolation. The care this section demands is in getting the *placement* right (§3.5's shared-code-path query, the retry-replace fix) and in the regression tests that prove it (§7) — not in line count.

---

## 4. Migration

`migrations/versions/0007_performance_notes.py`, `down_revision = "0006"`:

```python
def upgrade() -> None:
    op.create_table(
        "performance_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

def downgrade() -> None:
    op.drop_table("performance_notes")
```

Both `server_default` values match this repo's existing precedent exactly, not a new convention: `server_default="true"` (the string literal, not `sa.true()`) mirrors `0002_improvements.py`'s `assets.safe_to_publish`/`stage_events.ok` columns; `server_default=sa.func.now()` mirrors `stage_events.created_at` in the same migration. There's no existing-row backfill concern either way — this is a brand-new table, not a column added to one with existing rows — but matching the established style still matters for a reviewer scanning migrations for consistency.

No column changes to `cuts`/`jobs`/`reels` — everything else in this spec reads existing columns.

---

## 5. Explicitly out of scope (don't build these now)

- **Automatic/statistical weight tuning** (regression-fitting `evaluator_axis_weight_multipliers` from the correlation data). Needs a sample size this tool won't plausibly reach soon, and silently changing scoring behavior based on n≈10-20 data points is the overfitting risk from §3.1 applied to the evaluator instead of the prompt. The multiplier lever exists so a *human* can act on evidence; the fitting stays human too.
- **Per-axis correlation** (which of the 17 axes individually predicts engagement). `score_guide()`'s internal `deductions` dict already has every axis broken out (§3.6), but that dict never leaves the function today — it's used to build the issue strings and discarded. Persisting it (e.g. into `StageEvent.detail` or a new `job.meta` field) so a per-axis correlation could be computed later is a natural, small follow-up, but logging 17 numbers per generation "just in case" with no consumer yet is exactly the kind of speculative addition this spec is otherwise avoiding — not required for this spec's scope.
- **Per-niche correlation** — this operator has run one niche (football) so far; segmenting an already-small sample further makes every cell smaller. Revisit once niche diversity exists (ties to Phase 6c's non-football fixtures work).
- **likes/comments composite engagement metric** — `views` is what's already surfaced; defining a weighted composite is a real design decision (what weight? normalized how?) that deserves its own spec once there's a reason to believe views alone is insufficient.
- **Structured-path performance notes** — `build_visuals_messages()` has no `prior_feedback`-equivalent parameter today; threading one through is a separate, smaller follow-up if the structured path turns out to need it.
- **Time-series / trend charts** — already explicitly deferred in `docs/roadmap.md`'s open-issues table ("`pull_publish_metrics()` overwrites rather than accumulates a time series"); this spec doesn't change that column shape.

---

## 6. File-by-file change list

| File | Change |
|---|---|
| `engine/observability.py` | + `latest_quality_scores(jobs)` |
| `api/routers/reels.py` | `_reel_list_metrics()` uses the extracted helper |
| `engine/analytics/__init__.py` | new, empty |
| `engine/analytics/correlation.py` | new — `CorrelationResult`, `quality_engagement_correlation()`, `top_bottom_performers()` |
| `api/routers/insights.py` | new — `GET /api/insights`, note CRUD routes |
| `ui/templates/insights.html` | new — correlation summary, top/bottom table, notes form+list |
| `ui/templates/fragments/performance_notes.html` | new — htmx-swappable notes list |
| `ui/templates/index.html`, `ui/templates/reels_list.html` | + one link to `/api/insights` |
| `api/main.py` | `app.include_router(insights.router, prefix="/api")` |
| `api/models.py` | + `PerformanceNote` |
| `migrations/versions/0007_performance_notes.py` | new |
| `api/config.py` | + `evaluator_axis_weight_multipliers: dict[str, float] = {}` |
| `engine/generation/evaluator.py` | `score_guide()` gains `axis_multipliers` param; one small correction block before the final `return`, reusing the already-existing `deductions` dict — no change to the 23 existing `score -=` sites |
| `worker/tasks/generate.py` | seed `feedback` from active notes (both paths' `score_guide()` calls get `axis_multipliers`; only the standard path's `build_messages()` gets seeded `prior_feedback`); fix the retry-replace bug (§3.5); record `performance_note_ids` in `job.meta` |
| `docs/evaluation.md` | document `axis_multipliers` (default no-op) |
| `docs/data-model.md` | document `performance_notes` table |
| `CLAUDE.md` | module layout, key conventions, data model, build-phase-status entries |

---

## 7. Test plan

New test files, mirroring existing naming:

- **`tests/test_correlation.py`** — `quality_engagement_correlation()`: below-`MIN_SAMPLE` returns `(None, n, False)`; zero-variance quality scores returns `(None, n, True)`; zero-variance views likewise; a known synthetic dataset returns the expected *r* to a fixed tolerance (compute the expected value independently, e.g. via a hand-worked example, not by trusting `numpy` circularly); a reel with quality but no views is excluded; a reel with views but no quality (shouldn't be possible today, but defend it) is excluded; multiple cuts per reel uses the max-views cut consistent with `_reel_list_metrics()`. `top_bottom_performers()`: `n < 6` returns one combined list not two; ties in views; a reel whose max-views cut has no guide (defensive — shouldn't happen post-render, but the hook-text lookup must not crash).
- **`tests/test_insights_router.py`** — page renders with 0/some/enough data; note create/toggle/delete round-trip through the DB; deleted note no longer appears in a fresh `GET`.
- **`tests/test_evaluator.py` additions** — `axis_multipliers=None` (or `{}`) produces byte-identical scores to every existing test in the file — since the 23 `score -=` sites are untouched and the correction block's contribution is provably `0` for an empty/absent dict (§3.6), this should hold by construction, but assert it explicitly rather than trust the algebra; a multiplier of `0.0` on one named axis removes exactly that axis's deduction from the total (pick an axis with a single deterministic deduction site, e.g. `cta`, not one of the multi-path accumulating ones, for a clean expected-value assertion); a multiplier `> 1.0` increases a deduction past what the unmultiplied test fixture produced; an unknown axis name in the dict is a no-op (never a `KeyError`) — an operator typo must not crash generation.
- **`tests/test_generate_task.py` / `tests/test_r4_gaps.py` additions** — two regression tests, both required, both catching real bugs this spec's own draft contained until the review pass:
  1. **The retry-replace bug**: seed two active `PerformanceNote`s, force attempt 1 to score below threshold (mock `score_guide`/`_combined_score` to fail attempt 1, succeed attempt 2), assert the seeded notes' text is present in the `messages` passed to `build_messages`/`llm.complete` on **attempt 2**, not just attempt 1.
  2. **The structured-path NameError bug**: run `generate_guide` down the structured path to a threshold-clearing success, with zero or more active `PerformanceNote`s present, and assert the job completes `done` (not an unhandled exception) with `job.meta["performance_note_ids"]` set correctly. Mirror `tests/test_generate_task.py::test_the_soft_time_limit_in_the_structured_path_does_not_fall_back_to_the_standard_path`'s fixture shape (mocked `db`/`job`/`reel` via `_job()`/`_reel()`, `db = MagicMock()`, `SessionLocal` patched, `generate_guide` invoked directly) — **not** `test_structured_path_hook_beat_gets_a_default_music_cue`, which calls `_stubs_to_platform_guide()` directly and never invokes `generate_guide` at all, so it can't exercise this code path. This is the one test that exercises the code path where `active_notes_rows` must already be defined by the time the shared `job.meta` line at `worker/tasks/generate.py:530` runs — a standard-path-only test suite would never catch a query misplaced inside the `if guide is None:` block.

  For both: write the test first, confirm it fails against the naive/buggy version (plain `feedback = [...]` replace for #1; notes queried inside the standard-path branch for #2), then confirm it passes against the fixed version — the same mutation-testing discipline used for `reap_stuck_jobs`'s done-orphan sweep earlier in this codebase's history (mutate the real fix back out, confirm the test catches it, restore). Also: an inactive note is never seeded; zero active notes behaves exactly as today (empty list, not `None` vs `[]` mismatch — `build_messages` already treats `feedback or None` as the trigger, confirm that still holds with `feedback = []` from zero active notes).
- **`tests/test_migrations` (if this pattern exists — check first)** or a manual `alembic upgrade head` / `downgrade -1` round-trip against a throwaway Postgres, since this session's local environment had no Postgres running to verify against (see the hook/thumbnail-variant PR's same caveat) — **do this before merging**, not optional this time, since `performance_notes` is a new table (higher risk of a typo in `op.create_table` than the single-column `add_column` migrations that preceded it).

---

## 8. Rollout / backward compatibility

- Every new column/table is either a new table (no existing rows to migrate) or `Settings` fields with no-op defaults. No existing reel, cut, or job is affected by this migration.
- `evaluator_axis_weight_multipliers = {}` (default) must produce identical scores to the current evaluator for every existing evaluator test — this is a hard gate, not a nice-to-have, given `score_guide()` decides whether real content gets published.
- Zero `PerformanceNote` rows (fresh install, or an operator who never uses the feature) means `feedback = []` seeded, identical behavior to today.
- No behavior changes to the structured-script path except the new (also default-no-op) `axis_multipliers` param on its own `score_guide()` call.
