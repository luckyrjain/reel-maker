# change_impact_report — SRT caption export (v1)

## title
SRT caption export (Phase 5, roadmap item 5d) — blast-radius analysis of the reviewed system design

## assessment_target
- Design: `docs/specs/2026-09-srt-caption-export-system-design.md` (reviewed, revision applied — see its "Revision note")
- Repository: `reel-maker` (single monolith checkout, no multi-repo scope)
- Artifact state: `proposed_state` — no code changes exist yet; this is a pre-implementation blast-radius read against `main`

## coverage_status
**COMPLETE** for this repository. `host.repository.read` was available (full local checkout); every impacted file below was read directly, not inferred. No external SCM/PR context applies — there is no PR yet. No material unknowns come from missing repository access; the unknowns in this report are behavioral/external-contract unknowns already flagged by the design itself, not evidence gaps in this analysis.

## criticality
**Medium.** Additive feature (nullable column, new optional artifact, new best-effort external call) — nothing in the existing render/publish/review lifecycle is gated on it, and every existing test/behavior is unaffected by `subtitle_path = None`. Raised above Low because the design's central mechanism (§2 of the design doc) modifies a shared, already-fragile code path — `transcribe_audio()`/`_whisper_timestamps()` — whose exact offset semantics this repo has already been burned by once before (the zero-audio incident documented in `compositor.py`'s own module history). An incorrect implementation of the reviewed fix could silently corrupt burned-in caption timing for every reel, not just fail to produce subtitles.

## change_classes
- `data-model-migration` — new nullable `Cut.subtitle_path` column
- `internal-api-signature-change` — `transcribe_audio()` return type (`list[CaptionSegment]` → `TranscriptResult`) and `composite_cut()` return arity (2-tuple → 3-tuple), both internal (not HTTP-facing)
- `new-http-endpoint` — `GET /api/cuts/{id}/subtitles`
- `external-integration-addition` — YouTube Data API `captions.insert` call from `YouTubePublisher`
- `new-module` — `engine/render/srt.py`
- `ui-addition` — cut-card download link
- `docs-update` — `CLAUDE.md`, `docs/roadmap.md`, `docs/data-model.md`, `docs/api.md`

## impacted_repositories
- `reel-maker` (this checkout) — sole impacted repository. No other repos, no shared/vendored packages published from this one.

## impacted_services
Not a microservice system — mapped to internal components/processes instead:
- **API process** (`api/main.py`, uvicorn) — new route only; no change to existing routes, no change to FastAPI app wiring beyond one router addition.
- **`rendering` Celery queue** (`worker/tasks/render.py::render_cut`, concurrency=1) — consumes the extended `composite_cut()` return value; runs inside the *existing* job, no new job type, no queue-routing change.
- **`generation` Celery queue** (`worker/tasks/publish.py::publish_cut`) — gains one new best-effort HTTP call inside `YouTubePublisher.publish()`; still `max_retries=0`, still routed to the same queue, no signature change to the task itself.

## impacted_contracts
- **Internal function contract — breaking, coordinated in one PR:**
  `engine/render/captions.py::transcribe_audio()` (`captions.py:20`) return type changes from
  `list[CaptionSegment]` to `TranscriptResult`. Verified single call site:
  `engine/render/compositor.py:471-475` (inside `composite_cut()`). No other caller exists in
  `engine/` or `worker/` (grep-verified). Not a public/HTTP contract — no external consumer.
- **Internal function contract — breaking, coordinated in one PR:**
  `engine/render/compositor.py::composite_cut()` (`compositor.py:404`) return arity changes from
  `tuple[float, list[Path]]` to a 3-tuple (adds subtitle path or `None`). Verified call sites, all
  requiring the same-PR update per the design's rollout plan (§10 point 4):
  - Production: `worker/tasks/render.py:127`
  - Tests: `tests/test_compositor.py:51,196,216,235` (4 sites)
  - Tests: `tests/test_golden_reel.py:68` (1 site, the Phase 7d real-chain golden test)
  6 total call sites, all positional unpacking (`duration, thumbnail_candidates = composite_cut(...)`)
  — none use `*rest`/keyword unpacking that would mask a silent arity mismatch, so a missed call
  site fails loudly (`ValueError: not enough values to unpack`) rather than silently, which lowers
  (but does not eliminate) the risk of an incomplete rollout.
