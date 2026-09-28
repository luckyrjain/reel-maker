# System design: MoviePy video readers leak until worker recycle — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Open Issues table — `MoviePy video readers leak until worker recycle`
(Low severity)

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
