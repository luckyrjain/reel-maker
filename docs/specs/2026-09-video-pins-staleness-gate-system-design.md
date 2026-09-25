# System design: publish gate must verify the video matches the current pins — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Open Issues table — `safe_to_publish gate checks the current pins, not
the video that will ship` (Medium severity)

---

## 1. Problem and non-goals

**Problem, quoting the roadmap's own description verbatim:** *"`resolve_or_reuse()` re-pins and
commits per beat as the operator edits and re-renders; a failed render never clears
`cut.video_path`. If render N used a non-free asset (blocked) and a later render N+1 re-pins to a
safe one but then itself fails, 'Retry publish' gates against the safe N+1 pins while `video_path`
still points at render N's (unsafe) video — the gate passes and the wrong video ships, with
attribution built from the wrong pins too."*

Root cause, confirmed by reading the real code (`engine/render/asset_sourcer.py::resolve_or_reuse`,
`worker/tasks/render.py::render_cut`): `CutAsset` pins are committed **incrementally, per beat,
inside the render loop** — deliberately, so a crash mid-resolve leaves the old pin in place rather
than an uncommitted gap (`resolve_or_reuse`'s own comment explains this). `Cut.video_path` (and
`thumbnail_path`/`duration_s`) are set **only once, at the very end of `render_cut`, after
`composite_cut()` succeeds**. These two facts combined mean a render that dies anywhere after its
first beat's pin commit but before the final assignment leaves the database in a state where
`CutAsset` reflects the *new* (possibly different, possibly safer) resolution while `video_path`
still points at the *old* (possibly unsafe) file on disk. `engine/publish/gate.py::assert_safe_to_publish`
only ever reads the current `CutAsset` rows — it has no way to know they don't describe the file
`YouTubePublisher`/`InstagramPublisher` is about to upload.

**Why this is Medium, not Low:** it's a publish-time *safety* gate silently passing when its
premise (current pins == what's in the video) is false. The failure mode is exactly the one the
gate exists to prevent — an unlicensed/non-free asset shipping to a public platform — not a cosmetic
bug.

**Non-goals:**
- Preventing the underlying render failure itself. This design does not change render reliability;
  it changes what publish is willing to trust *after* a render failure has already happened.
- Retrying or auto-repairing a stale cut. The fix for a detected mismatch is "the operator
  re-renders" — this design does not add an automatic re-render trigger.
- Changing `resolve_or_reuse()`'s incremental-commit-per-beat behavior. That behavior is deliberate
  and documented (crash-mid-resolve safety) and is not the bug — the bug is that nothing downstream
  accounts for it.
- **Guide edits that never touch `visual_direction`.** `docs/roadmap.md`'s Open Issues table has a
  separate, already-tracked, Low-severity entry — *"'Retry publish' on a `failed` cut can ship a
  stale pre-edit video"* — for the case where an operator edits `vo_script`/`on_screen_text` via
  the PATCH endpoint (or a re-render fails for a reason unrelated to assets) without the guide's
  `visual_direction` changing. `CutAsset` pins are keyed off `visual_direction`'s fingerprint
  (`_fp()`), so that scenario never touches `CutAsset` at all — this design's fingerprint would
  correctly stay unchanged and would **not** catch it. That's a real, distinct gap, already
  captured by that other roadmap entry, and explicitly out of scope here: this design closes the
  *asset-pins* staleness hole, not the broader *any-part-of-the-guide* staleness hole.

---

## 2. Design choice: fingerprint comparison, not eager `video_path` clearing

The roadmap names two candidate fixes: *"clear `video_path` ... whenever a render starts or
re-pins"* or *"check a pins fingerprint."* This design picks the fingerprint check, and rejects
eager-clearing, for a concrete reason: **eager-clearing destroys a perfectly good, already-approved
video on every re-render, including one that fails for a reason that has nothing to do with asset
safety** (a transient TTS network hiccup, an unrelated ffmpeg crash). Today, an operator with a
published-quality `in_review` cut who triggers a re-render and hits an unrelated transient failure
can still fall back to the previous video. Clearing `video_path` at render start would take that
away as a side effect of fixing an unrelated safety hole — a real UX regression bundled into a bug
fix, which this codebase's own stated philosophy (`CLAUDE.md`: "a bug fix doesn't need surrounding
cleanup") argues against. A fingerprint comparison closes the exact hole described in the bug
report — *"the gate passes and the wrong video ships"* — without touching `video_path` or the
render loop's existing commit behavior at all.

---

## 3. Components

| Component | Change |
|---|---|
| `engine/render/asset_sourcer.py` | **New function** `compute_pins_fingerprint(db, cut_id) -> str \| None` — a small, pure-query helper, colocated with the existing `_fp()` (visual-direction fingerprint) and `resolve_or_reuse()` it's a natural companion to. Shared by both the writer (render) and reader (gate) call sites so there is exactly one implementation, not two that can drift (this codebase has already been burned by exactly that class of drift — see `CLAUDE.md`'s `heartbeat()`/`observability.py` precedents). |
| `api/models.py` | **New column** `Cut.rendered_pins_fingerprint` (nullable `String(64)`) — the fingerprint of the pins that built the *currently-stored* `video_path`, snapshotted at the moment a render succeeds. |
| `worker/tasks/render.py::render_cut` | **Extended.** Right alongside the existing `cut.video_path = ...` / `cut.thumbnail_path = ...` assignments at the end of a successful render, also set `cut.rendered_pins_fingerprint = compute_pins_fingerprint(db, cut.id)`. |
| `engine/publish/gate.py` | **New function** `assert_video_matches_pins(db, cut)` — compares `cut.rendered_pins_fingerprint` against a freshly-computed `compute_pins_fingerprint(db, cut.id)`; raises `ValueError` on mismatch. Separate function from `assert_safe_to_publish` (single responsibility — one checks *licensing*, the other checks *staleness*; conflating them would make a future licensing-only or staleness-only caller impossible without a flag). |
| `worker/tasks/publish.py::publish_cut` | **Extended.** Calls the new `assert_video_matches_pins(db, cut)` immediately alongside the existing `assert_safe_to_publish(db, cut.id)` call — same call site, same "before any credential lookup or upload" timing, same "only in the branch that actually uploads, never on the finalize-without-reupload branch" scoping `assert_safe_to_publish` already has. |
| `migrations/versions/0012_rendered_pins_fingerprint.py` | **New.** Additive, nullable `String(64)` column on `cuts`. |

---

## 4. API surface

Internal only — no HTTP-facing change.

```python
# engine/render/asset_sourcer.py
def compute_pins_fingerprint(db, cut_id: int) -> str | None:
    """Deterministic fingerprint of every CutAsset currently bound to this cut,
    across all beats. Returns None when the cut has zero bound assets (every
    beat black-framed, or not yet rendered at all) — same "nothing to fingerprint
    yet" semantics as this codebase's other nullable render-artifact columns.

    Order-independent per beat but NOT beat-order-independent: (beat_index,
    order_in_beat, asset_id) tuples are sorted before hashing, so the same set
    of pins always produces the same fingerprint regardless of query result
    ordering, but a genuine reshuffle of which asset plays in which beat (were
    that ever possible) would correctly change the fingerprint.
    """