- **New HTTP contract (additive, non-breaking):** `GET /api/cuts/{id}/subtitles`. No existing
  route changes shape or status-code behavior. `docs/api.md`'s `## Cuts` section (`docs/api.md:71`)
  gets a new entry alongside `GET /api/cuts/{cut_id}/video` (`docs/api.md:164`), which is the
  design's own required precedent for the guard pattern (403/404 semantics identical).
- **External API contract (new, unverified):** YouTube Data API `captions.insert`. The design
  itself flags (§3.3, §9.2) that this has not been exercised against a live account in this design
  pass — carried forward as `unknowns` below, not resolved by this impact analysis.

## impacted_data
- **New column:** `cuts.subtitle_path` (nullable `String(500)`), migration `0011` (next free
  number — confirmed via `migrations/versions/` listing, current head is `0010_black_frame_visibility.py`).
  Additive, no backfill, no data migration risk — matches the exact precedent of `0009`/`0010`.
- **No changes** to any other table, no changes to `CutAsset`, `StageEvent`, `Job`, `Credential`,
  or `PerformanceNote` schemas.
- **New disk artifact class:** one `.srt` file per rendered cut under `VIDEO_STORE_DIR`, sized
  single-digit KB per the design's own capacity estimate (§8) — no storage-growth concern flagged.
- **StageEvent growth:** one new `stage="captions_upload"` row per YouTube publish attempt with a
  populated `subtitle_path` — additive row growth only, same pattern as every other `record_stage`
  call site, no schema change (JSON `detail` column already exists).

## impacted_dependencies
- **No new third-party packages.** `openai-whisper` (already a project dependency, optional/
  best-effort per CLAUDE.md) has its *usage* extended (reading `result["segments"]`'s existing
  fields, not a new API surface of the library) — verified against the real installed library
  source during design review, not merely documented behavior.
- `httpx` (already used throughout `engine/publish/`) is reused for the new captions.insert call
  — same client library, same auth-header pattern as the existing YouTube upload call in
  `engine/publish/youtube.py`, no new dependency.
- No dependency version bumps required by this design.

## impacted_owners
This repository has no formal team/ownership model — CLAUDE.md and `api/oauth.py` both explicitly
describe it as a **single-operator tool** (no per-team CODEOWNERS, no multi-team routing found in
the repo). `impacted_owners` is therefore the repo's sole maintainer/operator for all surfaces
touched. No cross-team review trigger applies on ownership grounds; review-worthiness is scoped
entirely by technical risk (see `review_triggers` below).

## required_tests
Directly named by the design's own rollout plan (§10), cross-checked against real existing test
files for placement accuracy:
1. `tests/test_audio_text_sync.py` — update for `TranscriptResult` shape; **new** regression test
   asserting `.words`/`_whisper_timestamps()` drawtext output is byte-identical before/after the
   `.segments` field is added (the design's own explicit guard against the offset-doubling bug
   found in review — this is the single most important new test in this change, given the
   `criticality` rationale above).
2. `tests/test_srt.py` (new file) — `write_srt()` format correctness: timestamp formatting,
   sequential numbering, empty-cues → `None`, multi-beat cue concatenation with correct absolute
   offsets.
3. `tests/test_compositor.py` — extend the 4 existing real-ffmpeg `composite_cut()` call sites
   (`:51,196,216,235`) for the new 3-tuple return; add an assertion that a real `.srt` file is
   produced with real Whisper-or-fallback timing.
4. `tests/test_golden_reel.py` — extend the Phase 7d real-chain golden test (`:68`) with the same
   assertion, since it is explicitly the "no mocks anywhere" end-to-end test this class of claim
   belongs in (design §10 point 4 makes this same call).
