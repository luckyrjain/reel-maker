# System design: MoviePy video readers leak until worker recycle — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Open Issues table — `MoviePy video readers leak until worker recycle`
(Low severity)

> **Revision note (Lens A review of the built diff, after code existed):** an independent reviewer
> found a real, narrow gap the design's own §2 fix left open: the per-beat loop that populates
> `video_readers` originally ran **before** `composite_cut()`'s `try:` block, same as
> `_write_thumbnail_candidates()` did before the round-1 correction below. If `_build_beat_clip()`
> raised on beat N (e.g. a corrupt video file), every earlier beat's already-opened reader in that
> same call was leaked — not a regression (nothing closed them before this fix either), but an
> unaddressed instance of the exact same class of bug the round-1 correction fixed one call site
> over. Fixed by moving the whole per-beat loop (and the `final = concatenate_videoclips(...)`
> construction) inside the `try` block too — see the updated §2 below and the new
> `test_composite_cut_closes_earlier_beats_readers_when_a_later_beat_fails_to_build` regression test
> (mutation-tested: fails against the version with the loop still outside `try`).
>
> **A third gap, found by a round-2 re-review of the fixed code**, was one level deeper than either
> correction above could reach: `composite_cut()`'s own try/finally can only close readers that
> `_build_beat_clip()` actually *returns* to it. Within a single beat that has multiple
> `media_paths` (the multi-image-per-beat feature), if the second item's `_build_media_sub_clip()`
> call raised (corrupt media) after the first item had already opened a real reader, that reader
> was never returned anywhere — it only ever existed in `_build_beat_clip()`'s own now-abandoned
> local scope, unreachable to any outer `try`/`finally` no matter how far up the call stack it
> extends. The identical structural problem exists one level deeper still, inside
> `_build_media_sub_clip()`'s own loop-replica list comprehension (a short video looped to fill a
> longer beat — if replica 2 fails to open, replica 1's already-open reader is lost the same way).
> **Fix**: both `_build_media_sub_clip()` and `_build_beat_clip()` now wrap their own
> resource-accumulating work in a `try`/`except Exception: close whatever's already in the local
> readers list; raise` — each function is responsible for cleaning up its own partial state before
> propagating a failure, rather than relying on a caller that structurally cannot see that state.
> New regression test:
> `test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails`
> (calls `_build_beat_clip()` directly to isolate this layer; mutation-tested against a reverted
> version of this specific fix).
>
> **A fourth, sibling gap**, found by a follow-up review pass focused specifically on whether the
> `vo_tracks`/`AudioFileClip` side had the same problem the video-reader side did: it did.
> `composite_cut()`'s VO-track loop opens `AudioFileClip(str(vo_path))` first, then chains
> `.subclipped()`/`.with_effects()`/`.with_start()` — if any of those later steps raises, the
> enclosing `except Exception: _log.exception(...)` only logs and moves to the next beat; the
> already-opened `AudioFileClip` never reaches `vo_tracks.append(...)`, so the `finally` block's
> `vo_tracks` close loop can never reach it either. Same bug class, same file, one call site over
> from the three corrections above — narrow in practice (these are pure-Python metadata operations
> that rarely raise on a valid audio file), but real. **Fix**: capture the pre-transform
> `AudioFileClip` in its own variable (`audio_reader`) before any transform runs, and close *that*
> specifically in the `except` block — not whatever `track` currently holds — mirroring the video
> fix's own established pattern of always operating on the pre-transform object (chained
> `.subclipped()`/`.with_effects()`/`.with_start()` calls return new wrapper objects via shallow
> copy that share the same underlying `.reader`, so closing the original releases it regardless of
> which transform succeeded before the raise; the first draft of this fix instead closed whatever
> `track` currently was, which passed the *real* leak check but failed the regression test's
> identity-based opened-vs-closed comparison, since `.subclipped()` doesn't reconstruct via
> `AudioFileClip.__init__` — the reader was genuinely released either way, but the test caught the
> inconsistency with the established pattern before it shipped). New regression test:
> `test_composite_cut_closes_a_vo_track_opened_but_not_fully_built` (mutation-tested against a
> reverted version of this specific fix).

## 1. Problem and non-goals

**Problem, quoting the roadmap's own description verbatim:** *"`_build_media_sub_clip` opens
`VideoFileClip`s that only `worker_max_tasks_per_child=10` reclaims; marked with a `ponytail:`
comment."*

