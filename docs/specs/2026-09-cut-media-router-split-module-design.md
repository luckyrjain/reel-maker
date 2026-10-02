# cut_media.py router split — module design (Phase 7t)

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
`api/main.py` — 7 of 14 `test_cut_media.py` tests failed (every test that asserts a
200/403 through a real `TestClient` request; the other 7 pass vacuously — 5 already
expect 404 for an unrelated reason, and 2 call `_resolve_within_video_store()` directly,
bypassing routing entirely), confirming the mounting is genuinely load-bearing for the
tests that actually exercise it. Restored and re-verified all 828 tests pass.

## Corrections

A deep 4-persona review on the opened PR found Security/Red-Team and Correctness/Edge-
Case clean — independently confirmed byte-identical code at both the source and live
FastAPI-route level (no collision between `cuts.router` and `cut_media.router`), and the
mutation-testing numbers above independently reproduced exactly (7/14, as stated here —
this file's own wording was already accurate; a companion summary in CLAUDE.md had
overstated it as "every test fails" and was corrected to match). Documentation-
Consistency found `docs/architecture.md`'s module table and `docs/api.md`'s opening
sentence were left stale by the first pass — both still said every `/cuts/{cut_id}/*`
route, including the 3 file streams, lived in `cuts.py`. Fixed: `docs/architecture.md`
gained a `cut_media.py` row, and `docs/api.md`'s sentence now names both files. Also
added this doc's own `Phase 7t` label (CLAUDE.md used it throughout but this file hadn't,
until now).
