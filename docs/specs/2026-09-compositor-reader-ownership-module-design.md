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
`stack` parameter added to match the new real signature they wrap — 6 of the original 29 tests touched in
total. The remaining 23 tests that exercise `composite_cut()` directly, with or without `_ReaderTracker`,
needed **zero changes** — confirmed by direct read before implementing: they exercise the public
`composite_cut()` contract and observe reader open/close by patching the MoviePy class itself, never the
internal tuple shape. This is materially cheaper than the architecture review's own "~29 tests, several
directly destructure" concern.

## 5. Corrections — deep 4-persona review on the opened PR

A deep 4-persona review (Security/Red-Team, Correctness/Edge-Case, Test-Quality Auditor,
Documentation-Consistency) on the opened PR found and fixed one real security gap and one real
test-coverage gap before merge.

**Security/Red-Team**: `ExitStack`'s own unwind does not protect against a registered context's `__exit__`
itself raising — that close()-time exception REPLACES whatever exception was propagating, not the other
way around. A real `VideoFileClip.close()` calls into `FFMPEG_VideoReader.close()`, which can itself raise
(e.g. terminating/waiting on an already-dead ffmpeg subprocess) — plausible exactly when the render is
failing because of corrupt/malformed input media, the same condition already raising the real error the
stack is unwinding for. Without protection, a reader's close() failure during cleanup would silently
replace an operator-relevant corrupt-media error with an unrelated "close failed" message in `job.error`,
breaking this codebase's own invariant elsewhere (`worker/tasks/common.py`'s "errors while recording a
failure ... never mask the original exception"). Confirmed empirically, not assumed: a minimal repro
(`ExitStack` with one context whose `__exit__` raises, wrapping a body that raises a different exception)
showed the `__exit__`'s exception is what propagates. Fixed with `_closing()`, a small `@contextmanager`
wrapper used at every `stack.enter_context(...)` call site in `compositor.py` — its own `finally` block
catches and logs (never re-raises) a `close()`-time failure, so it can never mask whatever the stack is
actually unwinding for. `_build_media_sub_clip()`'s probe-clip manual `raw.close()` (the immediate discard
of a too-short probe clip, independent of `ExitStack`) was given the identical guard for the same reason.
Regression test `tests/test_compositor.py::test_composite_cut_propagates_the_original_exception_even_if_a_readers_close_fails`
mutation-tested by temporarily reverting `_closing()`'s use at the probe-open site back to a raw
`stack.enter_context(VideoFileClip(...))` and confirming the test fails with `AssertionError: Regex
pattern did not match ... Actual message: 'close failed'` — proving the original `RuntimeError` would have
been masked without the fix.

**Test-Quality Auditor**: `test_build_beat_clip_does_not_use_collage_for_one_or_three_items` lost the
property its pre-refactor version proved (`readers1 == readers3 == []` — a `None`/missing `media_path`
never opens a real reader) when the tuple return was dropped, with nothing added in its place. Confirmed
as a live gap by mutation-testing: a version of `_build_media_sub_clip()`'s black-frame branch that opens
and registers an unnecessary real `VideoFileClip` passed all 29 tests in the file, including this one,
undetected. Fixed by wrapping the test in `_ReaderTracker` and asserting `tracker.opened == []`,
re-verified to fail correctly against the same mutation.

**Correctness/Edge-Case and Documentation-Consistency**: no code defects found. Documentation-Consistency
found the Phase 7i/7n Key-Conventions bullets in `CLAUDE.md` (and the Phase 7 status paragraph's embedded
Phase-7i sentence) described the now-superseded `(clip, readers)` tuple mechanics without the same
"(Historical, pre-Phase-7r)" framing the module-layout entry already carried — fixed by adding the
identical framing to all three locations.

Full suite: 791 passed / 3 skipped / 1 deselected (795 total, unchanged), `ruff check --select F,E9 .`
clean. Mutation-tested: temporarily reverted `_build_media_sub_clip()`'s probe-clip open to a plain
`VideoFileClip(...)` (not registered on `stack`) and confirmed
`test_composite_cut_closes_every_video_reader_it_opens`,
`test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails`, and
`test_composite_cut_closes_readers_for_a_collage_beat` all fail with a real leaked-reader assertion
(`opened 2 readers, only closed 0`), then restored the fix and re-verified all 29 `test_compositor.py`
tests pass.
