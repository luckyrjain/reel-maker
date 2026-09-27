# change_impact_report — safe_to_publish staleness gate fix (v1)

## title
Publish gate must verify the video matches current pins (docs/roadmap.md Open Issues, Medium severity) — blast-radius analysis of the reviewed system design

## assessment_target
- Design: `docs/specs/2026-09-video-pins-staleness-gate-system-design.md` (reviewed, revised — see its "Revision note")
- Repository: `reel-maker` (single monolith checkout)
- Artifact state: `proposed_state` — no code changes exist yet

## coverage_status
**COMPLETE.** Full local repository read available; every impacted file below was read directly. No external SCM/PR context applies (no PR yet). No material unknowns from missing repository access — the only unknowns carried forward are the design's own explicitly-scoped-out items (§1's non-goals), not evidence gaps in this analysis.

## criticality
**Medium**, matching the source roadmap entry's own severity. This is a targeted fix to a real safety-gate correctness bug (a stale/potentially-unsafe video can ship despite the gate passing) — not architectural, additive-only (one nullable column, two small functions), but the property it protects (nothing unlicensed ships to a public platform) is exactly the kind of thing worth flagging above Low even though the blast radius of the *fix itself* is small.

## change_classes
- `data-model-migration` — new nullable `Cut.rendered_pins_fingerprint` column
- `internal-api-addition` — two new pure functions (`compute_pins_fingerprint`, `assert_video_matches_pins`), no existing function signature changes
- `safety-gate-extension` — a second check added alongside the existing `assert_safe_to_publish` call in `publish_cut`
- `test-harness-fixture-change` — `tests/test_publish_task.py`'s `_cut()` helper needs an explicit default (see `review_triggers`)
- `docs-update` — `CLAUDE.md`, `docs/roadmap.md`, `docs/data-model.md`

## impacted_repositories
- `reel-maker` (this checkout) — sole impacted repository.

## impacted_services
- **`generation` Celery queue** (`worker/tasks/publish.py::publish_cut`) — gains one new in-process function call (`assert_video_matches_pins`) inside the existing uploading branch. No task signature change, no queue-routing change, no new job type.
- **`rendering` Celery queue** (`worker/tasks/render.py::render_cut`) — gains one new field assignment at its existing success point. No new external call, no latency-relevant change.

## impacted_contracts
- **Internal function contract — additive, non-breaking:** `engine/render/asset_sourcer.py::compute_pins_fingerprint(db, cut_id) -> str | None` (new). No existing caller to break.
- **Internal function contract — additive, non-breaking:** `engine/publish/gate.py::assert_video_matches_pins(db, cut) -> None` (new, takes a `Cut` object — deliberately asymmetric with `assert_safe_to_publish(db, cut_id: int)`, justified in the design §4/reviewed as causing no real friction since `publish_cut` is the only caller of either and already has both forms available).
- **No breaking signature change anywhere** — unlike the SRT caption export change, nothing here alters an existing function's return type or arity. This lowers the blast radius considerably relative to that prior change.
- **HTTP contract:** none. No new route, no existing route's shape changes.