```

```python
# engine/publish/gate.py
def assert_video_matches_pins(db, cut) -> None:
    """Raise ValueError (deterministic, not retried — same class as
    assert_safe_to_publish) if the CURRENT CutAsset pins for this cut no longer
    match the pins that built cut.video_path. A mismatch means a render after
    the one that produced cut.video_path changed at least one beat's pinned
    asset and then failed before finishing — see docs/roadmap.md's Open Issues
    entry and docs/specs/2026-09-video-pins-staleness-gate-system-design.md for
    the full failure sequence this closes.

    Takes the Cut object directly (not cut_id) since the caller already has it
    loaded and this avoids a redundant fetch — assert_safe_to_publish takes
    cut_id for its own historical reasons; not worth changing that signature
    just for consistency with this new function.
    """
```

Implementation sketch (not final code, illustrative for review):

```python
def assert_video_matches_pins(db, cut) -> None:
    current = compute_pins_fingerprint(db, cut.id)
    if cut.rendered_pins_fingerprint is None:
        return  # legacy row, rendered before this column existed — see §7 rollout
    if current != cut.rendered_pins_fingerprint:
        raise ValueError(
            "Cannot publish — the rendered video no longer matches the currently "
            "pinned assets (a render after this video was built changed at least "
            "one beat's asset and then failed before completing). Re-render before "
            "publishing."
        )
