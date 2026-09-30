# System design: 2-up side-by-side collage for multi-image beats

**Status:** Proposed, revised once after adversarial review (see §10). **Severity:** Low per
`docs/roadmap.md`'s Open Issues table ("No multi-image collage in one frame").

## 1. Problem

`docs/roadmap.md`'s Open Issues table: *"No multi-image collage in one frame — Currently cycles
sequentially; side-by-side layout not implemented."*

When a beat resolves more than one media item, `engine/render/compositor.py::_build_beat_clip()`
splits the beat's `duration_s` evenly across `len(media_paths)` and concatenates each item as its
own sequential sub-clip (`per = duration_s / len(media_paths)`). The realistic case where a beat
resolves >1 item at all: `engine/render/asset_sourcer.py::resolve_beat_assets()`'s Wikipedia branch
returns one photo per named person found in the beat's `visual_direction` (via
`_extract_all_person_names()`), when that beat names two or more people (e.g. a hook beat "Messi
and Ronaldo — two eras, one debate"). A 2-name beat today flashes photo A for half the beat, then
photo B for the other half, instead of showing both people together — a worse result than the
guide's own intent when the VO explicitly pairs two people in the same sentence.

## 2. What the current code actually does (read before designing)

- `resolve_beat_assets()`'s branches are **mutually exclusive**: the Wikipedia name-extraction
  branch only fires when `wiki` is configured and `_extract_all_person_names()` finds ≥1 match; if
  it returns any results at all, the function returns immediately with one `(Asset, Path)` pair per
  successfully-resolved name — **always real assets, never a `None` slot** (`if result:
  found.append(result)` — a name whose Wikipedia lookup fails is silently dropped, not padded with a
  placeholder). If zero names are found, the function falls through to `sourcer.search()` (Pexels,
  single item), then HF video (single item), then HF image (single item), then `[(None, None)]`
  (black frame). **A beat's `media_paths` is therefore either exactly 1 item (footage, an HF asset,
  or a black frame) or N real photos (N = names found), never a mix of video and image, and never a
  photo slot paired with a `None`.**
