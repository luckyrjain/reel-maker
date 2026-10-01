# Module design — guide_edit.py (operator-driven guide edits)

## 1. Problem

`api/routers/cuts.py::update_cut()` (the PATCH beat-edit endpoint) and
`choose_hook_variant()` (the hook-variant swap) each carried real dict-diff/normalize
logic inline inside an HTTP handler: CRLF/strip normalization applied to both the
submitted and the already-stored value before comparing (a false-positive bug class
that took two separate review rounds to close correctly — see CLAUDE.md's Key
conventions entry on `Cut.rendered_guide_fingerprint`), on-screen-text dedup/truncation
to 5, and a `guide_changed` flag gating whether `cut.guide` gets reassigned at all.
Despite that documented bug history, none of it had a test surface of its own — every
test exercising this logic (`tests/test_cuts_publish_router.py`,
`tests/test_variants_router.py`) went through a full FastAPI `TestClient`, a real DB
session, row locking, and a template render, just to prove what is fundamentally a pure
dict-in/dict-out transform behaves correctly. `choose_hook_variant()` separately
duplicated, verbatim, the copy-guide→copy-beats-list→mutate-one-beat→reassign dance
`update_cut()` also needed (required because SQLAlchemy's JSON column type only detects
attribute reassignment, not in-place mutation of a value it already holds).

A separate, smaller duplication was folded into the same pass: `derive_on_screen` (the
VO→on-screen-text word-wrap algorithm) was implemented twice —
`engine/generation/script_parser.py::derive_on_screen()` and
`engine/generation/postprocess.py::_derive_on_screen()` — with one caller
(`worker/tasks/generate.py`) already needing an alias-import to use both side by side.

## 2. Design

New module `engine/generation/guide_edit.py`:

- `set_beat_field(beats: list[dict], index: int, field: str, new_value) -> bool` —
  writes one beat field (`duration_s`, `visual_direction`, `vo_script`,
  `on_screen_text`) only when its normalized value genuinely differs from what's
  stored, normalizing both sides identically. Returns whether anything changed, so
  `update_cut()` can OR several calls together into one `guide_changed` flag without
  re-deriving the comparison rule at each call site. `field == "vo_script"` also
  re-derives `on_screen_text` as a side effect inside the same call — matching the
  pre-existing behavior exactly (on_screen_text is never independently re-derived
  except as a consequence of a real vo_script edit).
- `replace_beat_vo(guide: dict, beat_index: int, new_vo: str) -> dict` — the
  copy→mutate→reassign dance for the hook-swap path, returning a new guide dict rather
  than mutating the input, so the caller's `cut.guide = replace_beat_vo(...)` reassigns
  the SQLAlchemy JSON column attribute correctly.

Neither function knows about HTTP or FastAPI. `choose_hook_variant()`'s "beat 0 must
exist and be type=='hook'" check stays in the router — that's an HTTP-level business
rule ("reject this request with a 422"), not a guide-editing mechanic, and keeping it
out of `guide_edit.py` means that module could be called from something other than a
FastAPI route later without dragging HTTP semantics along.

`derive_on_screen` unification: `script_parser.derive_on_screen()` is now the single
implementation (it already strips label prefixes via `_clean_vo()`, a no-op on real
operator-submitted text, which is never LLM-raw). `postprocess.py` drops its own
`_derive_on_screen`/`_MAX_LINE_CHARS` and imports the one in `script_parser.py`
instead; `worker/tasks/generate.py` drops its `as _postprocess_derive_on_screen` alias
entirely, since there's exactly one name to import. No import cycle — confirmed
`script_parser.py` imports nothing from `postprocess.py` and vice versa, before this
change was made.

## 3. Process notes

Produced via the `improve-codebase-architecture`/`grilling` skill pair: an HTML report
surfaced 3 candidates (this one, the `derive_on_screen` duplication folded in here, and
a `resolve_beat_assets()` HF-asset gate-block duplication left for a future pass), the
user picked this one, and a 2-round grilling session settled: module location, the
per-field `set_beat_field()` shape (vs. a whole-guide-diff function), vo_script's
on_screen_text side effect (one call does both, not two), `duration_s` going through
the same function as the other three fields (uniform interface, no special-casing),
`replace_beat_vo()` NOT owning the hook-type validation (stays in the router), and the
test-migration strategy (keep the existing HTTP-level tests as wiring coverage, add new
direct unit tests rather than narrowing the existing ones — this module's own bug
history is the reason to keep both, not replace one with the other).

## 4. Verification

New `tests/test_guide_edit.py` (14 tests): `set_beat_field()` for all 4 fields (writes
through a genuine change; no-op when the normalized values match; both review-round
regressions — submitted-side-only normalization, and the stored-side-also-needs-it
follow-up — covered directly for `visual_direction` and `vo_script`); `replace_beat_vo()`
(swaps + re-derives on_screen_text; returns a new object rather than mutating the
input). All pre-existing tests in `tests/test_cuts_publish_router.py`,
`tests/test_variants_router.py`, `tests/test_script_parser.py`,
`tests/test_audio_text_sync.py`, and `tests/test_generate_task.py` pass completely
unmodified. Full suite: 811 passed / 3 skipped / 1 deselected (797 + 14 new),
`ruff check --select F,E9 .` clean.

Mutation-tested 3 of the new tests against the exact regressions they guard: (1)
reverted stored-side normalization for `visual_direction` — the stored-side regression
test failed with `assert True is False`; (2) removed the `on_screen_text`
re-derivation from the `vo_script` branch — the rederive test failed on the mismatched
`on_screen_text` value; (3) made `replace_beat_vo()` mutate its input in place instead
of returning a new object — the no-mutation test failed, proving the original guide's
`vo_script` had changed. All three restored and the full `test_guide_edit.py` suite
re-verified green after each.
