# HF asset-generation gate extraction — module design

## Problem

`engine/render/asset_sourcer.py::resolve_beat_assets()` had two near-identical ~10-line
blocks — one for the HF video tier, one for the HF image tier — each: gate on
`reel_id is not None and source.api_key`, if gated wrap `source.generate(query)` in
`record_stage(...)`, set `ev.detail["cache_hit"]`, conditionally set `ev.cost_usd` only
when `source.last_call_was_generated`, else call `generate()` ungated. Surfaced as a
"Worth exploring" candidate by the `improve-codebase-architecture` architecture review
(candidate 2 of 3; candidate 1, `derive_on_screen` duplication, was folded into PR #30's
`guide_edit.py` extraction; candidate 3 shipped as PR #30 itself).

## Decision (settled via `/grilling`)

Narrow extraction only — explicitly NOT unifying all 4 asset sourcers (Pexels, Wikipedia,
HF video, HF image) into one `MediaSource` interface, since their `search()`/`generate()`
signatures are genuinely different and forcing a shared interface would be a shallow
abstraction (fails the deletion test — it would just move code around, not concentrate
complexity anywhere real).

`_generate_gated_hf_asset(db, reel_id, stage, source, query, cost_fn) -> SourcedAsset | None`
— a module-level private helper in `asset_sourcer.py` owning only the gate/record_stage/
cost-recording shell. It returns the raw result only:

- Never early-returns from `resolve_beat_assets()` — the fallback-chain tiering
  (Wikipedia → Pexels → HF Video → HF Image → None) stays entirely in the caller.
- Never calls `_cache_asset()` — that decision (what `asset_type` string, whether to
  return at all) stays in the caller.

`cost_fn(result) -> float` is a callable, not a fixed cost value, because
`hf_video_cost_usd(duration_s)` needs the GENERATED result's `duration_s` (only known
after `generate()` returns) while `hf_image_cost_usd()` takes no args. Each call site
supplies its own: `lambda r: hf_video_cost_usd(r.duration_s if r else 0.0)` for video,
`lambda r: hf_image_cost_usd()` for image.

## Test strategy

`tests/test_asset_sourcer_cost.py`'s 5 existing `resolve_beat_assets()`-level tests
(`test_real_hf_image_call_records_cost`, `test_cached_hf_image_call_records_zero_cost`,
`test_real_hf_video_call_records_cost_scaled_by_duration`,
`test_no_reel_id_skips_cost_tracking_entirely`,
`test_no_api_key_source_skips_stage_event_entirely`) are kept completely unmodified —
confirmed during grilling as the deliberate choice (not narrowed to call the new helper
directly), since they're also the only tests proving `resolve_beat_assets()`'s own
wiring (which stage name, which asset_type, that `_cache_asset()` still gets called)
stays correct after the extraction. 5 new direct, narrower tests were added alongside
for `_generate_gated_hf_asset()` itself: gated+real-call (cost recorded), gated+cache-hit
(zero cost), gated+no-result (event still recorded, cache_hit=False), ungated+no-api-key
(no event), ungated+no-reel-id (no event).

All 821 default-run tests (816 on `main` + 5 new) pass unmodified against the extraction
(822 total including the `golden`-marked test, deselected by default).

Mutation-tested: (1) removed the `if source.last_call_was_generated: ev.cost_usd =
cost_fn(result)` block — 3 tests failed for the right reason (`cost_usd` stayed `None`),
restored. (2) widened the gate from `reel_id is not None and source.api_key` to just
`reel_id is not None` — 2 tests failed for the right reason (a StageEvent was written for
a guaranteed-no-op call), restored.

## Corrections

None — no review findings yet; this section will be updated after the 4-persona review
round on the opened PR, per this pipeline's standard practice.
