# System design: word-level caption export (SRT) — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Phase 5, item 5d ("Word-level caption export — not done")
Scope handed to this design: *"After Whisper transcription in the compositor, write per-beat
segments to an SRT file alongside the MP4. Pass the SRT to YouTube Data API as a caption track
on upload."*

This is a small, additive feature layered onto an existing pipeline stage (`composite_cut()`
already runs Whisper transcription today, just for a different purpose). The design below is
scoped accordingly — no new job type, no new queue, one new nullable column.

---

## 1. Problem and non-goals

**Problem:** `composite_cut()` already transcribes each beat's VO with Whisper for on-screen
burned-in text timing (`_build_text_filter()`), but the transcript is discarded after the
`ffmpeg drawtext` filter string is built. There is no standalone caption/subtitle file produced,
so:
- An operator has no way to get accurate captions for a platform that doesn't support burned-in
  text well, or wants real (non-burned-in) closed captions.
- YouTube Shorts uploads have no caption track — YouTube's own auto-captions apply, but they're
  lower quality than the Whisper transcript this pipeline already computes and discards.

**Non-goals (explicitly out of scope for this design):**
- VTT export. The roadmap item title says "SRT/VTT" but the *Work* section only specifies SRT +
  YouTube upload. SRT is the only format YouTube's Captions API requires; VTT is listed as an
  **open question** (§9), not built now — a second writer function later, not a redesign.
- Instagram/TikTok caption-track upload. Neither platform's API this codebase integrates with
  (`engine/publish/instagram.py`, `engine/publish/tiktok.py`) exposes an equivalent to YouTube's
  `captions.insert`. TikTok publishing is already `NotImplementedError` by design (see CLAUDE.md).
  The SRT file is still generated and downloadable for every platform's cut — only the
  *automatic upload* is YouTube-only.
- Editing captions in the review UI. Out of scope; this ships the file, not an editor.

---

## 2. Components

No new services, workers, or queues. Three existing modules gain new responsibility; one new
small module is added.

| Component | Change |
|---|---|
| `engine/render/captions.py` | **Extended.** `transcribe_audio()` currently discards Whisper's sentence-level `segments` and returns only flattened word-level `CaptionSegment`s. Add a second accessor that also returns segment-level text so a single Whisper pass serves both the existing burned-in-text consumer and the new SRT writer — see §3 for why this must not become two separate Whisper calls. |
| `engine/render/srt.py` | **New.** Pure formatting module: `list[CaptionSegment] -> .srt file`. No I/O beyond the write; no ffmpeg, no network. Mirrors `engine/render/compositor.py`'s existing separation between "build a filter/data structure" (pure, unit-testable) and "run ffmpeg" (subprocess, integration-tested). |
| `engine/render/compositor.py::composite_cut()` | **Extended.** After building `beat_transcripts` (already happens today for the drawtext pass), also derive absolute-timeline caption cues and call `srt.write_srt()`. Returns the subtitle path (or `None`) as a fourth return value. |
| `worker/tasks/render.py::render_cut` | **Extended.** Reads the new return value, stores it on `Cut.subtitle_path`, same pattern as `thumbnail_candidates`. |
| `engine/publish/youtube.py::YouTubePublisher` | **Extended.** After a successful video upload, if `cut.subtitle_path` exists, best-effort POST to YouTube's Captions API. Failure here must never fail the publish job — see §7. |
| `api/routers/cuts.py` | **New endpoint.** `GET /api/cuts/{id}/subtitles` — serves the `.srt` file, same path-traversal guard as `stream_video`/`stream_thumbnail`. |
| `ui/templates/fragments/cut_card.html` | **Extended.** A "Download captions (.srt)" link next to the existing video/thumbnail actions, shown whenever `cut.subtitle_path` is set (not gated to `in_review` — same visibility rule as the video download itself). |

### Why extend `captions.py` instead of adding a parallel transcription call