Root cause, confirmed by reading `engine/render/compositor.py` and MoviePy 2.1.2's own source
directly (not assumed):

- `_build_media_sub_clip()` (line 130) opens a real `VideoFileClip(str(media_path), audio=False)`
  for every non-image beat media path. For a clip shorter than the beat duration, it loops: opens a
  short-lived "probe" `VideoFileClip` to read `.duration` (explicitly `.close()`d immediately after,
  line 141 — this part is already correct), then opens **N more** `VideoFileClip` instances (one per
  loop repetition) inside a list comprehension passed straight into `concatenate_videoclips(...)` —
  none of these N readers are ever retained by name, so nothing can close them later.
- The returned clip is further wrapped: `_crop_to_9_16(raw)` (`.resized()`/`.cropped()`), then
  `.subclipped(...)`. MoviePy's fluent clip API builds these via shallow-copy-and-mutate
  (`copy.copy(self)` under the hood), so the transformed clip object shares the *same* `.reader`
  attribute value as the original — but it is still a *different Python object*, and nothing in this
  chain retains a reference to the untransformed original either.
- `_build_beat_clip()` (line 320) concatenates one or more of these sub-clips per beat via
  `concatenate_videoclips(sub_clips)` — called with **no `method=` argument**, so it defaults to
  `method="chain"`. Read `concatenate_videoclips`'s own source: the `"chain"` branch only sets
  `result.clips = clips` **inside an `if any(clip.mask is not None for clip in clips)` guard** — in
  the common case (no clip has an explicit mask), the resulting `VideoClip` retains **no reference at
  all** to its constituent `sub_clips`, only an implicit closure capture inside `frame_function`
  that `.close()` can never reach.
- `composite_cut()` (line 461) then concatenates all `beat_clips` into `final = concatenate_videoclips(beat_clips, method="compose")` — this time `method="compose"`, which returns a real
  `CompositeVideoClip`. Read `CompositeVideoClip.close()`'s own source directly: it closes `self.bg`
  (a synthetic, reader-less `ColorClip` — nothing to leak there) and `self.audio`, but **never
  iterates or closes `self.clips`** (the beat clips list it does store, unlike the chain case above).
- The existing `finally` block in `composite_cut()` already closes `vo_tracks` (the `AudioFileClip`
  list, by name — correct, already fixed for a prior audio-specific bug per this file's own
  module docstring) and calls `final.close()`. Per the trace above, `final.close()` reaches neither
  the beat-level nor the sub-clip-level `VideoFileClip` readers — the "ponytail:" comment already
  correctly anticipates that "video readers opened inside `_build_media_sub_clip` are still only
  reclaimed by `worker_max_tasks_per_child`."

**Confirmed, not assumed**: read `moviepy.Clip.Clip.close()` (no-op base), `VideoFileClip.close()`
(closes `self.reader`, the actual open file/ffmpeg-subprocess handle),
`CompositeVideoClip.__init__`/`.close()`, and `concatenate_videoclips`'s `"chain"` branch source
directly in the installed `moviepy==2.1.2` package before writing this design.

**Why this matters despite Low severity**: each open `VideoFileClip` holds an ffmpeg subprocess
(for decoding) plus open file descriptors. A long reel (many beats, several media items per beat)
leaks several of these per render; `worker_max_tasks_per_child=10` (CLAUDE.md, `rendering` queue)
bounds the damage to at most 10 renders' worth of leaked readers before a worker restart reclaims
them via OS process teardown — a real mitigation, which is why this has stayed Low severity rather
than an operational incident, but it is still real fd/process pressure that scales with render
volume and beat/media count per reel.

**Non-goals:**
- Removing or loosening `worker_max_tasks_per_child=10` — that remains a reasonable defense-in-depth
  backstop regardless of this fix (any *future* leak this fix doesn't anticipate is still bounded by
  it); this design closes the *known* leak, not the whole class of "moviepy might leak something"
  risk.
- Any change to `AudioFileClip`/`vo_tracks` handling — already correct (see module docstring), not
  touched.
- Any change to `_write_thumbnail_candidates()`'s own frame-reading logic itself (the sampling
  strategy, candidate count, etc.) — untouched. Its *call-site placement* is not a non-goal, though
  — see the Correction below.
