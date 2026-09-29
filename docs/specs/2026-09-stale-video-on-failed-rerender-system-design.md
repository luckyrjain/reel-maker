# System design: "Retry publish" on a failed cut can ship a stale pre-edit video

**Status:** Implemented. **Severity:** Low per `docs/roadmap.md`'s Open Issues table.

## 1. Problem

`docs/roadmap.md`'s Open Issues table: *"'Retry publish' on a `failed` cut can ship a stale
pre-edit video — `CUT_TRANSITIONS["failed"]` intentionally allows both `→ draft` and `→ approved`
so a failed *publish* doesn't force a full re-render. The same ambiguity also covers a failed
*re-render*: if the operator edited the guide then re-rendered and that render failed,
`cut.video_path` still points to the last successfully rendered (pre-edit) file, and 'Retry
publish' on the `failed` card will ship it. Partially mitigated today by an explicit warning in
`cut_card.html`'s `failed` branch... a full fix would need tracking whether `video_path` reflects
the current guide (e.g. clearing it, or a `video_is_stale` flag) rather than just warning about
it."*

Concrete sequence:

1. A cut sits in `"in_review"` with a successfully rendered `video_path` matching the current
   `guide`.
2. The operator edits the guide — `PATCH /cuts/{id}` (`update_cut`) or a hook-variant swap
   (`choose_hook_variant`), both only reachable from `"in_review"` — and triggers a re-render.
3. The re-render **fails** (bad TTS network call, an ffmpeg crash, anything). `job_task`'s
   failure path rolls the cut back to `"failed"` via `JOB_IN_FLIGHT`. `cut.video_path` is
   untouched — it still points at the PRE-EDIT file from step 1.
4. `CUT_TRANSITIONS["failed"]` allows `→ approved` (retry publish) precisely so a failed *publish*
   doesn't force an unrelated full re-render (see `CLAUDE.md`'s Key conventions). But that same
   transition is reachable here too, and the `failed` card's "Retry publish" button (visible
   whenever `cut.video_path` is set — it always is, since step 1) uploads the stale video with no
   check that it still matches what the operator most recently edited.

