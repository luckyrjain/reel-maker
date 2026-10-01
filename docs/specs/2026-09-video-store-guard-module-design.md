# video-store path guard extraction — module design

## Problem

`api/routers/cuts.py`'s three file-streaming endpoints (`stream_video`, `stream_subtitles`,
`stream_thumbnail`) each independently inlined the identical path-traversal guard:

```python
video_store = Path(settings.video_store_dir).resolve()
resolved = Path(cut.X).resolve()
if not resolved.is_relative_to(video_store):
    raise HTTPException(status_code=403, detail="Forbidden")
```

`stream_subtitles` and `stream_thumbnail`'s copies carried a `# see stream_video` comment
acknowledging the duplication without factoring it out. Surfaced as a "Strong" candidate by the
`improve-codebase-architecture` review (candidate 1 of 3). `stream_video` itself had zero direct
test coverage before this fix — no happy-path test, no reject-path test — despite being the
oldest and most load-bearing of the three.

## Decision (settled via `/grilling`)

`_resolve_within_video_store(path_str: str) -> Path` — a private helper local to
`api/routers/cuts.py` (not a new module; all 3 callers live in this one file, and the helper
raises `HTTPException`, which is router-layer/FastAPI-specific — unlike `guide_edit.py`'s
deliberate "no HTTP knowledge" business-logic module, there's no reason to keep this portable
outside FastAPI). It does both resolves (store root + target) and the check internally, raises
`HTTPException(403, "Forbidden")` on mismatch, and **returns the resolved `Path`** so callers
pass it straight to `FileResponse` — this also removes the duplicated `resolved = Path(...).resolve()`
line, not just the check.

By the time any of the three endpoints reaches the guard, their own 404/None/index-range checks
have already run — the helper never needs to handle `None`, since every call site already
guarantees a non-empty string.

## Test strategy

New tests in `tests/test_variants_router.py` (the existing home of `stream_thumbnail`'s and
`stream_subtitles`' own reject-case tests):
- Two direct tests of `_resolve_within_video_store()` itself: returns the resolved `Path` when
  inside the store, raises `HTTPException(403)` when outside.
- Four new tests for `stream_video` (previously zero): happy path, 404 on unset `video_path`,
  404 on a missing cut, 403 on a path outside the store.
- `stream_thumbnail`'s and `stream_subtitles`' existing tests (happy path, 404s, reject) kept
  completely unmodified — they're wiring coverage for the call sites, not narrowed away.

21 tests total in the file (15 existing + 6 new). All 824 tests in the suite pass (818 existing +
6 new).

Mutation-tested: (1) removed the `if ... raise HTTPException` check from the helper entirely —
all 4 reject-case tests (`stream_video`, `stream_subtitles`, `stream_thumbnail`, and the direct
helper test) failed correctly, proving one shared guard protects every call site. (2) bypassed
the helper specifically in `stream_video` (inlined a raw `Path(cut.video_path).resolve()` with no
check) — `test_stream_video_rejects_a_path_outside_the_video_store` failed correctly, proving the
wiring test isn't vacuous (it actually exercises the call, not just the helper in isolation). Both
restored and re-verified.

## Corrections

None yet — this section will be updated after the 4-persona review round on the opened PR, per
this pipeline's standard practice.