- Adding `psutil`-based or subprocess-count-based test assertions — fragile and platform-dependent
  for CI. Verified instead by asserting `VideoFileClip.close()` (or `.reader.close()`) is actually
  *called* the expected number of times, which is what actually matters for the leak and is
  deterministic to assert.

## 2. Fix

**Correction (adversarial review round 1, before any code existed):** the first draft of this
section, and its own Non-goals claim above, assumed `_write_thumbnail_candidates(final,
thumbnail_path)` already runs inside `composite_cut()`'s existing `try`/`finally` block. It doesn't
— read the actual code: that call sits **before** the `try:` that wraps `final.write_videofile(...)`.
If `_write_thumbnail_candidates()` itself raises (e.g. a `get_frame()` failure on an edge-case
clip), the function exits before the `try`/`finally` ever begins — none of today's cleanup runs
(`vo_tracks`/`final` closes), and after this fix, the new `video_readers` close loop wouldn't run
either. This is a narrow, pre-existing gap (thumbnail extraction rarely fails on an
already-successfully-composited clip) — not introduced by this fix — but since this PR is already
rewriting this exact cleanup contract, it should close it rather than leave a stale, now-doubly-wrong
claim in the design. **Fix, folded into the plan below**: move `_write_thumbnail_candidates(...)`
to run *inside* the `try` block (immediately after `final = ...` is built, before
`final.write_videofile(...)`), so every code path past that point — including a
thumbnail-extraction failure — reaches the same `finally` cleanup.

**Correction 2 (Lens A review of the built diff — see the revision note at the top of this
document):** the same class of gap existed one step earlier and wasn't caught by round 1. The
per-beat loop that builds `beat_clips`/populates `video_readers`, plus
`final = concatenate_videoclips(beat_clips, method="compose")`, also ran **before** the `try:`
block. A `_build_beat_clip()` failure on beat N (corrupt media) left every earlier beat's readers
in that call leaked, for the identical reason correction 1 already fixed one call further down.
**Fix**: the whole per-beat loop and the `final = concatenate_videoclips(...)` construction move
inside the `try` block too — `try:` now starts immediately after `vo_tracks`/`video_readers`/
`beat_durations`/`notxt_path` are initialized, before the loop, not after it.

Thread the real, reader-owning `VideoFileClip` instances back out of `_build_media_sub_clip()` and
`_build_beat_clip()` as an explicit side list — the same shape `vo_tracks` already uses — so
`composite_cut()`'s existing `finally` block can close them by name, exactly like it already does
for `vo_tracks`.

```python
def _build_media_sub_clip(media_path, duration_s) -> tuple[clip, list[VideoFileClip]]:
    """Returns (visual_clip, readers) — readers is the list of real VideoFileClip
    instances this call opened (empty for an image or black-frame clip), so the
    caller can close them explicitly once done (concatenate_videoclips/
    CompositeVideoClip.close() do not reach nested clips — see design doc)."""
    if media_path and media_path.exists():
        if is image: ... return clip, []
        else:
            raw = VideoFileClip(...)
            if too short:
                loops = ...
                raw.close()
                readers = [VideoFileClip(...) for _ in range(loops)]
                raw = concatenate_videoclips(readers)
            else:
                readers = [raw]
            return _crop_to_9_16(raw).subclipped(0, duration_s), readers
    return black_frame_clip, []
```

`_build_beat_clip()` aggregates the per-media-item `readers` lists across all `media_paths` for a
beat into one flat list, returned alongside the beat clip. `composite_cut()` aggregates across all
beats into one flat `video_readers: list[VideoFileClip]` (parallel to the existing `vo_tracks: list[AudioFileClip]`), and the `finally` block gets one more loop, identical in shape to the existing
`vo_tracks` loop:

```python
for reader in video_readers:
    try:
        reader.close()
    except Exception:
        pass
```