```

---

## 5. Data model

```python
# api/models.py, class Cut — placed after black_frame_beat_indices, following that
# column's own precedent (both are render-artifact metadata written by render_cut)
rendered_pins_fingerprint = Column(String(64))
```

`migrations/versions/0012_rendered_pins_fingerprint.py` — additive, nullable, **no backfill** (see
§7 for why this is a deliberate choice, not an oversight).

No new table. No change to `CutAsset`'s own schema — `compute_pins_fingerprint` only *reads*
`(beat_index, order_in_beat, asset_id)` off existing rows.

---

## 6. Consistency and idempotency

- **Write timing.** `cut.rendered_pins_fingerprint` is computed and assigned inside `render_cut`'s
  job body, at the exact point `video_path`/`thumbnail_path`/`duration_s` already are (end of a
  successful render, after the `db.refresh(cut); if cut.platform_post_id: raise ...` re-check that
  guards against a concurrent publish). It inherits the same atomicity every other field set at
  that point already has: the assignment only becomes durable with the job's fenced done-stamp
  commit (`worker/tasks/common.py::job_task`) — a render that fails after this point (there is
  nothing after it) can't happen; a render that fails *before* this point never executes the
  assignment at all, leaving the column at its previous value, which is exactly the desired
  "still reflects the last successfully-built video" semantics.
- **Read timing (the fix's actual mechanism).** `compute_pins_fingerprint` at render-write-time
  necessarily reads pins that are already fully committed for this render — `resolve_or_reuse()` is
  called once per beat, synchronously, inside the loop that precedes `composite_cut()`; by the time
  `composite_cut()` (and therefore the video) exists, every beat's pin commit for this render has
  already landed. There is no window where the fingerprint could be computed from a partially-pinned
  state for the render that succeeds. This relies on one existing invariant this design doesn't
  introduce but does depend on: only one `render_cut` can be in flight for a given cut at a time —
  the router transitions `cut.status` to `rendering` before enqueueing, and `CUT_TRANSITIONS` has no
  edge that lets a second render trigger while a cut is already `rendering`. If that invariant were
  ever relaxed (concurrent renders of the same cut), this fingerprint's "reflects exactly this
  render's pins" guarantee would need re-examining — out of scope here since nothing about this
  design changes that invariant either way.
- **Two independent checks, not one merged check.** `assert_safe_to_publish` (licensing) and
  `assert_video_matches_pins` (staleness) are both called from the same `else:` branch of
  `publish_cut` (the branch that actually uploads), in either order relative to each other (they're
  independent — a stale-but-safe video and a fresh-but-unsafe video are both real states this
  system needs to reject for different reasons, and reporting the specific failure reason each way
  is strictly better than one merged check that always says "cannot publish" without saying why).
- **Finalize branch is untouched, on purpose.** Neither `assert_safe_to_publish` nor the new
  `assert_video_matches_pins` runs on `publish_cut`'s `if cut.platform_post_id:` branch — that
  branch uploads nothing (see CLAUDE.md's existing note on why `assert_safe_to_publish` is scoped
  this way); gating it would block finalizing a post that is already live with no operator way out.
  The new check follows the identical, already-established reasoning — this is not a new policy,
  it's the existing one applied consistently.
- **No interaction with `CUT_TRANSITIONS`/state machine.** This is a value comparison inside an
  existing gate function, not a status change. No new `CutStatus` value, no new transition.

---

## 7. Failure strategy and rollout / backward compatibility

**The rollout question this design has to answer explicitly:** every `Cut` row that already has a
non-null `video_path` from *before* this migration ships will have `rendered_pins_fingerprint =
NULL`, while its live `CutAsset` pins will hash to some real, non-null value. A naive "reject on any
mismatch, including `None != real_hash`" implementation would immediately block publishing on
*every* existing rendered-but-unpublished cut in the database the moment this ships, regardless of
whether that specific cut was ever actually affected by the bug — a deployment-breaking regression
for a fix whose entire point is narrowing a safety gate, not widening it into a false-positive trap.

**Decision: treat `cut.rendered_pins_fingerprint is None` as "legacy, unknown, don't block."** The
`assert_video_matches_pins` sketch in §4 returns early (no raise) when the stored fingerprint is
`None`. This is a deliberate, explicitly-accepted gap, not an oversight — considered against the
alternative (a data-migration backfill computing `rendered_pins_fingerprint` for every existing
non-null-`video_path` cut from its *current* live pins) and rejected for this reason: a backfill
would be **correct** for every cut the bug never actually hit (current pins genuinely are what built
the video, since nothing has gone wrong yet — backfilling with the live fingerprint is accurate) but
would **actively paper over** the one class of cut this fix exists to catch (a cut where the bug
*has already* silently caused a mismatch) by blessing its already-wrong current state as "matching."
A backfill can't distinguish those two cases without deeper forensics this design has no signal for.
The `None`-means-skip approach makes no claim either way about pre-existing rows — it simply doesn't
retroactively apply the new protection to renders that happened before the protection existed, and
self-heals the moment any affected cut is re-rendered (the very next successful render sets a real
fingerprint, and every future publish attempt is protected from then on). This mirrors every other
nullable render-artifact column in this codebase (`black_frame_beat_indices`,
`thumbnail_candidates`, `subtitle_path` before Phase 5d shipped) — all explicitly documented as
`None` meaning "not yet computed under this feature," never backfilled. Worth stating plainly: this
is a single-operator tool (`CLAUDE.md` describes its OAuth CSRF state as "process-local... single-
operator tool"; Phase 7's Docker work was an explicit smoke test, not a live production rollout) —
the realistic blast radius of the rollout gap this section accepts is a handful of locally-rendered
dev cuts, not a fleet of already-published content sitting exposed. The reasoning above would still
hold at larger scale, but the practical cost of accepting it here is close to zero, not just
theoretically bounded.

**Failure mode when the check does trigger:** `ValueError`, same deterministic (non-retried) failure
class as `assert_safe_to_publish` — `publish_cut` is `max_retries=0` and this is not a transient
condition retrying would fix, so no `should_retry()` classification change is needed. The job fails,
`cut.status` rolls back via the existing `JOB_IN_FLIGHT` owner-rollback machinery (unchanged), and
the operator sees a failed publish with a message telling them to re-render — actionable, not a dead
end.

---

## 8. Observability

No new `StageEvent` — this is a synchronous, in-request-path validation (like `assert_safe_to_publish`
itself, which also isn't separately instrumented) rather than a slow/networked call. The `ValueError`
message itself is the operator-facing signal, surfaced the same way every other `publish_cut`
failure already is (`job.error`, the failed-cut card).

---

## 9. Capacity

Negligible. `compute_pins_fingerprint` is one query filtered by `cut_id` — not a standalone index,
but index-assisted via `CutAsset`'s `UniqueConstraint("cut_id", "beat_index", "order_in_beat")`,
whose leading column is `cut_id` (`CutAsset` is already queried by `cut_id` this same way elsewhere
in this exact code path — `unsafe_assets()` does the same shape of query) plus a
cheap string hash over a beat count that's always small (a <90s reel has at most a handful of
beats). Called twice per publish attempt (write once at render success, read once at publish gate)
— no measurable latency impact on either `render_cut`'s already-multi-second runtime or
`publish_cut`'s pre-upload checks.

---

## 10. Rollout plan

Single PR, no phased flag — additive column, and the `None`-skips-check rollout decision (§7) means
this cannot regress any existing cut's publishability on deploy.

1. Migration `0012_rendered_pins_fingerprint.py` (`Cut.rendered_pins_fingerprint`), verified against
   real Postgres (`upgrade head` / `downgrade -1` / `upgrade head`), per this codebase's established
   practice.
2. `engine/render/asset_sourcer.py::compute_pins_fingerprint()` + tests (new cases in
   `tests/test_asset_sourcer.py`, which already covers `resolve_or_reuse`'s pin/reuse/re-pin
   behavior and is the natural home): deterministic for a given pin set, order-independent w.r.t.
   query result ordering, `None` for zero pins, changes when a beat's pin changes, unaffected by an
   untouched beat's pin staying the same.
3. `worker/tasks/render.py::render_cut` writes `rendered_pins_fingerprint` —
   `tests/test_render_task.py` is 100%-`MagicMock`-based today (`resolve_or_reuse` itself is
   patched, no real `CutAsset` row ever exists in that file), so the honest claim a test there can
   make is call-and-assign wiring only: patch `compute_pins_fingerprint`, assert it's called with
   the render's `cut.id` and its return value lands on `cut.rendered_pins_fingerprint`. The stronger
   claim — that the fingerprint actually reflects the real pins a real render bound — is proven by
   the real-DB end-to-end test in step 6 below, not here; don't duplicate that property with a
   second, weaker-but-differently-worded assertion in this file.
4. `engine/publish/gate.py::assert_video_matches_pins()` + `tests/test_publish_gate.py` (existing
   file, exact same fixture style as its current three tests): passes when fingerprints match, blocks
   with a clear `ValueError` message on mismatch, does NOT block when `rendered_pins_fingerprint`
   is `None` (the explicit legacy-row regression test — this is the one most worth mutation-testing,
   since it's the rollout-safety property this whole design hinges on).
5. `worker/tasks/publish.py::publish_cut` wires the new call in alongside `assert_safe_to_publish`.
   **This step has a real, non-optional prerequisite the naive version of this plan misses**:
   `tests/test_publish_task.py`'s `_cut()` helper returns a bare `MagicMock()` with no
   `rendered_pins_fingerprint` set — on an unconfigured `MagicMock` that attribute is a truthy
   `MagicMock` instance, not `None`, so the moment `assert_video_matches_pins` is wired in, every
   *existing* test that exercises the uploading branch (there are several — the deterministic-
   failure, successful-publish, transient-error, post-id-committed-early, missing-credential, and
   unsafe-asset tests all go through it) would start failing from the *new* check firing on a
   `None`-vs-`MagicMock` mismatch, not from whatever each test actually means to exercise. Fix:
   `_cut()`'s default must explicitly set `cut.rendered_pins_fingerprint = None` (a legacy/no-op
   row under §7's skip rule), so every pre-existing test keeps passing unchanged; only the new
   mismatch-specific test(s) added in this step override that default to a value that provokes a
   real mismatch, which in turn requires its own distinct mock for whatever `compute_pins_fingerprint`
   reads (a separate stub from `unsafe_assets`'s existing `db.query(...).join(...).filter(...).all()`
   chain). Extend the file with: a mismatch blocks publish (publisher never reached, same assertion
   style already used for the existing safety gate), and a mutation-test pass confirming the new
   call is genuinely absent from the finalize branch, not just present-and-vacuously-passing there.
6. **The single most important test in this rollout**: an end-to-end reproduction of the exact
   sequence from the bug report — render a cut (safe pins), re-render with a beat's `visual_direction`
   changed so it re-pins, simulate that second render failing *after* the re-pin commits but *before*
   `video_path` updates (e.g. by calling `resolve_or_reuse()` directly to mutate pins, then asserting
   `assert_video_matches_pins` now raises for the still-old `video_path`). This is the regression
   test that proves the fix, not just that the new functions individually behave as documented in
   isolation.
7. Docs: `CLAUDE.md` (module layout for `asset_sourcer.py`/`gate.py`/`render.py`, Data model entry
   for the new column, a Key conventions entry explaining the fingerprint-vs-eager-clearing choice
   from §2 and the `None`-means-legacy rollout decision from §7 — both are exactly the kind of
   "non-obvious, would silently regress if changed carelessly" rule this file exists to preserve),
   `docs/roadmap.md` (mark the Open Issues entry resolved, matching the level of detail every other
   resolved item there has), `docs/data-model.md` (migration list + `cuts` table row).
8. Independent review pass, focused specifically on: (a) the rollout/backward-compat reasoning in
   §7 — is `None`-means-skip actually safe, or does it leave a real gap someone should push back on;
   (b) whether `compute_pins_fingerprint`'s sort key genuinely makes it order-independent in the way
   claimed; (c) whether the new check's placement in `publish_cut` really is scoped identically to
   `assert_safe_to_publish` (same branch, same timing) and not accidentally reachable from the
   finalize path.

---

## Readiness verdict

**Ready for implementation.** The design is a small, additive, single-PR fix that directly closes
the exact failure sequence the roadmap's bug report describes, chooses and justifies one of the two
candidate approaches the roadmap itself named (§2), and explicitly reasons through the one real
rollout hazard a naive implementation would hit (§7) rather than leaving it as an implicit
assumption. No open questions are carried forward — unlike the SRT caption export design, this
feature has no external API surface and therefore no live-verification gap of that kind.

**Revision note:** an independent adversarial review verified the core mechanism, data-model
claims, call-site scoping, and §7's rollout reasoning against the real code and found them all
accurate — the one real gap was in §10's rollout plan, not the design itself: step 5's naive wording
would have broken nearly every existing test in `tests/test_publish_task.py` (its `_cut()` fixture
defaults to a `MagicMock` for `rendered_pins_fingerprint`, which is truthy and non-`None`, so every
test exercising the uploading branch would fail from the *new* check rather than what each test
actually means to exercise) — fixed by requiring `_cut()`'s default to explicitly set
`rendered_pins_fingerprint = None`. Step 3 was also corrected to state honestly what
`test_render_task.py`'s fully-mocked style can and can't prove (call-and-assign wiring only; the
"reflects real pins" property is the end-to-end test in step 6's job, not step 3's). Also added: an
explicit note on the single-render-at-a-time invariant this design relies on (§6), a cross-reference
to the separate, already-tracked, out-of-scope "guide edited without a visual_direction change"
staleness gap (§1), and a sharper single-operator-tool framing of §7's rollout blast radius.
