# Product Requirements Document

This document describes Reel Maker **as it exists today** — what it does, who it's for, and
what it deliberately does not do. It is not a wishlist; every requirement below is grounded in
the actual code or in `CLAUDE.md`/`docs/roadmap.md`. For module-level detail see
`docs/architecture.md`; for the engineering rationale behind major decisions see `docs/design.md`.

---

## Problem statement

Producing faceless short-form video (YouTube Shorts / Instagram Reels / TikTok-style content) at
meaningful volume is a multi-stage manual workflow: research a topic, write a script, source
footage and photos, record or synthesize voiceover, edit video with timed captions, pick a
thumbnail, write a platform-appropriate caption and hashtags, publish, and track performance. A
single person doing all of this by hand for more than a handful of videos a week hits a hard
ceiling — not from any one step being impossible, but from the end-to-end cycle time per video.

Reel Maker exists to collapse that pipeline into: enter a topic or paste a script → review an
AI-generated guide → review a rendered video → approve and publish → see what worked. It is
built for **one operator** producing content at a volume no manual workflow supports, not for a
content team or a managed platform.

---

## Goals / non-goals

**This is:**
- A local-first, single-operator pipeline (`docs/architecture.md`'s own framing).
- An async job-driven system where review and editing happen between fully-automated stages, not
  a fully "lights out" generator.
- Opinionated about quality: a generated guide must clear a two-tier evaluation threshold (or
  best-of-3) before it's shown for review at all (§7).

**This is explicitly NOT:**
- **A multi-tenant SaaS.** There is no user/account model — `credentials` are OAuth tokens for
  *the* operator's own YouTube/Instagram accounts, not per-tenant. OAuth CSRF state
  (`api/oauth.py::new_state()`/`consume_state()`) is an in-process dict, documented in code as
  "process-local CSRF state, single-operator tool" — this does not survive a multi-worker
  deployment or a restart mid-flow, and that's an accepted constraint, not an oversight.
- **A team collaboration tool.** There are no roles, permissions, assignment, or review-handoff
  concepts. `PerformanceNote`s (operator-written feedback, see §4.5) are free text with no
  author field — one operator is assumed.
- **Optimized for scale beyond one operator.** The render queue runs at `--concurrency=1` by
  design (CPU-bound ffmpeg work; see `docs/architecture.md`). Cost estimation, the paid-call
  budget cap, and the quality↔engagement correlation all model "this operator's own historical
  `StageEvent` data" — there is no per-tenant partitioning anywhere in the schema.

---

## Core user workflows

### 1. Generate a guide from a topic or script

`POST /api/reels` (`ui/templates/index.html`, the home page). The operator pastes either loose
topic context or a structured script (≥3 ALL-CAPS section headers, auto-detected, or explicitly
forced via the "Generation path" dropdown: `auto` / `structured` / `standard`), picks a niche,
target platforms (YouTube Shorts / Instagram Reels checked by default; TikTok available for
render/review only, publishing not implemented), voiceover mode, and optionally a per-reel TTS
voice and on-screen text color (both from curated lists).

A live cost/time estimate (`POST /api/reels/estimate`) updates as the operator types, sourced
from this operator's own historical `StageEvent` averages for the selected generation path — not
a guessed token count.

Submitting creates a `Reel` + one `Cut` row per platform + an `enrich` `Job`, and returns a
polling fragment. Behind the scenes: `enrich_context` scores the raw context (5-axis rule scorer)
and optionally enriches it via LLM, then chains into `generate_guide`, which runs the
structured-script path or the standard LLM path (§7) and writes the resulting `MasterGuide` to
each `Cut.guide`. The reel transitions `draft → enriching → generating → guide_ready` (or
`failed`).

### 2. Review and edit the guide

Once the reel reaches `guide_ready`, each cut's generated guide is reviewed and edited on its
cut card (`GET /api/reels/{id}`, `ui/templates/fragments/cut_card.html`):
per-beat `visual_direction`, `vo_script`, `on_screen_text` (up to 5 segments), and `duration_s`
are all editable, along with the cut's caption and hashtags. `PATCH /api/cuts/{id}` saves
changes, writing only fields whose *normalized* value actually differs from what's stored (so a
pure caption edit via a full-form resubmit doesn't spuriously dirty an untouched multi-line
`vo_script`, which browsers always re-encode with `\r\n` on submit — see `docs/design.md` §5 for
why this precision matters: it feeds a publish-time staleness fingerprint).

