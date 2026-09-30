# Module design — compositor.py reader-ownership refactor (CAR-3)

## 1. Problem

`engine/render/compositor.py`'s clip-building chain (`_build_media_sub_clip()`, `_build_beat_clip()`,
`_build_collage_clip()`, `composite_cut()`) threaded resource ownership manually: a function opens
`VideoFileClip`/`AudioFileClip` readers, returns `(clip, readers: list)`, and every caller extends its own
`readers` list and closes everything in a local `try`/`except`/`finally`. This exact discipline was
independently missed 5 times across 2 PRs (PR #19: `composite_cut`'s try-block placement, the per-beat
loop's placement, `_build_beat_clip`/`_build_media_sub_clip`'s own partial-failure paths, the sibling
`vo_tracks` path; PR #24: `_build_collage_clip()`'s crop/composite step) — a full-repository architecture
review flagged this as CAR-3, "Worth exploring."

## 2. Design

Replaced the manual `(clip, readers)`-tuple threading with `contextlib.ExitStack`, verified directly
against this repo's real `moviepy` install (not assumed from docs) to support what the design depends on:

- `VideoFileClip`/`AudioFileClip` both implement the context-manager protocol — `__exit__` calls
  `self.close()`.
- `Clip.close()` is idempotent (`if self.reader: ...`) — closing an already-closed clip a second time is a
  safe no-op.

`composite_cut()` opens `with ExitStack() as stack:` around the whole render body and threads `stack` as a
parameter into `_build_beat_clip()`, `_build_collage_clip()`, `_build_media_sub_clip()`. Each function
registers any real reader it opens via `stack.enter_context(...)` at the moment it opens it, and returns
just `clip` (not a tuple). Every function's own local `try`/`except: close what I opened; raise` block was
deleted — `ExitStack` unwinds automatically on any exception propagating out of the `with`, closing every
successfully-entered context in reverse order, regardless of which nested function raised or how deep.
`composite_cut()`'s VO-track loop registers each beat's `AudioFileClip` the same way, removing its manual
`audio_reader`-close-on-except dance (the reader is already on the stack the moment it's opened).

`final` (the whole-reel `CompositeVideoClip`) stays outside the stack — single top-level object, already
explicitly closed in the existing inner `try`/`finally`, never part of the 5x-recurrence bug class (that
was specifically about *nested* readers `CompositeVideoClip.close()` doesn't cascade to).

## 3. Rejected alternatives

**A hand-written `_ReaderPool` context-manager class** — functionally equivalent to `ExitStack`, but
duplicates a capability the stdlib already provides (confirmed above), and would need its own tests for
open/track/close/exception-unwind semantics that `ExitStack` already has in the standard library. Rejected
under the deletion test: it earns nothing `ExitStack` doesn't already provide for free.

**Keep `(clip, readers)` tuples, centralize the close logic into one `_close_all()` helper** — doesn't
address the actual bug class. All 5 historical recurrences were "a reader opened at some depth never made
it into the list a caller could see" — a shared close-helper still requires every function to correctly
build and propagate that list in the first place, leaving the exact defect intact.

## 4. Verification

All 4 function signatures changed (`_build_media_sub_clip`, `_build_beat_clip`, `_build_collage_clip` gain
a `stack: ExitStack` parameter; `composite_cut()` opens the stack internally, no public signature change).
5 direct-call test sites in `tests/test_compositor.py` needed updating to pass a `stack`:
`test_build_beat_clip_does_not_use_collage_for_one_or_three_items`,
`test_build_beat_clip_collage_respects_the_duration_floor`,
`test_build_collage_clip_with_one_missing_path_renders_a_black_half`,
`test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails`,
`test_build_collage_clip_closes_both_readers_when_the_crop_or_composite_step_fails` — plus 2 mocked
`side_effect` functions (`flaky_build_beat_clip`, `flaky_build_media_sub_clip` in
`test_composite_cut_closes_earlier_beats_readers_when_a_later_beat_fails_to_build` and
`test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails`) needed a
`stack` parameter added to match the new real signature they wrap. The remaining ~20 tests that exercise
`composite_cut()` directly, with or without `_ReaderTracker`, needed **zero changes** — confirmed by direct
read before implementing: they exercise the public `composite_cut()` contract and observe reader
open/close by patching the MoviePy class itself, never the internal tuple shape. This is materially
cheaper than the architecture review's own "~29 tests, several directly destructure" concern.

Full suite: 791 passed / 3 skipped / 1 deselected (795 total, unchanged), `ruff check --select F,E9 .`
clean. Mutation-tested: temporarily reverted `_build_media_sub_clip()`'s probe-clip open to a plain
`VideoFileClip(...)` (not registered on `stack`) and confirmed
`test_composite_cut_closes_every_video_reader_it_opens`,
`test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails`, and
`test_composite_cut_closes_readers_for_a_collage_beat` all fail with a real leaked-reader assertion
(`opened 2 readers, only closed 0`), then restored the fix and re-verified all 29 `test_compositor.py`
tests pass.