## impacted_data
- **New column:** `cuts.rendered_pins_fingerprint` (nullable `String(64)`), migration `0012` (confirmed next-free number — `migrations/versions/` currently tops out at `0011_subtitle_caption_export.py`). Additive, no backfill (§7's explicit, reviewed rollout decision).
- **No changes** to `CutAsset`, `Asset`, `Job`, `StageEvent`, or any other table's schema. `compute_pins_fingerprint` only reads existing `CutAsset.beat_index`/`order_in_beat`/`asset_id` columns (confirmed present at `api/models.py`'s `CutAsset` class, `:151` onward, with the `UniqueConstraint("cut_id", "beat_index", "order_in_beat")` at `:154`).
- **No data migration risk either direction** on rollback (`downgrade -1` simply drops the column; every existing row's `video_path`/`CutAsset` rows are untouched).

## impacted_dependencies
- **None.** No new third-party package. `hashlib` (already used identically for `_fp()` at `engine/render/asset_sourcer.py:561`) is the only library the new function needs.

## impacted_owners
Same as the prior change-impact report for this repository: no formal team/ownership model, single-operator tool (`CLAUDE.md`/`api/oauth.py`'s own framing). Sole owner is the repository's maintainer for every surface touched.

## required_tests
Directly named by the design's revised rollout plan (§10), cross-checked against the real test files' current structure:
1. `tests/test_asset_sourcer.py` — new cases for `compute_pins_fingerprint()`: deterministic for a given pin set, order-independent w.r.t. query result ordering, `None` for zero pins, changes when a beat's pin changes, unaffected when an untouched beat's pin stays the same.
2. `tests/test_render_task.py` — call-and-assign wiring only (this file is 100%-`MagicMock`-based, confirmed by reading it — `resolve_or_reuse` is patched in every success-path test, no real `CutAsset` row ever exists there): patch `compute_pins_fingerprint`, assert it's called with `cut.id` and its return value lands on `cut.rendered_pins_fingerprint`. The design's revision explicitly corrects an earlier draft that overclaimed this file could verify the fingerprint "reflects the actual pins."
3. `tests/test_publish_gate.py` — new cases mirroring its existing three-test style (confirmed: `db_session` fixture from `tests/conftest.py:11`, same helper pattern as `_make_cut_with_asset`): match passes, mismatch raises `ValueError` with an actionable message, `rendered_pins_fingerprint is None` does NOT block (the single most important test for this rollout's safety property, per the design's own emphasis).
4. `tests/test_publish_task.py` — **requires a fixture-default fix before any new test can be added safely**, see `review_triggers.1`. Then: a mismatch blocks publish (publisher never reached), the finalize branch (`worker/tasks/publish.py:45` `if cut.platform_post_id:`) is confirmed via mutation-test to never reach the new call.
5. **End-to-end regression test** (the design calls this "the single most important test in this rollout," §10 step 6): reproduce the exact bug sequence — render succeeds (safe pins), a beat's `visual_direction` changes causing a re-pin, simulate that second render failing after the re-pin commits but before `video_path` updates, assert `assert_video_matches_pins` now raises for the still-stale `video_path`. This is the test that proves the *fix*, not just that each new function behaves correctly in isolation — cannot be satisfied by items 1-4 individually.
6. Migration round-trip (`upgrade head` / `downgrade -1` / `upgrade head`) against real Postgres, per this repository's established practice for every prior migration this session.

## operational_impacts
- **Render latency:** negligible — one additional indexed-by-`cut_id` query plus a cheap hash over a small beat count, at a point (`worker/tasks/render.py:142-151`) `render_cut` already does comparable DB work.
- **Publish latency:** negligible — same shape of query, called once per publish attempt, at the exact point `assert_safe_to_publish` already runs (`worker/tasks/publish.py:52`).
- **Deploy/rollback:** purely additive nullable column; rollback drops it cleanly. The design's §7 rollout decision (`None` means "legacy, skip the check") is specifically engineered so this deploy cannot regress any existing cut's publishability — no operational runbook step needed beyond the standard migration application.
- **No feature flag** — single-PR rollout, consistent with the design's own stated rationale (§10) and every other roadmap item shipped via this pipeline this session.
- **No queue/worker restart considerations** beyond the standard "workers don't auto-reload" convention already documented in `CLAUDE.md`.

## review_triggers
1. **CRITICAL, will break CI if missed** — `tests/test_publish_task.py`'s `_cut()` helper (`:24`) currently returns a bare `MagicMock()`. Confirmed empirically reachable: six existing tests exercise `publish_cut`'s uploading (`else:`) branch (`test_deterministic_publisher_failure_transitions_cut_to_failed`, `test_successful_publish_records_platform_post_id`, `test_a_transient_publisher_error_is_never_retried_automatically`, `test_post_id_is_committed_before_anything_that_can_fail_after_the_upload`, `test_missing_credential_fails_with_actionable_message`, `test_unsafe_asset_blocks_publish`). Once `assert_video_matches_pins` is wired into that branch, every one of these tests will hit the new check first unless `_cut()` explicitly sets `cut.rendered_pins_fingerprint = None`. A reviewer must confirm this fixture change lands in the same commit as the `publish_cut` wiring, not as an afterthought once CI turns red.
2. **Mutation-test verification of the rollout-safety property** — confirm the `rendered_pins_fingerprint is None` → skip-the-check test (in `test_publish_gate.py`) is genuinely mutation-tested: temporarily make the check fire on `None` too, confirm that specific test fails, revert, confirm it passes. This is the property the design's entire §7 rollout argument depends on; a reviewer should not accept a passing test suite as proof without seeing this mutation cycle done.
3. **The end-to-end regression test (required_tests.5) is not optional polish** — a reviewer should treat its absence as a genuine gap, not a nice-to-have, since it's the only test in the plan that actually reproduces the bug sequence rather than testing each new function's documented behavior in isolation.
4. **Confirm the finalize branch is truly untouched** — `worker/tasks/publish.py:45-50` (`if cut.platform_post_id:`) vs `:51` (`else:`) — the new call must land only in the `else:` branch. Existing tests already exercise the finalize branch (`test_an_already_posted_cut_is_finalized_without_uploading_again`, `test_an_already_posted_cut_is_finalized_even_if_an_asset_was_flagged_since`); a reviewer should confirm these still pass unmodified and, ideally, that a mutation attempt to move the new call into that branch would make one of them fail.

## unknowns
None carried from the design beyond its own explicitly-scoped non-goals (§1): this fix does not address the separate, already-tracked, Low-severity "guide edited without a `visual_direction` change" staleness gap (`docs/roadmap.md`'s "'Retry publish' on a `failed` cut can ship a stale pre-edit video" entry) — confirmed by the design itself as an intentional scope boundary, not an oversight, and cross-referenced in both documents.

## evidence_refs
- `docs/specs/2026-09-video-pins-staleness-gate-system-design.md` (full document, post-review revision)
- `engine/render/asset_sourcer.py:491` (`resolve_beat_assets`), `:561` (`_fp`), `:566` (`resolve_or_reuse`)
- `worker/tasks/render.py:142-151` (`db.refresh(cut)` re-check through `cut.duration_s = duration` — the exact point the new field assignment joins)
- `engine/publish/gate.py:12` (`unsafe_assets`), `:32` (`assert_safe_to_publish`)
- `worker/tasks/publish.py:45` (finalize branch), `:51` (`else:`), `:52` (existing `assert_safe_to_publish` call site)
- `api/models.py:85` (`Cut`), `:151-154` (`CutAsset` + its `UniqueConstraint`)
- `migrations/versions/` — confirms `0011_subtitle_caption_export.py` is current head, `0012` free
- `tests/test_publish_task.py:24` (`_cut()` helper), `:55-249` (all test names, confirming which six exercise the uploading branch)
- `tests/test_publish_gate.py` (full file — existing three-test fixture style, `db_session` from `tests/conftest.py:11`)
- `tests/test_render_task.py`, `tests/test_asset_sourcer.py` (confirmed existing fixture styles referenced in `required_tests`)