### 3. Render and review the video

`POST /api/cuts/{id}/render` transitions the cut to `rendering`, creates a `render` `Job`, and
returns a polling fragment (`GET /api/cuts/{id}/render-status`). `render_cut` sources assets per
beat (pinned, reused across re-renders — §7), synthesizes TTS and budgets it to each beat's
duration, composites video + audio + timed captions, and writes an atomic MP4 plus up to 4
thumbnail candidates and an `.srt` subtitle file. The cut lands in `in_review` with a video
player, thumbnail picker, hook-variant picker (3 alternate hook lines, generated once per
accepted guide), and Re-render / Approve buttons. A beat the asset-sourcing fallback chain
couldn't fill for at all is flagged with a black-frame warning banner rather than failing
silently.

### 4. Approve and publish

`POST /api/cuts/{id}/approve` moves the cut to `approved`. `POST /api/cuts/{id}/publish` enqueues
`publish_cut`, which gates on `assert_safe_to_publish()` (Wikipedia-sourced assets without a
permissive license block publishing) and the two staleness gates (`assert_video_matches_pins`,
`assert_video_matches_guide` — see `docs/design.md` §5) before calling the platform uploader.
YouTube uses a resumable upload plus a best-effort captions attach; Instagram uses the Graph
API's container-create/poll/publish flow and requires a real public HTTPS `PUBLIC_BASE_URL`
(Instagram fetches the video itself, not an upload body). TikTok is a selectable render/review
platform but publishing raises `NotImplementedError` on purpose — see §8. A published cut shows
views/likes/comments once the first 6-hourly metrics pull lands.

### 5. Review performance and feed learnings back