This is the guide-content sibling of the already-fixed `rendered_pins_fingerprint` gap (Phase 7e,
`docs/specs/2026-09-video-pins-staleness-gate-system-design.md`) — same shape of bug (a later
mutation committed, then the render that would have caught video_path up failed before finishing),
different content (asset pins there, guide text/timing here). `docs/roadmap.md`'s own Open Issues
row for that fix explicitly scoped this one out at the time (see its closing note: *"Does not
address the separate, still-open, Low-severity 'Retry publish on a failed cut can ship a stale
pre-edit video' row below — that's the broader 'any guide edit, not just an asset re-pin'
staleness gap, explicitly out of scope for this fix."*) — this design closes that gap using the
identical mechanism.

## 2. Fix

Mirrors `rendered_pins_fingerprint`/`assert_video_matches_pins` exactly, one level up (guide
content instead of asset pins):

- **`Cut.rendered_guide_fingerprint`** (migration `0014`, nullable `String(64)`) — a sha256
  fingerprint of the `guide` dict that built the currently-stored `video_path`, snapshotted by
  `render_cut` at the same point `rendered_pins_fingerprint` is already written.
- **`engine/generation/guide_schema.py::compute_guide_fingerprint(guide: dict | None) -> str |
  None`** — `hashlib.sha256(json.dumps(guide, sort_keys=True).encode())`. `sort_keys=True`
  normalizes dict-key ordering at every level (so two structurally-identical guides built through
  different code paths fingerprint the same) but deliberately does **not** touch list ordering:
  `beats` stays in its meaningful sequence, and any genuine content change — including a
  reordered `hashtags` list — changes the fingerprint, mirroring `compute_pins_fingerprint()`'s
  own "a genuine change to any one pin changes the fingerprint" philosophy applied to guide
  content. Returns `None` for a falsy `guide` (not reachable in practice — `trigger_render()`
  already requires `cut.guide` truthy — but matches the sibling function's own defensiveness
  rather than raising).
- **`engine/publish/gate.py::assert_video_matches_guide(db, cut) -> None`** — raises `ValueError`
  (deterministic, not retried, same class as `assert_safe_to_publish`/`assert_video_matches_pins`)
  when a freshly computed fingerprint of `cut.guide` doesn't match
  `cut.rendered_guide_fingerprint`. `None` (no completed render has ever written this column —
  never rendered, or a legacy row from before this migration) is treated as "unknown, don't
  block," the identical rollout-safety decision `assert_video_matches_pins` already established:
  it lets this ship without retroactively blocking every already-rendered cut in the database, and
  self-heals on that cut's next successful re-render.
- Wired into `worker/tasks/publish.py::publish_cut` in the same branch and at the same timing as
  the two existing gate calls — never on the `if cut.platform_post_id:` finalize branch, which
  uploads nothing and would leave a live post unrecorded with no operator way out if gated.

```python
assert_safe_to_publish(db, cut.id)
assert_video_matches_pins(db, cut)
assert_video_matches_guide(db, cut)   # new
```

**Why a fingerprint and not eager-clearing `video_path`/a `video_is_stale` flag on every guide
edit** (the two alternatives the roadmap entry's own text raised): the pins-fingerprint fix already
settled this exact design question, and the reasoning transfers unchanged — eager-clearing on
every `PATCH`/hook-variant edit would destroy a perfectly good, already-approved video for an
edit the operator might revert, or one they intend to re-render successfully seconds later; a
fingerprint comparison only ever matters at the one moment it's needed (publish time), and only
ever blocks the one case that's actually broken (a re-render that failed to catch `video_path` up
to a guide edit already in place).

**Why not merge the two fingerprints into one column**: they answer genuinely different
questions — pins tracks *which assets are bound*, guide tracks *what the guide itself says*
(vo_script, on_screen_text, timing, caption, hashtags) — and a future change to either dimension
independently (e.g. re-pinning without any guide edit, which the existing pins gate already
covers) must not be masked by conflating them into a single hash. Keeping them as two columns
checked by two gate functions, called back-to-back in `publish_cut`, also means a mismatch message
can name which dimension is actually stale, rather than a generic "something changed."

## 3. Testing

- `tests/test_guide_schema.py` (new file, no prior coverage of `guide_schema.py` beyond its
  pydantic models) — 6 unit tests for `compute_guide_fingerprint()`: `None` for a falsy guide;
  deterministic for the same input; independent of dict-key insertion order at every level;
  changes when `vo_script` changes (the actual bug this exists to catch); changes when beat or
  hashtag list *order* changes (list order is meaningful, unlike dict-key order); returns a valid
  64-char hex string.
- `tests/test_publish_gate.py` — 4 new tests mirroring the pins-fingerprint gate's own suite
  exactly: matching fingerprint doesn't raise; a mismatched fingerprint raises an actionable
  message; `rendered_guide_fingerprint is None` doesn't block even with a real, non-`None` current
  guide fingerprint (the rollout-safety property, mutation-tested — see below); an end-to-end
  reproduction of the exact bug sequence (render succeeds and snapshots a fingerprint → operator
  edits the guide → the gate now catches the resulting mismatch on the eventual "Retry publish"
  attempt), the guide-level counterpart to the pins gate's own `test_staleness_bug_sequence_...`
  test.
- `tests/test_render_task.py` — 1 new call-and-assign wiring test (100%-MagicMock-based, same
  honesty caveat as the sibling pins test: proves `compute_guide_fingerprint()` is called with the
  render's real `cut.guide` dict and its return value lands on `cut.rendered_guide_fingerprint`;
  the stronger "actually reflects real guide content" property is `test_publish_gate.py`'s
  real-guide-dict tests, not this one).
- `tests/test_publish_task.py` — 3 new tests: the wiring test (a mismatched
  `rendered_guide_fingerprint` blocks publish before any credential lookup, mirroring
  `test_stale_video_pins_mismatch_blocks_publish`); the finalize-branch-exemption test using a
  REAL mismatched fingerprint (not the fixture's default `None`, which can't by itself distinguish
  correct wiring from a misplaced call in the `if cut.platform_post_id:` branch — mirrors the
  pins gate's own stronger exemption test); `_cut()`'s fixture default now explicitly sets
  `rendered_guide_fingerprint = None` (a bare `MagicMock` attribute is truthy and non-`None`,
  which would otherwise trip the new check on every pre-existing uploading-branch test — same
  fixture-hygiene note the pins fingerprint fix already documented for its own field).
- Mutation-tested: reverted `worker/tasks/publish.py`'s `assert_video_matches_guide(db, cut)` call
  and confirmed the wiring test fails (`DID NOT RAISE`), then restored; reverted
  `assert_video_matches_guide()`'s `None`-means-skip early return (making it fire on `None` too,
  mirroring the exact mutation the pins gate's own rollout-safety test is guarded against) and
  confirmed the rollout-safety test fails for the predicted reason, then restored.

Full suite: 777 tests (was 758; +19), 1 deselected (golden), `ruff check --select F,E9 .` clean.

## 4. Corrections — Round 1 (dual-lens review, before this PR was opened)

Two independent review passes (Lens A — Safety/State; Lens B — Contracts/Operations) ran against
the implemented diff before this shipped, both explicitly asked to verify empirically rather than
trust this document's claims.

**Correction 1 (both lenses, independently) — a real false-positive: any "Save changes" click on
the beat-editing form could spuriously trip the new gate, even with no actual content change.**
`ui/templates/fragments/cut_card.html`'s edit form resubmits **every** beat field
(`beat_{i}_vo_script`, `visual_direction`, `on_screen_text`, `duration_s`) on every save, whether
or not the operator touched a given field — and the pre-existing `update_cut()` unconditionally
rewrote `cut.guide` from the submitted values regardless. Two concrete drift sources, both real and
both demonstrated (not hypothetical) by the reviewing agents:

1. An HTML `<textarea>` is always re-encoded with `\r\n` line endings on form submission, touched
   or not — but `script_parser.py` (the structured-script path) originally stores multi-line
   `vo_script` values with plain `\n` (`"\n".join(...)`). An untouched multi-line `vo_script`
   textarea therefore round-tripped `\n` → `\r\n` on *every single save*, changing
   `compute_guide_fingerprint()`'s output for a beat the operator never edited.
2. `visual_direction`/other fields could carry incidental whitespace from generation (e.g. a
   trailing space an LLM produced) that the pre-existing code silently `.strip()`ped on every
   resave — again changing the stored value, and therefore the fingerprint, with zero operator
   intent behind it.

Net effect as reported by Lens A: *"edit the caption, click Save, click Approve, click Publish —
publish now fails with 'guide was edited… Re-render', even though nothing that affects the video
changed."* This would have made the fix actively worse than the bug it closes for the single most
common editing workflow (a caption-only tweak).

**Fixed** by making `update_cut()` write a beat field into `cut.guide` **only when its normalized
value actually differs** from what's already stored — `vo_script`/`on_screen_text` now normalize
`\r\n` → `\n` before comparing (matching `script_parser.py`'s own line-ending convention), and every
field comparison happens before any mutation, so a truly untouched (or edited-and-reverted) field
leaves the stored `guide` dict byte-for-byte unchanged. `cut.guide` is only reassigned at all when
at least one beat field's normalized value genuinely changed. Two new regression tests in
`tests/test_cuts_publish_router.py` reproduce the exact reported sequence — a full-form resubmit
with unchanged (but CRLF-normalized-away) values leaves `cut.guide` and its fingerprint identical,
while a genuine content edit still writes through and still changes the fingerprint — both
mutation-tested against a reverted version of the fix (confirmed the false-positive test fails for
the exact predicted reason, confirmed the genuine-edit test is unaffected, then restored).

**Correction 2 (Lens B) — no test proved the fingerprint survives a real DB round-trip.** Every
other test in this design constructs a guide dict in-memory and fingerprints it directly, or
mutates an already-loaded ORM object without ever forcing a fresh `SELECT` — none proved that
`compute_guide_fingerprint()` produces the *same* output for a guide written by `render_cut` (an
in-memory dict, just constructed) and the same guide re-read fresh by `assert_video_matches_guide`
at publish time (through the DB's own JSON column encode/decode cycle) in a later, separate
request. An unrelated-to-content round-trip difference (key reordering by the DB driver, float
precision drift, `None`-vs-missing-key normalization) silently blocking every publish would be a
significantly worse regression than the staleness bug this whole feature exists to catch. Added
`test_guide_fingerprint_survives_a_real_db_round_trip` (`tests/test_publish_gate.py`): writes a
guide containing values most likely to expose that class of drift (a non-terminating float,
unicode text, smart quotes, an emoji, an explicit `None` field), commits it, forces `db.expire_all()`
so the next read is a genuine fresh `SELECT` rather than the cached in-memory object, and confirms
the fingerprint computed before the write matches the one computed after the round-trip. (A
reviewing agent separately verified determinism against a real, disposable Postgres container —
`json`/`jsonb` column round-trip, `psycopg2`'s `json.loads` decode path — confirming no live bug
exists here; this test makes that property a permanent, repo-resident guard rather than a one-off
manual check.)

**Correction 3 (Lens B) — the pre-existing "failed" card copy became factually wrong.**
`cut_card.html`'s `"failed"`-branch hint used to say *"'Retry publish' ships the last successfully
rendered video — if you edited the guide since then and a re-render just failed, that video won't
reflect those edits"* — true before this fix (the stale video really would ship), but now
misleading: that exact scenario is the one this fix blocks. Reworded to *"...publishing is blocked
until you re-render (the video wouldn't reflect those edits)"*.

**Confirmed, not changed:** both lenses independently traced every write site for `cut.guide`
(`update_cut`, `choose_hook_variant`, and the initial `generate_guide` write) and found no other
false-positive source — the initial generation write can never cause a spurious mismatch, since
`rendered_guide_fingerprint` is still `None` at that point and the gate's rollout-safety early
return skips it; there is no render-time race on reading `cut.guide` (the state-machine guard
described in §2 is airtight under the codebase's actual row-locking behavior, verified directly
against `update_cut`'s and `choose_hook_variant`'s own guards); `compute_guide_fingerprint()` and
`assert_video_matches_guide()` remain pure reads with no commit/mutation, matching the sibling pins
functions exactly.

**Noted, not changed (deliberately out of scope):** a genuine guide edit followed by Approve with
no re-render in between is now also caught — but only at publish time, not at approve time, since
`approve_cut()` has no reason to know about a render staleness concern at all (it doesn't touch
`video_path`/`guide`, and gating it would mean duplicating this exact check on a second endpoint for
marginal earlier feedback). This matches the already-established precedent of the sibling
pins-fingerprint gate, which is equally publish-time-only, not approve-time. An operator only
discovers the block on the eventual publish attempt — acceptable for a Low-severity fix whose
entire purpose is closing a correctness hole, not redesigning the review-and-approve UX flow.

## 5. Corrections — Round 2 (deep 4-persona review on the opened PR)

Per this pipeline's own standing convention (established after PR #20's drawtext fix, now applied
by default to every PR rather than only on explicit request): four parallel personas —
Security/Red-Team, Correctness/Edge-Case, Test-Quality Auditor, Documentation-Consistency — ran
against the already-dual-lens-reviewed PR before merge.

**Correction 4 (Security/Red-Team AND Documentation-Consistency, independently) — Round 1's fix
for the false-positive in Correction 1 was itself incomplete.** Both personas, working
independently, reproduced the identical residual bug: Correction 1's fix compared the
**submitted** form value (always passed through `.strip()`/CRLF-normalization) against the
**stored** value **as-is, unnormalized**. Any beat field whose stored value already carried
incidental whitespace or line-ending quirks — realistic and unremarkable, since e.g.
`worker/tasks/generate.py` stores an LLM's `visual_direction` raw, never itself `.strip()`'d — still
registered as "changed" on every untouched save. The Security/Red-Team persona confirmed this
**fails closed**: it can only over-block a publish that should be allowed, never bypass the gate,
so it was never a security defect — but it remained a real, user-facing false positive within the
exact bug class Correction 1 already exists to close, and the Documentation-Consistency persona
independently caught it by testing the design doc's own "leaves `guide` byte-for-byte unchanged"
claim (this section, Round 1) against the actual code rather than trusting the prose. Fixed by
normalizing **both sides** identically — stored and submitted — before every beat-field comparison
(`visual_direction`, `vo_script`, `on_screen_text`), and by also handling a lone `\r` (not just
`\r\n`) as a line-ending variant. A new regression test pins a stored value with pre-existing
trailing whitespace and confirms an untouched resave leaves it exactly as-is; mutation-tested
against a version that normalizes only the submitted side (confirmed the test fails for the exact
predicted reason), then restored.

**Correction 5 (Test-Quality Auditor) — the beat/hashtag list-order test in
`tests/test_guide_schema.py` didn't actually test beat order.** The single test covering both
claims used two hashtag lists of equal length for both the "forward" and "reordered" case (and,
implicitly, never varied `beats` at all) — a hypothetical future regression that sorted every list
before hashing (not just normalizing dict-key order, which is correct) would still make the two
hashtag fingerprints differ for an unrelated reason (this was verified directly: mutating
`compute_guide_fingerprint()` to deep-sort every list still failed the hashtag-order assertion, but
for the wrong reason — sorted-and-then-compared-by-repr order happened to differ regardless of
whether order was genuinely preserved). Split into two tests: hashtag-order (unchanged) and a new
beat-order test using two *structurally distinct* beats swapped, which a deep-sort mutation now
provably fails for the right reason (both new tests fail identically under that mutation, confirmed
directly, then reverted).

**Correction 6 (Test-Quality Auditor) — no test proved `Cut.rendered_guide_fingerprint` is written
only on render success, at the correct point in `render_cut`.** The design's entire safety property
depends on this write happening *after* `composite_cut()` succeeds, at the same point
`rendered_pins_fingerprint` is written (§2) — a hypothetical future refactor that moved it earlier
(e.g. to the top of the function, snapshotting whatever `cut.guide` says *before* the render
actually runs) would silently defeat the whole gate for any cut whose render simply crashes: the
fingerprint would already reflect the *current* guide by the time of the crash, so a subsequent
"Retry publish" of the (stale, pre-crash) video would pass the gate instead of being caught by it —
the exact failure mode this whole feature exists to close, reintroduced through a different door.
Added `test_rendered_guide_fingerprint_is_not_touched_when_the_render_fails`
(`tests/test_render_task.py`): pins a sentinel value on `cut.rendered_guide_fingerprint`, forces
`composite_cut()` to raise, and confirms the sentinel survives untouched. Mutation-tested by
actually moving the fingerprint-write line to the top of `render_cut()` (the exact regression this
test exists to catch) and confirming the test fails for the predicted reason, then restored.

**Confirmed, not changed (Round 2):** the Security/Red-Team persona found no hash collision, no
unhandled exception reachable from any value `cut.guide` can legitimately hold (circular
references, deep nesting, mixed-type keys, and `bytes` all throw, but none of these are reachable
through the JSONB column or the PATCH endpoint's fixed-shape writes), no information leak in the
`ValueError` message (a fixed string, no guide content/fingerprint/path), and no new injection
surface (Jinja's autoescaping is unaffected by the normalize-then-compare logic; an HTML-payload
round-trip test confirmed escaped output and byte-identical re-storage). The Test-Quality Auditor
separately confirmed the DB-round-trip test from Correction 2 is not vacuous, by instrumenting
SQLAlchemy's own query events to prove `db.expire_all()` genuinely forces a fresh `SELECT` rather
than reusing the cached in-memory object.