Removed: the "ponytail:" comment (its own anticipated fix — "thread the clips back out of the
builder" — is exactly what this does; the comment is resolved, not just annotated).

**Why return the pre-transform `raw`/loop-replica objects, not the final transformed clip**: MoviePy's
`.resized()`/`.cropped()`/`.subclipped()` chain is a fluent API that returns new wrapper objects via
shallow copy — closing whichever object still holds the live `.reader` reference closes the shared
underlying resource regardless of which wrapper you call `.close()` through, so either would work
functionally. Returning the pre-transform objects is chosen because it's the object this file
*already* names and holds a reference to at the point of creation (`raw`, and the loop's list
comprehension elements) — no need to introspect the transformed clip to find its `.reader` attribute
name, and it stays close to the existing pattern the probe-clip's own `raw.close()` (line 141) call
already establishes in the same function.

## 3. Data model / API / events

None. Pure internal function-signature change, fully contained within
`engine/render/compositor.py`. `composite_cut()`'s own public signature and return type
(`tuple[float, list[Path], Path | None]`) are unchanged — this only touches two `_`-prefixed private
helper functions and the `finally` block of the function that already calls them.

## 4. Consistency / state machines

Unaffected — no DB writes, no state transitions in any part of this call chain.

## 5. Failure strategy

The new close loop follows the exact same `try/except Exception: pass` shape the existing
`vo_tracks`/`final` closes already use in this `finally` block — a failure to close one reader
(e.g. the underlying ffmpeg subprocess already exited) must not prevent closing the rest, and must
never mask whatever exception (if any) is already propagating out of the `try` block above it. No
new exception surface introduced.

## 6. Observability

None added or needed — this is a resource-cleanup correctness fix with no new failure mode to
report; a failure to close (caught and swallowed, matching existing sibling loops) has no
operator-facing signal today for the *existing* `vo_tracks`/`final` closes either, so this stays
consistent rather than introducing an inconsistent instrumentation asymmetry for one specific
resource type.

## 7. Test plan

`tests/test_compositor.py` currently only exercises the real-ffmpeg path with `beat_video_paths=[[None]]` (no real video file, black-frame `ImageClip`, no reader to leak) — the video-reader leak
path is entirely untested today. New coverage needed:

1. A small real MP4 test fixture, generated via `ffmpeg -f lavfi -i color=...` (no external network
   dependency, consistent with this file's own "real ffmpeg" testing philosophy) at test time into
   `tmp_path`.
2. Patch **both** `VideoFileClip.__init__` and `VideoFileClip.close` (not close alone — proving
   "every instance actually opened was closed" needs the opened-set to diff against, not just a
   closed-call counter that could pass vacuously if construction itself is undercounted) to track
   which instances were constructed and which were closed while running `composite_cut()` end-to-end
   with a real video `beat_video_paths` entry, and assert the two sets are equal by the time
   `composite_cut()` returns — this is the direct, deterministic regression guard (not an fd-count or
   process-count assertion, which would be fragile/platform-dependent per §1's Non-goals).
3. Mutation-test this new test yourself: temporarily revert the fix (stop threading `readers` out,
   or drop the new `finally` close loop), confirm the test fails (some `VideoFileClip` instance is
   never closed), then restore.
4. A beat whose video is shorter than the beat duration (exercises the loop-replica path, multiple
   `VideoFileClip` instances per beat) should also be covered, since that's the specific code path
   with the most instances to leak (the probe `raw` plus N loop replicas).
5. Confirm the existing `test_composite_cut_preserves_vo_audio` (image-only, `beat_video_paths=[[None]]`) still passes unmodified — the fix must be a no-op for the no-real-video-file case.
6. Cover the §2 correction: `_write_thumbnail_candidates()` failing (mock it to raise) must still
   reach the cleanup path and close every already-opened `VideoFileClip` — this is the one new
   failure-path branch the correction adds, and should have its own explicit test rather than only
   being implied by the happy-path move.
7. Cover correction 2: a later beat's `_build_beat_clip()` raising must still close every earlier
   beat's already-opened readers in that same call — patch `_build_beat_clip` to succeed on the
   first call (delegating to the real implementation against a real video fixture) and raise on the
   second, and assert the tracker's opened/closed sets are still equal despite the raise.

## 8. Rollout plan

Single-PR, no migration, no config, no flag. Purely internal to one render-pipeline module; safe to
deploy/revert independently. `worker_max_tasks_per_child=10` stays in place as a backstop (see
Non-goals) — no coordinated config change needed alongside this fix.

## Readiness verdict: **Ready for implementation planning.**

Root cause independently confirmed by reading MoviePy 2.1.2's actual installed source
(`Clip.close`, `VideoFileClip.close`, `concatenate_videoclips`'s `"chain"` branch,
`CompositeVideoClip.__init__`/`.close()`), not inferred from the roadmap description alone. Fix is a
function-signature threading change confined to one file, matching an existing sibling pattern
(`vo_tracks`) already in the same function. No existing test currently exercises the leaking code
path at all — new coverage is additive, not a modification of anything existing.
