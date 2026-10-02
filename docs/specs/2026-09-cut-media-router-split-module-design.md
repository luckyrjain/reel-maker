# cut_media.py router split — module design

## Problem

`api/routers/cuts.py` bundled three groups with no interface between them: (a) Cut
lifecycle orchestration (render/approve/publish/update + polling fragments), (b) raw
file-byte streaming (video/subtitles/thumbnail — `GET`, no template, no DB write), and
(c) variant-picker mutations (choose a thumbnail candidate, swap the hook-beat vo_script).
Surfaced as a "Worth exploring" candidate (candidate 2 of 3) by the
`improve-codebase-architecture` review.

The review's own sketch proposed splitting (a) from (b)+(c). Grilling found this wrong:
(c)'s two handlers both call `_cut_card()` — the same shared template-rendering helper
(a)'s four handlers use — and are both gated on `cut.status == "in_review"`, a
state-machine-shaped precondition. (b)'s three handlers never touch `_cut_card()` at all;
they return a raw `FileResponse`. The real seam is "renders `cut_card.html`" (a+c) vs.
"streams a raw file" (b), not the review's guessed split.

## Decision (settled via `/grilling`)

`api/routers/cuts.py` keeps (a)+(c) — the state machine, its polling fragments,
`active_job_for_cut()`/`latest_failed_job_for_cut()` (both called inside `_cut_card()`
and imported externally by `api/routers/reels.py` — unaffected since they stay put), and
`choose_thumbnail`/`choose_hook_variant`.

New `api/routers/cut_media.py` gets only the 3 streaming `GET`s
(`stream_video`/`stream_subtitles`/`stream_thumbnail`) plus `_resolve_within_video_store()`
(candidate 1's extraction — confirmed to have exactly these 3 callers and no others in
`cuts.py`). It has its own `APIRouter()`, mounted in `api/main.py` the same way as every
other `routers/*.py` module (`app.include_router(cut_media.router, prefix="/api")`) — no
re-export, no new pattern.

Pure reorganization: no route path, HTTP method, request/response shape, or
status-transition logic changed.

## Test strategy

`tests/test_variants_router.py` keeps the 8 tests for (c) (`choose_hook_variant`/
`choose_thumbnail`) — its existing name already fits once (b) moves out, no rename
needed. New `tests/test_cut_media.py` gets the 14 tests for (b) (the 3 streams + the
2 direct `_resolve_within_video_store()` tests), each `patch(...)` target updated from
`api.routers.cuts.settings` to `api.routers.cut_media.settings` to match the function's
new home.

828 tests total (unchanged — pure move, no tests added or removed).

Mutation-tested: commented out `cut_media.router`'s `include_router()` line in
`api/main.py` — 7 of 14 `test_cut_media.py` tests failed (every test that makes a real
HTTP request through the `TestClient`; the 7 that passed were the pure-function
`_resolve_within_video_store()` direct-call tests and 404-for-nonexistent-cut variants
that don't depend on the route being mounted), confirming the router wiring is genuinely
load-bearing. Restored and re-verified all 828 tests pass.

## Corrections

None yet — this section will be updated after the 4-persona review round on the opened
PR, per this pipeline's standard practice.