Whisper transcription is the most expensive step this feature touches (CPU-bound, seconds per
beat — CLAUDE.md already flags `base` model speed as a known Low-severity issue). If the SRT
writer called Whisper again independently, every render would pay for transcription **twice**
per beat with VO audio. `transcribe_audio()` already loads the model once per process via
`@lru_cache` and calls `model.transcribe(..., word_timestamps=True)` once per beat — that single
call's result (`result["segments"]`, each with its own `.text`/`.start`/`.end` *and* nested
`.words`) already contains everything both consumers need. The fix is to stop discarding half of
that result, not to call Whisper twice.
(Verified against the real `openai-whisper` source during design review, not assumed from
documentation — `transcribe.py`'s `new_segment()` and `timing.py`'s `add_word_timestamps()` do
produce exactly this shape: each segment dict carries its own `text`/`start`/`end`, and
`add_word_timestamps()` mutates it in place to add a nested `"words"` list when
`word_timestamps=True`, which is exactly what today's `transcribe_audio()` already reads.)

Concretely: refactor `transcribe_audio()` internals so both the existing word-level output and a
new sentence/segment-level output are derived from one `model.transcribe()` call within the same
function invocation: `transcribe_audio(path) -> TranscriptResult` with
`.words: list[CaptionSegment]` (today's return value, unchanged for the existing caller) and
`.segments: list[CaptionSegment]` (new — one `CaptionSegment` per Whisper segment, `text` = the
full segment text rather than one word). `composite_cut()` already calls this once per beat per
render (line ~471 today, called with no `beat_offset_s` argument — i.e. today's default `0.0`);
it keeps calling it exactly the same way, just reads one more field off the result.

**Offset handling — both fields stay beat-relative at the source, exactly as `.words` already
is.** `transcribe_audio()` does **not** gain a caller-supplied absolute offset. `.words` is
consumed today by `_whisper_timestamps()` (`compositor.py`), which itself adds the beat's
cumulative start time (`beat_start`) to convert beat-relative → absolute for the burned-in-text
overlay. If `transcribe_audio()` were instead called with an absolute `beat_offset_s` so `.words`
came back pre-shifted, `_whisper_timestamps()` would add `beat_start` a *second* time and every
beat past the first would get wrong drawtext timing — a real, silent regression the first draft
of this design would have introduced (caught in review; `_whisper_timestamps()`'s existing
`abs_start = beat_start + first.start_s` line is the tell). The fix: `.segments` follows the exact
same pattern `.words` already uses — `composite_cut()` computes the cumulative per-beat offset
itself (an explicit running sum over `beat_durations`, not the loop variable from the first
`for beat, media_paths, vo_path in zip(...)` loop, which has already been fully consumed by the
time the transcription step runs) and adds it to each `CaptionSegment.start_s`/`end_s` **at the
SRT-cue-building call site**, the same place `_whisper_timestamps()` already does it for `.words`.
`transcribe_audio()`'s signature and behavior for `.words` are therefore genuinely unchanged, not
just "unchanged for the existing caller" in name only.

---

## 3. API surface

No new HTTP APIs beyond the one read-only download route; no new job/task signatures beyond an
extra return value threaded through two existing internal calls.

### 3.1 Internal: `engine/render/captions.py`

```python
@dataclass
class TranscriptResult:
    words: list[CaptionSegment]      # existing shape, beat-relative timestamps — unchanged
    segments: list[CaptionSegment]   # new — one per Whisper segment, also beat-relative

def transcribe_audio(audio_path: Path, beat_offset_s: float = 0.0) -> TranscriptResult:
    ...  # beat_offset_s keeps its existing default-0.0, beat-relative meaning for BOTH fields —
         # see §2's "Offset handling" note. composite_cut() still never passes a non-zero value;
         # the parameter is kept only because tests may want to construct offset transcripts directly.
```

Breaking-change note: `transcribe_audio()`'s return type changes from `list[CaptionSegment]` to
`TranscriptResult`. Its only current caller is `composite_cut()` (verified — no other call sites
in `engine/` or `worker/`), so this is a same-PR, coordinated change, not a compatibility concern.
`tests/test_audio_text_sync.py` (which exercises `_build_text_filter()`'s whisper-timestamp path)
gets updated in the same change, plus a new explicit regression test asserting that adding the
`.segments` field does not change `.words`' values or `_whisper_timestamps()`'s resulting drawtext
timestamps for a multi-beat reel (a direct guard against the offset-doubling bug described in §2).

### 3.2 Internal: `engine/render/srt.py`

```python
def write_srt(cues: list[CaptionSegment], path: Path) -> Path | None:
    """Write standard SRT format. Returns `path` on success, None if `cues` is empty
    (nothing to write — not an error, matches the existing "no Whisper installed"
    degrade-gracefully behavior elsewhere in this module)."""
```

Cue source: `TranscriptResult.segments` (§2), concatenated across all beats, each beat's
`CaptionSegment.start_s`/`end_s` shifted to the reel's absolute timeline by `composite_cut()` at
the point cues are built — a running sum over `beat_durations` computed explicitly for this step
(that list is retained after the first loop; the first loop's own `t` variable is not reused, see
§2). This is the same shift `_whisper_timestamps()` already applies to `.words`, just performed at
the SRT-building call site instead of inside `transcribe_audio()`. This is
*better* source material than reusing the on-screen `on_screen_text` lines: those are capped at 5
lines per beat and word-count-truncated for the burned-in overlay (`clean_guide()`/`_derive_on_screen()`);
a real caption track should cover the full VO, not a truncated summary of it.

**Whisper-unavailable fallback:** `_build_text_filter()` already has a proportional
(word-count-based) fallback when Whisper isn't installed, operating on
`vo_script`-derived sentences. `composite_cut()` reuses that same sentence split
(`re.split(r"[.!?—]+", vo)`) to build fallback SRT cues with proportional timing when
`TranscriptResult.segments` is empty — same technique, not a second implementation. Degrades to
`Cut.subtitle_path = None` only if `vo_script` is also empty for every beat (e.g. `silent`
voiceover mode) — nothing to caption.

### 3.3 External: YouTube Captions API

`POST https://www.googleapis.com/upload/youtube/v3/captions?uploadType=multipart&part=snippet`
(existing pattern in `engine/publish/youtube.py` — same auth header style, same
`get_valid_access_token()` reuse). Multipart body: JSON `snippet` (`videoId`, `language: "en"`,
`name: ""`, `isDraft: false`) + the `.srt` file bytes as the media part.

**Assumption flagged, not verified against a live call in this design pass:** YouTube's Captions
API accepts SRT directly (documented, format auto-detected from content) — this is stated in
Google's public API reference but this design has not made a live test call. Implementation
should do one manual verification call against a real connected YouTube account before treating
this as done, the same way Phase 7c's startup validation and Phase 4b's OAuth flows were each
verified against real accounts/instances per CLAUDE.md's stated practice, rather than trusting
docs alone.

### 3.4 New route: `GET /api/cuts/{id}/subtitles`

Mirrors `stream_video`/`stream_thumbnail` exactly: 404 if `cut.subtitle_path` is unset, 403 if the
resolved path (via `Path.resolve().is_relative_to(VIDEO_STORE_DIR)`) falls outside the video
store — same guard CLAUDE.md calls out as non-negotiable for the existing two streaming routes.
Content-Type `application/x-subrip`. No new auth model — this app is a single-operator tool with
no per-request auth today (see `api/oauth.py`'s own "single-operator tool" note); this route
follows that existing posture, not a new one.

---

## 4. Data model

One new nullable column, following the exact precedent of `thumbnail_path`/`black_frame_beat_indices`:

```python
# api/models.py, class Cut
subtitle_path = Column(String(500))
```

Migration `migrations/versions/0011_subtitle_caption_export.py` — additive, nullable, no backfill
needed (existing rows simply have `subtitle_path = None`, meaning "not yet re-rendered under this
feature," same semantics as `black_frame_beat_indices` being `None` for pre-Phase-7a rows).

No new table. A caption *track id* returned by YouTube's `captions.insert` is **not** persisted
(see §7's idempotency discussion for why, and the accepted limitation that follows from that
choice).

---

## 5. State machines

No change to `REEL_TRANSITIONS`/`CUT_TRANSITIONS`. Subtitle generation is not a gated lifecycle
step — it's an artifact produced as a side effect of the existing `render_cut` job, exactly like
`thumbnail_candidates`. It does not block `in_review`, does not require operator approval, and a
missing/`None` subtitle path never prevents render, review, or publish (same "enhance, never
block" posture as `hook_variants`, `black_frame_beat_indices`).

---

## 6. Consistency and idempotency

- **Render-time (producer):** `subtitle_path` is written inside `render_cut`'s existing job body,
  inside the same `record_stage(db, reel.id, "composite", ...)` block that already wraps
  `composite_cut()`. It inherits `job_task`'s existing atomicity: the column write only becomes
  visible with the job's fenced done-stamp commit, same as `video_path`/`thumbnail_path` today.
  No new failure mode is introduced — if SRT writing raises, it fails `render_cut` exactly like a
  failure anywhere else inside `composite_cut()` already would (this is intentional: a broken
  subtitle write should not silently succeed as a "done" render with `video_path` set but corrupt
  captions — the whole render either lands as one lifecycle-consistent unit or fails as one).
- **Re-render:** `subtitle_path` is replaced wholesale on every render, same explicit policy
  already documented for `thumbnail_candidates`/`video_path`/`black_frame_beat_indices` — no
  merge, no versioning.
- **Publish-time (consumer, YouTube only):** the caption upload call happens strictly *after*
  `cut.platform_post_id` is committed (the existing early-commit-after-upload step in
  `publish_cut`, done specifically so a retry never re-posts the video — see CLAUDE.md's
  `publish.py` notes). Captions.insert is **not idempotent** on YouTube's side — calling it twice
  can create two caption tracks — so this design does **not** attempt the upload on the existing
  "already has `platform_post_id`, finalize without re-uploading" branch of `publish_cut`. That
  branch exists specifically for the rare crash-after-video-upload-before-done-stamp case; on that
  path, whether captions were already attempted is unknown, and the safe choice is to skip rather
  than risk a duplicate track. This is an accepted, explicitly-stated limitation (§9), matching
  this codebase's existing tolerance for similar rare-path gaps (e.g. the documented
  "Retry publish on a failed cut can ship a stale pre-edit video" entry in `docs/roadmap.md`'s
  Open Issues table) rather than adding new schema (a `youtube_caption_track_id` column plus
  upsert-or-replace logic) to close a rare edge case for a non-critical artifact.

---

## 7. Failure strategy

**Render-time:** subtitle generation failing fails the render (§6) — it runs inside the same
transcription step the burned-in text already depends on, so if Whisper itself is broken, the
render was already going to degrade (falls back to proportional timing, not a hard failure) or
fail (a genuine Whisper crash, not "not installed") independent of this feature.

**Publish-time (the new failure surface):** the YouTube captions upload call **must be
best-effort and must never fail the `publish_cut` job**. The video is already live and
`cut.platform_post_id` already committed by the time this call happens — failing the job here
would incorrectly surface "publish failed" for a post that is, in fact, live, and would put the
operator through a needless "check the platform before retrying" caution (the exact caveat
`publish_cut`'s own comments already document for the *video* upload's transient-failure case,
which would be actively wrong to apply to a captions-only failure).

**This has to compose correctly with `record_stage()`, which does not swallow exceptions by
itself** — `engine/observability.py::record_stage` sets `ev.ok = False` and commits a `StageEvent`
on an exception inside its `with` block, but then **re-raises**. Wrapping the upload in
`record_stage(...)` with no inner try/except would fail `publish_cut` (the opposite of this
section's whole requirement); catching the exception *inside* the `with` block without touching
`ev` would keep the job alive but the `StageEvent` would record `ok=True` (its default) for a call
that actually failed — silently losing the one signal an operator has that a caption upload
didn't happen (§4 deliberately doesn't persist a caption-track id, so this `StageEvent` is the
*only* record of success/failure). The two requirements — never fail the job, and record the
failure truthfully — are only both satisfied by catching inside the `with` block and explicitly
setting `ev.ok`/`ev.detail` before letting the function return normally:

```python
if cut.subtitle_path:
    with record_stage(db, cut.reel_id, "captions_upload", cut_id=cut.id, provider="youtube") as ev:
        try:
            upload_captions(video_id, cut.subtitle_path, access_token)
        except Exception as exc:
            ev.ok = False
            ev.detail["error"] = repr(exc)
            _log.exception(
                "caption upload failed for cut %s (video is live, video_id=%s)", cut.id, video_id
            )
```

This matches the established pattern for every other "nice-to-have, never block the core
artifact" step in this codebase — `generate_hook_variants()` (`[] on any failure — this runs
after the guide already cleared the quality gate`) and thumbnail-candidate generation are the
closest precedents — combined with `record_stage`'s actual (re-raising) semantics rather than an
assumed swallow-everything behavior.

**Observability:** the `record_stage(db, cut.reel_id, "captions_upload", cut_id=cut.id,
provider="youtube")` wrapper above is the mechanism, not a separate add-on — same convention as
every other slow/networked call site in this codebase. It is not a paid call (doesn't affect
`paid_call_count()`'s NVIDIA-only budget gate) — this is a pure operator-visibility stage event,
giving a per-cut success/failure/latency row in the existing pipeline panel without adding a new
UI surface, *provided* the failure path explicitly sets `ev.ok = False` as shown above (the
default `ev.ok=True` would otherwise make a failed upload indistinguishable from a successful one
in that panel).

---

## 8. Capacity

Negligible. No new external calls at render time (Whisper already runs; this reads more of its
existing output). One new HTTP call at publish time, added only for YouTube cuts with VO, adding
low seconds of latency to a publish job whose `max_runtime_s` is already 60 minutes — no runtime
budget concern. SRT files are plain text, single-digit KB for a <90s reel; no meaningful storage
growth against `VIDEO_STORE_DIR`, which already holds the MP4 for the same cut.

---

## 9. Open questions (not blocking, explicitly deferred)

1. **VTT export.** Deferred — no current consumer needs it (YouTube accepts SRT; the roadmap's
   Work section only specified SRT). If a future platform integration needs VTT, `srt.py` gains a
   sibling `write_vtt()` operating on the same `CaptionSegment` cues — no redesign, since the cue
   data model is already format-agnostic.
2. **YouTube Captions API contract verification.** Flagged in §3.3 — needs one live call against a
   real connected account before considering this feature done, per this codebase's established
   practice of verifying external integrations against the real service rather than docs alone.
3. **Finalize-path caption upload gap.** Accepted limitation from §6 — a `publish_cut` re-run that
   hits the "already posted, finalize only" branch will not attempt a caption upload even if the
   original attempt never got to try. Rare (requires a crash between video-upload success and
   done-stamp commit specifically), and the operator can always re-run
   `GET /api/cuts/{id}/subtitles` and upload manually via YouTube Studio as a manual fallback —
   not worth new schema for. A narrower, earlier window exists too: a crash *during*
   `publisher.publish()` itself (video uploaded, captions call attempted or in flight, but the
   function never returns so `platform_post_id` never commits) leaves the job stuck `running` for
   the reaper to fail; an operator retry then re-enters the full upload path and re-uploads both
   the video *and* the captions against a new `video_id`. This is not a new risk this feature
   introduces — it's the same pre-existing "crash before `platform_post_id` commits = full
   double-post" risk `publish_cut`'s `max_retries=0` design already accepts for the video itself
   (see CLAUDE.md's Reliability features) — captions simply inherit it, rather than this being a
   second, separate gap to design around.
4. **Instagram/TikTok caption upload.** Not attempted — no equivalent API surface identified in
   this codebase's existing publisher integrations. The `.srt` file remains downloadable for every
   platform's cut regardless (§3.4), just not auto-attached.

---

## 10. Rollout plan

Single PR, no phased flag needed — this is additive (nullable column, new optional file, new
best-effort publish step) and every existing test/behavior is unaffected by a `None`
`subtitle_path`.

1. Migration `0011_subtitle_caption_export.py` (`Cut.subtitle_path`), verified against real
   Postgres (`upgrade head` / `downgrade -1` / `upgrade head` round-trip) per this codebase's
   established migration-verification practice.
2. `captions.py` refactor to `TranscriptResult` (§3.1), with `tests/test_audio_text_sync.py`
   updated for the new return shape — mutation-test the refactor by temporarily reverting to
   confirm the whisper-timestamp-path tests actually exercise the new field, not just the
   unchanged `.words` field.
3. `engine/render/srt.py` — new module + `tests/test_srt.py` (format correctness: timestamp
   formatting `HH:MM:SS,mmm`, sequential numbering, empty-cues returns `None`, cues spanning
   multiple beats concatenate with correctly-offset absolute times).
4. `compositor.py::composite_cut()` extended to build cues and call `write_srt()`, returns the
   path as a fourth tuple element. Extend `tests/test_compositor.py`'s existing real-ffmpeg tests
   plus `tests/test_golden_reel.py` (the real end-to-end, no-mocks test from Phase 7d) to assert a
   real `.srt` file is produced with real Whisper-or-fallback timing — this is exactly the kind of
   "actually runs the real chain" test Phase 7d's golden test was built for, and the natural place
   to add this assertion rather than a new isolated golden test.
5. `render.py::render_cut` stores the path on `Cut.subtitle_path`.
6. `api/routers/cuts.py::GET /api/cuts/{id}/subtitles` + `cut_card.html` download link, with the
   same path-traversal regression test style already used for `stream_video`/`stream_thumbnail`.
7. `engine/publish/youtube.py` best-effort captions upload (§7), with a `record_stage` wrapper and
   a test asserting a captions-upload failure does not fail `publish_cut` or affect
   `cut.platform_post_id`/`published_at` (mirrors `test_publish_task.py`'s existing safety-gate
   test style).
8. Docs: `CLAUDE.md` module layout + Data model + Key conventions entries, `docs/roadmap.md`
   marks 5d done, `docs/data-model.md` (the authoritative per-column reference — append `0011` to
   its migration list and add a `subtitle_path` row to the `### cuts` table, following the exact
   precedent of its existing `black_frame_beat_indices` row), `docs/api.md` gains the new route
   (and should not repeat the existing gap where `/thumbnail`, `/thumbnail/{index}`, and
   `/hook-variant` shipped in Phase 6 without ever being added there).
9. Independent review pass (this codebase's established practice per its own session history —
   an isolated reviewer re-derives correctness rather than trusting self-report), focused
   especially on §6/§7's idempotency and failure-isolation claims and the path-traversal guard on
   the new route.

---

## Readiness verdict

**Ready for implementation.** All required design aspects (components, API surface, data model,
consistency/idempotency, failure strategy, observability, rollout) are derived directly from this
codebase's existing, well-established patterns — no novel architecture is introduced. The
remaining non-blocking gaps (§9.2 external-API contract not yet live-verified, §9.3 the finalize-
and mid-publish crash windows) are explicitly named rather than silently assumed, per this
design's own standard.

**Revision note:** an independent adversarial review of the first draft found two must-fix issues,
both corrected in this version:
1. The original offset-handling plan for the shared Whisper call would have double-applied the
   beat-start offset to `.words` (called this design's central mechanism into question) — fixed by
   keeping `.words`/`.segments` beat-relative at the source and shifting only at the SRT-building
   call site, mirroring `_whisper_timestamps()`'s existing pattern exactly (§2, §3.1, §3.2).
2. The original §7 best-effort code sample didn't compose correctly with `record_stage()`'s real
   (re-raising) semantics — as first written it would have either failed the publish job or
   silently recorded a failed upload as successful. Fixed by placing the try/except inside the
   `with record_stage(...)` block and explicitly setting `ev.ok = False` on failure (§7).
Also added: `docs/data-model.md` to the rollout plan's doc list (§10), the mid-publish-crash
caption-duplication window as an explicit inherited (not new) risk (§9.3), and confirmation that
Whisper's segment/word structure was checked against the real library source rather than assumed
(§2).