`GET /api/insights` shows a Pearson correlation (`r` + `sample_size`, always together, with a
permanent caveat about restriction-of-range bias) between each reel's quality score and its
max-per-cut views, refusing to compute below 5 reels or on zero variance; a top/bottom-3
performer table with each performer's hook line; and a `PerformanceNote` CRUD form. An active
note is seeded into every subsequent standard-path generation's `prior_feedback` from attempt 1
— a deliberately human-curated, not automatically few-shot-injected, feedback loop (this
codebase has already been burned by unconstrained prior-context leaking into generation and
causing topic drift — see CLAUDE.md's topic-fence notes).

---

## Functional requirements, by pipeline stage

**Context entry & enrichment**
- Accept free-text context or a structured script; auto-detect which via `≥3 ALL-CAPS headers`,
  overridable per-reel.
- Score raw context on 5 axes (length, specificity, stakes, narrative arc, hook potential);
  enrich via LLM only when below threshold and only for unstructured context (structured scripts
  skip enrichment entirely — their topic is already locked in).

**Guide generation**
- Two generation paths: a fast structured-script extractor (beats parsed directly, tactical
  insight + conflict-beat injection, visuals-only LLM call) and a full standard LLM path
  (complete `MasterGuide` JSON, up to 3 attempts with closed-loop feedback).
- A hard per-reel cap on paid LLM calls (`Settings.max_paid_llm_calls_per_reel`), enforced at
  task entry and before every retry.
- Output must validate against the `MasterGuide`/`PlatformGuide`/`Beat` Pydantic schema
  (`engine/generation/guide_schema.py`).

**Quality evaluation**
- Every generated guide is scored by a deterministic 17-axis rule scorer and, above a rule-score
  floor, an LLM semantic judge; combined score must clear a threshold (80 NVIDIA / 65 local) or
  the best of 3 attempts is accepted.
- `evaluator_axis_weight_multipliers` lets an operator manually reweight a named axis based on
  the Insights-page correlation data — explicitly not auto-tuned (§8).

**Asset sourcing**
- Fallback chain per beat: Wikipedia (named-subject photos, with license/attribution metadata)
  → Pexels stock footage → HuggingFace-generated video → HuggingFace-generated image → black
  frame (flagged, never a silent failure).
- Assets are pinned per beat and reused across re-renders unless `visual_direction` actually
  changed (fingerprinted).
- A beat naming exactly two subjects renders as a 2-up side-by-side collage; 1 or 3+ uses
  sequential Ken Burns sub-clips.

**Rendering**
- TTS (Edge TTS default, Kokoro optional, Silent for testing) measures and budgets actual audio
  duration per beat, adjusting speaking rate ±25% to hit target.
- Whisper-driven word-level caption timing when available, proportional fallback otherwise.
- Local-library background music, sidechain-ducked against VO when present.
- Atomic MP4 write (`.tmp.mp4` → `os.replace()`); every asset download is atomic the same way.
- Four thumbnail candidates sampled per render; the operator picks one.

**Review / edit**
- Every beat field, caption, and hashtags are editable in `in_review`; an edit only dirties the
  stored guide (and the publish-time staleness fingerprint) when the normalized value actually
  changed.
- Re-render only re-resolves beats whose `visual_direction` changed.
- Hook-variant swap and thumbnail-candidate selection, both gated to `in_review`.

**Publishing**
- `safe_to_publish` (asset licensing) enforced exactly once, immediately before any platform
  call.
- Two staleness gates (pins fingerprint, guide fingerprint) block a "Retry publish" that would
  ship a video that no longer matches its current pins or guide.
- YouTube and Instagram publishing are implemented (OAuth connect/disconnect UI at
  `/api/credentials`); TikTok is not (§8).
- A Wikipedia-sourced asset's attribution/license is appended to the published caption without
  mutating the DB-stored caption.
- Publishing never auto-retries (`max_retries=0`) — an upload that times out mid-flight could
  otherwise double-post.

**Analytics**
- Views/likes/comments pulled every 6 hours for published cuts, best-effort per cut (a missing
  fetcher, missing credential, or fetch exception skips that cut, never the whole run).
- An append-only `cut_metric_snapshots` history table accumulates each raw pull (not the
  forward-filled "latest known" value) for a future trend feature — not read by anything yet.
- Quality↔engagement Pearson correlation and a top/bottom performer table on `/api/insights`.

---

## Non-functional requirements

**Reliability**
- Every background task runs through one shared lifecycle (`job_task`): atomic claim,
  idempotency guard, heartbeat, fenced done-stamp, transient-failure retry (2 attempts,
  30s/60s backoff) for classified-transient errors only.
- A stuck-job reaper runs every 60s, failing stale `running` jobs (no heartbeat in 5 min) and
  never-picked-up `pending` jobs (240 min), and — for resumable task types only — can put a
  killed job back to `pending` for a second worker, protected by the `Job.claim_token` fencing
  mechanism (`docs/design.md` §3).
- Atomic file writes everywhere a file is cached or served, so a killed process never leaves a
  truncated file that gets reused.

**Cost control**
- A hard ceiling on paid LLM calls per reel.
- Pre-generation cost/time estimates sourced from this operator's own historical data, not a
  guessed figure; "no history yet" is reported honestly rather than fabricated.
- HuggingFace asset-generation cost is charged only on an actual generation call, never a cache
  hit.

**Observability**
- `StageEvent` rows record latency, provider, cost, and success/failure for every instrumented
  stage; a per-reel pipeline panel (`GET /api/reels/{id}`) surfaces cost/latency/quality.
- `record_stage()`'s "never fail the job, but still tell the truth" composition pattern is used
  wherever a step (captions upload, Instagram metrics drift detection) must not fail its
  enclosing job but still needs an honest, queryable failure signal.

**Security**
- OAuth tokens (`Credential.token_blob`/`refresh_token_blob`) are encrypted at rest (Fernet);
  plaintext storage with a warning is the deliberate dev-mode fallback, not production behavior.
- Per-reel TTS voice and on-screen text color are both curated-list selections, not free text —
  the text color in particular is a filter-injection guard, since it's interpolated unescaped
  into an ffmpeg `drawtext` filter string.
- Video/thumbnail/subtitle file streaming validates the resolved path stays under the configured
  store directory before serving (path-traversal guard).
- A credential token is never placed in a URL query string — always an `Authorization: Bearer`
  header or a POST body — so it can't leak into an exception message, a log line, or `job.error`.

---

## Explicitly out of scope / known gaps

Pulled directly from CLAUDE.md's "What is not yet wired in" and `docs/architecture.md`'s "What
is not yet built" — these are the real, currently-documented gaps, not an invented list:

- **TikTok publishing** — `TikTokPublisher.publish()` raises `NotImplementedError` on purpose;
  TikTok's Content Posting API needs a separate audited app review, unlike YouTube/Instagram's
  self-serve OAuth. Render/review works; only the upload call is missing.
- **Scheduled publishing has no trigger UI** — the `scheduled` cut status and `publish_cut` both
  handle a cut already sitting in `scheduled`, but nothing currently transitions a cut *into*
  it (no date/time picker, no beat-driven scheduler).
- **Multi-image collage beyond 2 items** — a beat with 3+ resolved media items still cycles
  sequentially; no grid layout. An explicit, bounded non-goal, not an oversight.
- **No Pixabay Music API** — Pixabay's public REST API has never documented a Music search
  endpoint, so music sourcing is a local library (`LocalMusicSource`) by design, not a stopgap.
- **Unpublish / re-publish flows** — a published cut has no "take down" or "publish again"
  action.
- **Analytics beyond raw views/likes/comments and the quality↔views correlation** — no
  trend-chart UI yet (the snapshot history table now exists but nothing reads it), no
  engagement-rate normalization, no per-axis correlation (which of the 17 evaluator axes
  individually predicts engagement), no per-niche correlation, no likes/comments composite
  metric.
- **Automatic/statistical tuning of `evaluator_axis_weight_multipliers`** — the multiplier lever
  exists so a human can act on the correlation data; fitting it statistically from an n≈10–20
  sample was judged the same overfitting risk `PerformanceNote`s were designed to avoid on the
  prompt side.
- **Logo/watermark overlay and per-channel presets** — Phase 6 creative-range items not started.
- **Auth/rate-limiting scope decision** — the one remaining open item under Phase 7 production
  hardening.

---

## Success criteria / how quality is measured today

- **Per-guide acceptance gate**: `combined = int(rule_score * 0.4 + llm_judge_score * 0.6)` must
  clear 80 (NVIDIA-routed generation) or 65 (local Ollama) — or the best of 3 attempts is
  accepted, so an unattended pipeline never simply fails on a mediocre result. The rule scorer
  alone covers 17 axes (retention architecture, narrative quality, context coverage, insight
  density, script↔visual alignment, clip availability, editability, emotional impact, audio
  delivery, visual variety, duration fit, caption/hashtag, CTA quality, conversational tone,
  hook-CTA throughline, per-beat specificity, repetition) — see `docs/evaluation.md` for the
  full point breakdown.
- **Outcome-level signal**: the quality↔engagement Pearson correlation on `/api/insights` is the
  system's only attempt to validate that the acceptance gate actually predicts what matters
  (views). It is presented with an explicit statistical-honesty caveat (restriction-of-range
  bias — accepted scores cluster near the threshold by construction — and "correlation, not
  causation"), and refuses to compute below a 5-reel sample or zero variance in either series
  rather than show a misleading number.
- **Operator-facing cost/quality visibility**: the per-reel pipeline panel and the reel-list
  Quality/Views columns are the day-to-day instrument an operator uses to judge whether a given
  reel's generation was worth its cost — grounded in real `StageEvent` data, not estimates, once
  a reel completes.

---

## See also

- `docs/architecture.md` — module breakdown, request/response flow diagrams, render pipeline
  detail, guide schema.
- `docs/design.md` — the engineering rationale behind the job lifecycle, staleness gates, and
  review discipline referenced throughout this document.
- `docs/data-model.md` — table definitions and state machines.
- `docs/evaluation.md` — the full quality-scoring axis table.
- `docs/roadmap.md` / `docs/product-gap-analysis-and-roadmap-2026-08.md` — phase-by-phase build
  log and the forward-looking strategic gap analysis.