5. New router test (mirroring `test_variants_router.py`'s existing style for
   `stream_thumbnail`'s 403/404 cases) for `GET /api/cuts/{id}/subtitles`'s path-traversal guard.
6. New/extended `tests/test_publish_task.py` or `test_youtube_publisher.py` test asserting a
   captions-upload failure (a) does not fail `publish_cut`, (b) does not affect
   `cut.platform_post_id`/`published_at`, **and** (c) results in a `StageEvent` with `ok=False` —
   part (c) is the design's own review-driven addition and is easy to omit if an implementer
   copies only the "doesn't fail the job" half of the requirement.
7. Migration round-trip verification (`upgrade head` / `downgrade -1` / `upgrade head`) against
   real Postgres, per this repo's established practice for every prior migration in this session's
   history (`0008`, `0009`, `0010` were each verified this way, per prior session work).

## operational_impacts
- **Render latency:** effectively none — no new external call added to the render path; the
  Whisper call that already runs is read more fully, not run more often.
- **Publish latency:** one new HTTP call added to `publish_cut`'s YouTube path only, expected low
  seconds, well inside the existing 60-minute `max_runtime_s` budget (design §8) — no operational
  alerting/timeout tuning required.
- **Worker restart/deploy:** none — no queue routing change, no new Celery task registered
  (`worker_max_tasks_per_child=10` recycling behavior unaffected).
- **Rollback:** the migration is purely additive (nullable column) — a rollback (`downgrade -1`)
  is safe with no data loss beyond the new column's own values, matching every prior migration in
  this codebase.
- **Feature flag:** none used or needed — the design explicitly states single-PR rollout, no
  phased flag (§10), consistent with every other roadmap item shipped this session.

## review_triggers
1. **Correctness review of the offset-handling fix** (design §2/§3.1/§3.2) — this is the one place
   in this change where an implementation detail, not just a design decision, matters: an
   implementer must not "simplify" by passing a non-zero `beat_offset_s` into `transcribe_audio()`
   for convenience, which would silently reintroduce the exact bug the design review caught. Flag
   for a dedicated re-read against the merged diff, not just the design doc.
2. **`record_stage()` composition review** (design §7) — verify the actual PR places the
   try/except *inside* the `with record_stage(...)` block and explicitly sets `ev.ok = False` on
   failure, not just that `publish_cut` doesn't raise. A superficial "job didn't fail" test could
   pass while still losing the `StageEvent` failure signal — needs a test asserting `ok=False`
   specifically (see `required_tests` item 6c), and a reviewer should confirm that test actually
   exists and actually exercises the failure branch (mutation-test it: force the upload call to
   raise, confirm the StageEvent it produces has `ok=False`).
3. **YouTube Captions API live verification** (design §9.2, carried into `unknowns` below) — before
   this is called done, one real call against a real connected account, not just a passing unit
   test against a mocked `httpx` response.
4. **Docs completeness** — the design's rollout plan now explicitly requires `docs/data-model.md`
   (added during review) in addition to `docs/api.md`/`CLAUDE.md`/`docs/roadmap.md`; a reviewer
   should confirm all four actually changed, not just the ones easiest to remember.

## unknowns
Carried forward directly from the design doc's own `Open questions` (§9) and `Assumption flagged`
(§3.3) — not resolved by this impact analysis, which is scoped to repository blast radius, not
external API verification:
1. Whether YouTube's `captions.insert` truly accepts raw SRT bytes as its media part with
   auto-detected format (documented, not live-tested as of this analysis).
2. VTT export remains unimplemented by design (deliberately deferred, not an unknown so much as an
   explicit non-goal — listed here only because the roadmap item's title mentions it).
3. Instagram/TikTok caption upload remains unattempted (no equivalent API identified in this
   codebase's existing publisher integrations) — confirmed as a genuine absence, not an oversight,
   by reading `engine/publish/instagram.py` and `engine/publish/tiktok.py` directly.

## evidence_refs
- `docs/specs/2026-09-srt-caption-export-system-design.md` (full document, post-review revision)
- `engine/render/captions.py:20` (`transcribe_audio` signature)
- `engine/render/compositor.py:175` (`_whisper_timestamps`), `:404` (`composite_cut`), `:471-476` (transcription call site + `_build_text_filter` call)
- `worker/tasks/render.py:127` (production `composite_cut()` call site)
- `tests/test_compositor.py:51,196,216,235` (4 `composite_cut()` call sites)
- `tests/test_golden_reel.py:68` (Phase 7d golden-reel `composite_cut()` call site)
- `api/models.py` — `Cut` class (existing columns, precedent for `subtitle_path` placement)
- `migrations/versions/` — confirms `0010_black_frame_visibility.py` is current head, `0011` free
- `docs/data-model.md:3` (migration list sentence), `:45` (`black_frame_beat_indices` row — precedent for the new `subtitle_path` row)
- `docs/api.md:71` (`## Cuts` section start), `:164` (`GET /api/cuts/{cut_id}/video` — precedent entry)
- `engine/publish/youtube.py` (existing upload flow, `get_valid_access_token()` reuse point)
- `worker/tasks/publish.py` (`platform_post_id` commit ordering, finalize-branch structure)
- `engine/observability.py::record_stage` (re-raise semantics — confirmed during design review)