- `_NAME_RE` has no match-count cap — a beat could in principle name 3+ people — but this is rare in
  practice (the prompt's own instruction is "start `visual_direction` with the FULL NAME" of *the*
  player for a beat, singular; 2-name beats arise from beats that legitimately compare or pair two
  people, which the enrichment/generation prompts don't actively suppress).
- `CutAsset.order_in_beat` is already the deterministic left-to-right ordering — `resolve_or_reuse()`
  reads pins `.order_by(models.CutAsset.order_in_beat)`, and the order the Wikipedia branch writes
  pins in is exactly the order `_NAME_RE.findall()` matched names in the `visual_direction` string
  (left to right as written). No new ordering field is needed for a 2-up layout to put the
  first-named person on the left.
- `_build_media_sub_clip(media_path, duration_s)` already builds a full `TARGET_W × TARGET_H`
  (1080×1920) 9:16 clip uniformly for an image (`_fit_image_9_16` + `_ken_burns`), a video
  (`_crop_to_9_16` + `.subclipped()`), or `None` (a black `ImageClip`) — and returns `(clip,
  readers)`, where `readers` is every real `VideoFileClip` it opened, per the reader-leak discipline
  established in `docs/specs/2026-09-moviepy-reader-leak-system-design.md` and CLAUDE.md's Key
  conventions. `.cropped()`/`.resized()`/`.subclipped()` on an already-built clip are lazy
  transforms that share the same underlying reader via shallow copy (confirmed by that same design
  doc's account of the video-reader-leak fix) — they open **no new** `VideoFileClip`.
- `_build_beat_clip()` aggregates `readers` across every item in the beat and closes them itself on
  a later item's failure (`test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_
  the_same_beat_fails` in `tests/test_compositor.py` is the existing regression test for this,
  calling `_build_beat_clip([video, video], 2.0)` directly with two real video files).
- `_build_text_filter()` (the FFmpeg drawtext pass) is keyed **only** on `beats` and
  `beat_durations` — it has no visibility into how a beat's video track was assembled underneath it.
  It runs as a wholly separate FFmpeg pass over the finished MoviePy output (`composite_cut()`'s
  `_build_ffmpeg_args()` call happens after `final.write_videofile()`). A collage layout inside
  `_build_beat_clip()` cannot affect on-screen-text timing — confirmed by reading, not assumed.

## 3. Design decisions

**Trigger: exactly 2 items → collage; 1 item or 3+ items → unchanged sequential/single-clip
behavior.** A 2-up split is a simple, well-understood layout with no new grid-geometry code. A 3+
grid is a materially different layout problem (aspect ratio per cell, readability of 3+ small
photos on a 1080-wide frame) and the realistic 3+ case is rare (see §2) — building it now would be
solving a problem this roadmap item doesn't ask for. 3+ items keep today's sequential cycling
exactly as they do now; this is an explicit, bounded non-goal, not an oversight.

**No new settings/toggle.** Unlike `text_color`/`tts_voice` (subjective creative choices the
operator legitimately wants control over, and both already have a curated-value precedent in this
codebase), there is no legitimate reason an operator would prefer flashing two paired people one
after another over showing them together when the VO/guide already named them together in one beat
— this is a mechanical rendering improvement, not a creative one. Collage is unconditional for the
2-item case, mirroring how the drawtext-`expansion=none` fix and the MoviePy reader-leak fix were
both unconditional, no-flag changes to existing rendering behavior.

**Type-agnostic over image/video, for free.** Although §2 confirms only image+image pairs occur in
practice today, the implementation does not special-case media type — it reuses
`_build_media_sub_clip()` unchanged for each of the two items, which already handles image, video,
and `None` uniformly. This costs nothing extra and means a future sourcer change that ever produced
a 2-item video pair would compose correctly without a second implementation.

**Each half is a centered half-width crop of the existing full 9:16 clip — not a parallel
lower-resolution build.** Each item is built exactly as it is today, at full `TARGET_W × TARGET_H`
(so an image still gets its full Ken-Burns 8% zoom-in, and a video still gets its full
`_crop_to_9_16()` treatment) — the **only** new step is a `.cropped()` call taking the centered
`TARGET_W // 2 (540) × TARGET_H (1920)` strip out of that already-built clip, then
`.with_position()` into the left or right half of a new `CompositeVideoClip`. This is deliberately
the minimal-diff option: it reuses 100% of the existing per-item build logic (Ken Burns, video
loop-replica handling, black-frame fallback) with zero new parameters threaded into
`_build_media_sub_clip()`, and the crop step opens no new `VideoFileClip` readers (see §2), so the
existing reader-accounting (`readers` list, closed in `composite_cut()`'s `finally` block) needs no
new bookkeeping beyond what `_build_beat_clip()` already aggregates today.

Concretely: each item plays for the **full** beat `duration_s` (not divided — both halves are
visible simultaneously for the whole beat), whereas the existing sequential path still divides
`duration_s` by item count for 1- and 3+-item beats. This is the one behavioral change to how
`_build_media_sub_clip()` is *called* (the duration argument), not to the function itself.

**Nested centered crops stay centered — no drift.** `_ken_burns()`'s zoom keyframes are already
centered on the full 1080×1920 frame (`x1 = (w - cw) // 2`); the new half-width crop is centered on
that same frame too. Two centered crops compose to a still-centered result at every timestamp — no
independent math, no risk of the visible strip drifting off-center over the beat's duration.

**Known, accepted limitation — no subject-aware re-centering.** A center-crop of an already-9:16
photo down to a 540-wide strip is a materially narrower view (aspect ratio 0.28 vs. 0.56) and can
crop the edges of a face or body in an image that wasn't composed with a narrow crop in mind. This
codebase has no face-detection or subject-tracking infrastructure, and building one is out of scope
for a Low-severity layout fix — this is accepted as the same tradeoff real split-screen/duo video
layouts make elsewhere, not a defect to fix here.

## 4. Implementation sketch

`engine/render/compositor.py`:

```python
def _center_crop_half(clip):
    """Crop an already-9:16 (TARGET_W x TARGET_H) clip to its centered half-width
    strip, for the 2-up collage layout. A pure lazy MoviePy transform -- like
    .resized()/.subclipped() elsewhere in this file, it shares the underlying
    reader via shallow copy and opens no new VideoFileClip."""
    return clip.cropped(
        x_center=clip.w / 2, y_center=clip.h / 2,
        width=TARGET_W // 2, height=TARGET_H,
    )


def _build_collage_clip(media_paths: list[Path | None], duration_s: float):
    """2-up side-by-side layout for a beat with exactly two resolved media items,
    each playing for the FULL beat duration (not divided, unlike the sequential
    path). Returns (clip, readers) -- readers is exactly the two
    _build_media_sub_clip() calls' own readers; the returned CompositeVideoClip's
    own .close() does not cascade to them (see this module's other reader-leak
    notes), but nothing relies on it -- the flat readers list is what
    composite_cut() closes explicitly, unchanged from today."""
    left_clip, left_readers = _build_media_sub_clip(media_paths[0], duration_s)
    try:
        right_clip, right_readers = _build_media_sub_clip(media_paths[1], duration_s)
    except Exception:
        for r in left_readers:
            try:
                r.close()
            except Exception:
                pass
        raise
    left = _center_crop_half(left_clip).with_position((0, 0))
    right = _center_crop_half(right_clip).with_position((TARGET_W // 2, 0))
    collage = CompositeVideoClip([left, right], size=(TARGET_W, TARGET_H), bg_color=(0, 0, 0))
    return collage, left_readers + right_readers
```

`bg_color=(0, 0, 0)` (see §10, Correction 1) makes `CompositeVideoClip` build a plain opaque
background instead of MoviePy's transparent/alpha-mask compositing path — the two halves tile the
full frame exactly, so no background pixel is ever actually visible, but skipping the mask path
avoids real per-frame alpha-compositing work for no visual benefit.

`_build_beat_clip()` gains one branch, placed **after** the existing `duration_s = max(duration_s,
0.5)` floor and the `if not media_paths: media_paths = [None]` guard (see §10, Correction 2 — this
placement is load-bearing, not cosmetic):

```python
    duration_s = max(duration_s, 0.5)
    if not media_paths:
        media_paths = [None]
    if len(media_paths) == 2:
        return _build_collage_clip(media_paths, duration_s)
    per = duration_s / len(media_paths)
    ...
```

Everything else in `_build_beat_clip()` — the `per = duration_s / len(media_paths)` sequential path,
its own reader accounting and partial-failure cleanup — is untouched for the 1-item and 3+-item
cases.

## 5. Design-doc-template sections this codebase doesn't have literal analogues for

This is a single-process rendering-pipeline change, not a distributed system — several of this
template's sections are N/A by the nature of the change, recorded explicitly rather than omitted:

- **Components / APIs / Events** — no new component, HTTP endpoint, or async event. The only touched
  unit is `engine/render/compositor.py`'s beat-clip builder, called synchronously within
  `render_cut`'s existing Celery task body.
- **Data model** — no schema change. `CutAsset.order_in_beat` (already present) is read, not
  written, and already provides the left/right ordering a 2-up layout needs.
- **State machines** — none; this doesn't touch `Cut`/`Job`/`Reel` status transitions.
- **Consistency / Retries & idempotency** — unchanged; `render_cut`'s existing atomic-claim, retry,
  and atomic-MP4-write behavior is untouched by a change scoped entirely to
  `composite_cut()`'s internal clip assembly.
- **Capacity** — no new external call, no new dependency. **Correction (see §10, Correction 1):** the
  original draft of this section claimed a negligible CPU delta ("one extra `.cropped()` lazy-
  transform call, no new video decode"); adversarial review traced `CompositeVideoClip.__init__`'s
  actual behavior and found that without an explicit `bg_color`, it takes MoviePy's transparent/
  alpha-mask compositing path (a synthetic RGBA background `ColorClip` plus a nested masked
  composite) — real per-frame alpha-compositing work, not free. §4's implementation passes
  `bg_color=(0, 0, 0)` specifically to avoid this path; with that fix in place the per-collage-beat
  cost is genuinely small (two lazy crops plus a two-clip opaque blit), still no new video decode.

## 6. Failure strategy

Identical to today's: if either half's `_build_media_sub_clip()` call raises (corrupt media file),
`_build_collage_clip()` closes whatever it already opened (the first item's readers) and re-raises,
exactly mirroring `_build_beat_clip()`'s own existing partial-failure cleanup for the sequential
path — no new failure mode, no new exception type, no silent degrade. A single missing/nonexistent
path in a 2-item pair (defensive — §2 established real code never produces this today) renders as a
black half via `_build_media_sub_clip()`'s own existing `None`/missing-file handling, same as it
already does for a single-item beat. **Known gap, defensive-only (see §10, Correction 4):**
`worker/tasks/render.py`'s `black_frame_beat_indices` detection only flags a beat when *every* path
resolved is `None` — a collage beat with one real photo and one missing/black half would render
half-black but never surface in that operator-visible list. Since §2 establishes no code path
produces this mix today, this is recorded as a known limitation rather than fixed now; a future
`asset_sourcer.py` change that ever did produce a partial pair would silently reopen it.

## 7. Observability

No new signal needed — this doesn't add an external call, cost, or new failure class that
`StageEvent`/`job.error` don't already cover. A collage-specific bug would surface exactly like any
other render defect: a bad frame in the output video, or a render failure via the existing
`render_cut` job-failure path.

## 8. Rollout plan

No migration, no schema change, no flag. This changes `_build_beat_clip()`'s internal behavior only
— it takes effect the moment the code ships, for the next render of any 2-item beat (new reel
generation, or a re-render of an existing cut whose `visual_direction` names two people). Already-
rendered videos are untouched until their next render, identical to every prior compositor-only fix
in this codebase (drawtext `expansion=none`, reader-leak fix, audio fade).

## 9. Test plan

Extends `tests/test_compositor.py`, reusing the file's own established patterns (real-ffmpeg
fixtures via `_make_test_video()`, the `_ReaderTracker` reader-leak harness):

1. **Collage geometry, real ffmpeg** — build a 2-item beat from two distinctly-colored real test
   videos (`_make_test_video(..., color="red")` / `color="blue"`), render via `composite_cut()`, and
   sample a frame at the beat's midpoint: assert the left half's pixels are (approximately) red and
   the right half's are (approximately) blue — proves both items are visible **simultaneously**, not
   sequentially, and in the expected left/right order. This is the test that actually exercises the
   bug this design fixes; a test that only checks "doesn't crash" would pass against the old
   sequential code too.
2. **1-item and 3+-item beats unaffected** — a regression test asserting `_build_beat_clip()` with 1
   or 3 media items still produces output whose frame content matches the pre-existing sequential
   behavior (reuses the existing sequential-path assertions this file already has, extended to a
   3-item case if not already present).
3. **Reader-leak regression, extended** — `_ReaderTracker` wrapped around a `composite_cut()` call
   with a 2-item beat built from real video files, asserting every opened `VideoFileClip` is closed
   — the collage path's equivalent of `test_composite_cut_closes_every_video_reader_it_opens()`.
   `test_build_beat_clip_closes_an_earlier_items_reader_when_a_later_item_in_the_same_beat_fails`
   (already exists, already calls `_build_beat_clip([video, video], 2.0)` directly) continues to
   exercise `_build_collage_clip()`'s own partial-failure cleanup path once the 2-item branch routes
   there — no rewrite needed, since its mock (`flaky_build_media_sub_clip(media_path, duration_s)`)
   matches `_build_media_sub_clip()`'s unchanged signature exactly (this design deliberately never
   adds parameters to that function — see §3).
4. **`on_screen_text` timing unaffected** — a mutation-style sanity check (or reasoned-through
   confirmation in the PR description if a dedicated test would be redundant with existing
   `_build_text_filter()` coverage) that a 2-item beat's drawtext timing is identical to what it
   would be for a black-frame or single-item beat of the same `duration_s`/`on_screen_text` — proving
   §2's "text filter has no visibility into the collage" claim empirically, not just by inspection.
5. **Sub-0.5s duration floor** (added per §10, Correction 2) — `_build_beat_clip()` called with a
   2-item `media_paths` and a `duration_s` below the 0.5s floor (e.g. `0.1`) must still produce a
   collage built at the floored duration, not an unfloored one — the regression test for the branch-
   placement bug adversarial review caught; mutation-tested by moving the new branch before the floor
   line and confirming this test fails.
6. **One missing path in a 2-item pair** (added per §10, Correction 4) — `_build_collage_clip()`
   given `[real_path, nonexistent_path]` renders one real half and one black half without raising,
   proving the defensive-only claim in §6 rather than just asserting it.
7. **Frame-color sampling tolerance** (added per §10, Correction 5) — test 1's red/blue midpoint
   sampling asserts an explicit RGB tolerance band (not exact equality) to avoid flakiness from
   H.264 compression drift, per adversarial review's note.

Every new test is written to fail against the current sequential-only code first (mutation-tested by
temporarily reverting the `len(media_paths) == 2` branch and confirming test 1 fails for the right
reason — two sequential half-duration clips instead of one simultaneous composite), per this
pipeline's standing convention.

## 10. Corrections (adversarial review, before any code was written)

An independent review of this design doc against the actual code (`engine/render/compositor.py`,
`engine/render/asset_sourcer.py`, `worker/tasks/render.py`, `tests/test_compositor.py`, and MoviePy
2.1.2's installed source) confirmed §2's factual claims about `resolve_beat_assets()` and the reader-
leak/text-filter independence claims, and confirmed the MoviePy API usage (`.cropped()`,
`CompositeVideoClip` + `.with_position()`) works and tiles exactly as described. It found:

1. **Capacity understated** — `CompositeVideoClip` without an explicit `bg_color` takes MoviePy's
   transparent/alpha-mask path (real per-frame compositing work), not the "one extra lazy transform"
   originally claimed. Fixed by passing `bg_color=(0, 0, 0)` in §4's implementation — see §5's
   Correction note.
2. **Branch-placement bug, must-fix** — the original draft said the new `len(media_paths) == 2`
   branch goes "at the top, before the existing sequential loop," which was ambiguous about whether
   that's before or after `duration_s = max(duration_s, 0.5)`. Placed before the floor, a short real
   beat would reach `_build_collage_clip()` with an unfloored (possibly near-zero) duration,
   diverging from every other path's guarantee. §4 now states the branch goes after both the floor
   and the `if not media_paths` guard, with a regression test (test 5 in §9).
3. **Reader-leak mechanism, informational only** — review traced *why* `CompositeVideoClip.close()`
   not cascading is harmless here: everything it builds internally (the synthetic background, the
   nested mask composite) holds no real ffmpeg reader, so nothing is actually leaked. No design
   change; noted for the implementer's understanding.
4. **`black_frame_beat_indices` defensive gap** — see §6's added note. No code change (§2 establishes
   the mixed-pair case doesn't occur today), but recorded as a known limitation.
5. **Test-plan gaps** — no test proved the one-missing-path-in-a-pair case, the sub-floor-duration
   case, or the type-agnostic (video+image pair) claim in §3, and the red/blue frame-sampling test
   needed an explicit tolerance rather than exact equality. §9's test plan now includes tests 5–7 for
   the first three; the type-agnostic claim is accepted as covered by the existing per-type
   `_build_media_sub_clip()` test coverage rather than needing a fourth new collage-specific test,
   since `_build_collage_clip()` calls that function unmodified.
