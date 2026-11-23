# Design — `engine/render/render_output.py`: one owner for "what a render produced"

Status: implemented (spec r4 after spec-review rounds 1-3 and a clean round 4; implementation record in §10) · Phase 8h · Candidate C3 of the 2026-11-03 full-codebase architecture
review ("Worth exploring", medium confidence) · Base `main` = `24cbc91`; every line number below refers to that tree ·
Branch `c3-render-output` · Builds on the Phase 7e (pins) and Phase 7m (guide) staleness gates.

## 1. Problem

Eight `Cut` columns share one lifecycle: they are written together by one successful render, replaced wholesale on the
next one, and the last two are compared against fresh values at publish time.

| Column | Meaning |
|---|---|
| `video_path`, `duration_s` | the MP4 and its length |
| `thumbnail_path`, `thumbnail_candidates` | the sampled frames; `thumbnail_path` is `candidates[0]` until the operator picks |
| `black_frame_beat_indices` | beats that rendered as a black frame, `None` when none did |
| `subtitle_path` | the `.srt`, `None` when there was nothing to caption |
| `rendered_pins_fingerprint`, `rendered_guide_fingerprint` | snapshots of what built the video |

The rule "these eight move together, and each fingerprint is computed the same way it is checked later" is held by the
35 lines `worker/tasks/render.py:154-188` (27 of them comments) and CLAUDE.md. Nothing in code enforces it:

- The writer is eight hand-ordered assignments at the end of the 155-line `render_cut`, which also resolves assets, paces
  TTS and composites. A ninth column added to the lifecycle compiles and passes the tests if the author forgets it.
- Today's tests do not pin the writer well: after a successful render nothing asserts `video_path`, `thumbnail_path`,
  `thumbnail_candidates` or `duration_s` (`tests/test_render_task.py` asserts only `subtitle_path`, the two fingerprints,
  `black_frame_beat_indices`, and a negative `video_path` check at :137). Deleting `cut.duration_s = duration` breaks no
  test.
- "How a fingerprint is computed" lives in two packages (`engine/render/asset_sourcer.py` for pins,
  `engine/generation/guide_schema.py` for the guide) and is called from two more (`render.py` writes, `gate.py` reads).
  The `EMPTY_PINS_FINGERPRINT` sentinel exists only because the write site and the read site once disagreed (Phase 7e
  review); "both use `compute_pins_fingerprint_for_render`, never the raw function" is a docstring.
- The two `assert_video_matches_*` gate functions repeat one shape: "`None` means never rendered, skip; else compare;
  else raise".

Deletion test: removing the comment block breaks no test; the invariant is implicit.

## 2. Scope

In scope: one new module owning the eight columns' write, the fingerprint computations and the staleness check; the call
sites moved onto it; guards that make a forgotten or stray column fail loudly; docs.

Out of scope (decided, §3): splitting `render_cut` (asset resolution, TTS pacing, timecode back-fill, compositing);
routing the readers (`cut_card.html`, `cut_media.py`, `youtube.py`, `choose_thumbnail`) through the module; any schema,
migration, endpoint, route or template change; changing the rollout-safety rule ("`None` = never rendered, do not
block") or the `EMPTY_PINS_FINGERPRINT` sentinel.

## 3. Decisions (grilling round 1, all recommended answers accepted)

| # | Decision |
|---|---|
| Q1 | The module covers writer + staleness check only. Readers keep reading `cut.<column>`. |
| Q2 | New `engine/render/render_output.py`; a frozen `RenderOutput` value, `record(db, cut, output)`, staleness checks. |
| Q3 | The four fingerprint names (three from `asset_sourcer`, one from `guide_schema`) move into it: pure move, no bridge, importers retargeted. |
| Q4 | Option (a): `gate.py` keeps `assert_video_matches_pins` / `assert_video_matches_guide` as thin delegates; their logic and messages move. |
| Q5 | `render_cut` is not decomposed. `_audio_duration` -> `audio_duration` is a separate one-line-rename commit (the one private name `render.py` imports from `tts.py`; housekeeping on the same file, not part of the invariant). |
| Q6 | Guards force every `Cut` column to be classified and keep stray writers out. |
| Q7 | Lighter proof than C1/C2: characterization tests first, mechanical retargets, mutation battery, four-persona reviews. The section order below follows the sibling specs except that §6 carries the test and mutation plan. |

Two refinements made while reading the code, both consequences of the decisions above:

- `RenderOutput` holds **five** fields, not six: `thumbnail_path` is `thumbnail_candidates[0]` by definition (the
  operator pick in `cuts.py::choose_thumbnail` is a later partial overwrite). Deriving it in `record` makes that
  relation structural.
- There is no combined `assert_video_current`. With Q4(a) the two gate functions are the public surface and
  `publish_cut` calls them in sequence; a combined function would have no caller (deletion test). The module exposes
  `assert_pins_not_stale` and `assert_guide_not_stale`, which the gate functions delegate to.

## 4. Design

### 4.1 Public surface of `engine/render/render_output.py`

```python
EMPTY_PINS_FINGERPRINT = "no-pins-bound"          # moved from asset_sourcer, value unchanged

def compute_pins_fingerprint(db, cut_id) -> str | None            # moved from asset_sourcer, body unchanged
def compute_pins_fingerprint_for_render(db, cut_id) -> str        # moved from asset_sourcer, body unchanged
def compute_guide_fingerprint(guide: dict | None) -> str | None   # moved from guide_schema, body unchanged

RENDER_ARTIFACT_COLUMNS: tuple[str, ...]   # the eight Cut column names (a set in all but spelling; order not significant)

@dataclass(frozen=True)
class RenderOutput:
    video_path: Path
    thumbnail_candidates: Sequence[Path]    # must be non-empty; [0] becomes thumbnail_path
    duration_s: float
    black_frame_beat_indices: Sequence[int] # empty means "every beat resolved real media"
    subtitle_path: Path | None

def record(db, cut, output: RenderOutput) -> None
def assert_pins_not_stale(db, cut) -> None      # ValueError on mismatch
def assert_guide_not_stale(db, cut) -> None     # ValueError on mismatch
```

Everything else is module-private (`_assert_not_stale`). The `compute_*` names keep their public spelling because over
100 test references and the rest of the codebase use them; renaming is not part of a move. After the move
`asset_sourcer.py` still uses `hashlib` (`_fp`) and `models`; `guide_schema.py` loses its only users of `import hashlib`
and `import json` (lines 1-2), removed in the same commit (ruff F401 would otherwise fail CI). The moved docstrings and
comments are edited in the move commit so their cross-references name the new home (they name `gate.py`,
`asset_sourcer` and `guide_schema` today); function bodies and constant values are unchanged.

### 4.2 `record` semantics

1. Compute into locals, touching nothing on `cut`: `pins_fp = compute_pins_fingerprint_for_render(db, cut.id)`,
   `guide_fp = compute_guide_fingerprint(cut.guide)`, `thumbnail_path = str(candidates[0])`,
   `candidates_json = [str(p) for p in candidates]`, `video = str(output.video_path)`,
   `black = list(output.black_frame_beat_indices) or None`, `subtitle = str(p) if p else None`.
2. Then assign the eight columns in one block from those locals.

Consequences, all deliberate:

- **Atomic in memory.** If either fingerprint computation or `candidates[0]` raises, no column has been touched. Before,
  up to seven columns were already assigned when a later line raised (one for a `candidates[0]` failure, six for a
  pins-fingerprint failure, seven for a guide-fingerprint failure). (The job fails and the session rolls back either way —
  `worker/tasks/common.py:549-550` — so the database state is identical; this removes an in-memory half-written `Cut`.)
- **"Fingerprints last" becomes "fingerprints derived from the same moment".** The old rule existed so the snapshot was
  taken after every beat's `resolve_or_reuse()` commit. That is a property of *where `record` is called* (after
  `composite_cut` returns and after the "posted while it rendered" refresh), not of assignment order, and `SessionLocal`
  has `autoflush=False` (`api/db.py:10`), so the pins read cannot see pending assignments either way. The call-site rule
  is documented on `record`.
- `record` does **not** commit, flush, refresh or expire, and does not check `platform_post_id`: the race check stays in
  `render_cut`, immediately before the call. `record` is the persistence tail only. `transition(cut, "in_review", ...)`
  also stays in `render_cut`.
- An empty `thumbnail_candidates` raises `IndexError` (same type as before; not newly validated at construction).
- The adapter in `render_cut` builds `RenderOutput(video_path=out_path, thumbnail_candidates=thumbnail_candidates,
  duration_s=duration, black_frame_beat_indices=black_frame_beats, subtitle_path=subtitle_path)`; `thumb_path` (the
  base path `composite_cut` was handed) is not passed — `candidates[0]` is what is stored, as before.

### 4.3 Staleness checks

`_assert_not_stale(stored, compute_current, message)`: `stored is None` -> return **without calling `compute_current`**
(rollout safety, unchanged; no DB query on the legacy path); `compute_current() != stored` -> `ValueError(message)`.
`assert_pins_not_stale` and `assert_guide_not_stale` are one-line callers with the two existing message texts, byte for
byte, resolving `compute_pins_fingerprint_for_render` / `compute_guide_fingerprint` through the module global at call
time so a test patching `engine.render.render_output.compute_*` is seen.

`gate.py` keeps `assert_video_matches_pins(db, cut)` and `assert_video_matches_guide(db, cut)` with their signatures,
each a one-line delegate written as `render_output.assert_pins_not_stale(db, cut)` (module-attribute call, not a
from-import, so a patch of the render_output function is seen — the C2 "D3" rule). Their long docstrings (gate.py:48-79
and :92-115) move with the logic; the delegates keep a short pointer. `assert_safe_to_publish` / `unsafe_assets` are
untouched.

### 4.4 Dependency direction

`render_output` imports `api.models` (for `CutAsset`) and the standard library only; nothing from `engine.generation`,
`engine.publish`, `asset_sourcer` or `worker`. `gate.py` and `worker/tasks/render.py` import it; `asset_sourcer.py` and
`guide_schema.py` stop defining the moved names and do not import it. No cycle.

### 4.5 Guards (Q6) — all in `tests/test_render_output.py`, each with synthetic positive and negative controls

- **G-A column ledger.** `{c.name for c in models.Cut.__table__.columns}` equals `set(RENDER_ARTIFACT_COLUMNS) |
  NOT_RENDER_OUTPUT`, with the two sets disjoint. `NOT_RENDER_OUTPUT` is a complete literal: `id, reel_id, platform,
  target_length_s, guide, caption, hashtags, hook_variants, status, published_at, platform_post_id, views, likes,
  comments, metrics_updated_at`. A new `Cut` column fails until its author classifies it (§7 notes it can still be
  misclassified).
- **G-B exact write set.** `record` on a real, flushed-but-never-refreshed `Cut` whose eight artifact columns are
  pre-seeded with sentinels that differ from every input — **including non-`None` sentinels for both fingerprint
  columns** — with non-empty black-frame indices, a non-`None` subtitle and a non-empty guide, and with the other 15 columns seeded to values that differ from anything a mutant could write (history reports "no change" for an equal value): attribute history shows
  every `RENDER_ARTIFACT_COLUMNS` member changed and no other column changed. (SQLAlchemy history reports "no change"
  when the loaded value equals the assigned one, so a refreshed `Cut` or an empty/`None` input would hide a dropped
  assignment.)
- **G-C no stray writer.** An AST walk of production code under `api/`, `engine/`, `worker/` (not tests), with these
  exact rules:
  - *Attribute stores.* Any store whose target is an `ast.Attribute` with `attr` in the eight names (`Assign`,
    `AugAssign`, `AnnAssign` with a value, tuple/list/starred targets, `for ... in` and `with ... as` targets, `del`)
    is a write. **Bare `ast.Name` targets are never writes** (`duration, thumbnail_candidates, subtitle_path =
    composite_cut(...)` in render.py, the locals in `compositor.py` and `youtube.py` stay legal).
  - *Subscript stores.* A store whose key is a string literal naming one of the seven names other than `duration_s`
    (`d["video_path"] = ...`) is a write (the tree has none). For `duration_s` a subscript store is a write only when the
    receiver is `__dict__` or a `vars(...)` call: `duration_s` is a plain dict key in unrelated code
    (`worker/tasks/render.py:111` `d["duration_s"] = ...` on a beat dict, `render.py:144` `ev.detail["duration_s"] = ...`
    on a `StageEvent`, `engine/generation/guide_edit.py:56` `beat["duration_s"] = ...` on a guide dict), and those must
    not fire.
  - *Calls.* `setattr(x, "<literal name>", ...)` is a write. Keyword arguments count **only** when the callee is `Cut(...)`
    / `models.Cut(...)`, or a `.values(...)` / `.update(...)` / `insert(...)` call. A dict literal counts only when it is
    an argument of `.update(...)` / `.values(...)` and has a string key naming one of the eight, or anywhere when a key is
    `<x>.Cut.<name>`. Keyword arguments of any other callee (`composite_cut(thumbnail_path=...)`,
    `RenderOutput(video_path=...)`, `SourcedAsset(duration_s=...)`, `Beat(duration_s=...)`) and other dict literals
    (`"duration_s": s.duration_s` in `generate.py:244`) are not writes.
  - *Allowlists, fail-closed.* Inside `render_output.py` only `record`'s body is exempt; a separate assertion requires that
    body to contain exactly eight attribute stores and that no other function in the module (`_assert_not_stale`,
    `assert_pins_not_stale`, `assert_guide_not_stale`, the `compute_*` functions) contains an attribute store of the
    eight names. `thumbnail_path` is also allowed inside `api/routers/cuts.py::choose_thumbnail`
    (an `async def`, so the allowlist handles `AsyncFunctionDef`); `duration_s` is allowed only as the attribute stores
    `stub.duration_s` in `beat_enrichment.py::_enrich_with_insight` and `beat.duration_s` in
    `generate.py::_enrich_standard_path_guide` (`Beat`/`BeatStub` fields), keyed by (file, enclosing function) like
    `choose_thumbnail`. Any other write fails.
  - *Non-vacuity.* On the real tree the walk must find exactly these allowed writes outside `render_output.py`:
    `cuts.py:294` (`thumbnail_path`), `beat_enrichment.py:117` and `generate.py:352` (`duration_s`) — three attribute
    stores, and no attribute store in `render.py` after the move. (`render.py:111,144` and `guide_edit.py:56` are
    `duration_s` dict-key subscript stores and are not writes.) Each rule above has a synthetic snippet that must fire,
    and each exclusion (`composite_cut(thumbnail_path=...)`, a bare-name store, `SourcedAsset(duration_s=...)`,
    `d["duration_s"] = x`, `ev.detail["duration_s"] = x`) a snippet that must not.
- **G-D module seam.** `render_output` imports none of `engine.generation`, `engine.publish`,
  `engine.render.asset_sourcer`, `worker` (reusing `tests/_seam_ast.py::g5_import_problems`); `hasattr` is false for the
  four moved names on `asset_sourcer` and `guide_schema`.

## 5. Behavior preservation

Everything is a pure move except:

1. In-memory atomicity of the eight assignments (§4.2). No database-observable change.
2. Module homes: `asset_sourcer.{compute_pins_fingerprint, compute_pins_fingerprint_for_render, EMPTY_PINS_FINGERPRINT}`
   and `guide_schema.compute_guide_fingerprint` no longer exist; importers use `engine.render.render_output`. No bridge.
3. Retargeted tests (§6.2). `tests/_seam_oracle.py::STAYS` loses the three `asset_sourcer` names (and its comment at
   line 83, "every non-moved baseline top-level definition of asset_sourcer", is amended) so
   `test_module_seams.py::test_G5_every_moved_name_...` (`hasattr(AS, stays)`) keeps passing. The one-time C2 fidelity
   script (`docs/specs/2026-11-safe-fetch-media-policy-fidelity.py`) is a pinned proof against the C2 baseline, not a CI
   guard or a test; after C3 its ORACLE check (it derives "baseline top-level defs not in STAYS" and compares that to
   MOVED) and its B and C checks (which compare `asset_sourcer.py` and the test files to the inverse-transformed
   baseline) **fail**. It is left as is and the Build-phase entry says so.
4. `tts._audio_duration` becomes `tts.audio_duration` (own commit, §10): the import at `render.py:11` and the call at
   `render.py:109`, `tts.py:130,225`, and four `patch("engine.render.tts._audio_duration")` targets
   (`tests/test_tts.py:101,109,117,124`).
5. Logger names, error messages, exception types, SQL statements and the order of the `StageEvent`/commit sequence are
   unchanged.

## 6. Tests

### 6.1 Proof of a mechanical move

Baseline: `pytest --collect-only -q` at `24cbc91` = 3075 selected (3072 passed + 3 skipped; the golden test is
deselected), captured by a throwaway script before any change. Writer behavior is pinned **before** the move (commit 1,
below): the wiring test is green on `24cbc91`'s writer and stays green unchanged after `record` replaces it, so a dropped
column assignment in the move commit fails a test written against the old code. The move commit then contains the
production move and mechanical retargets and **no new tests**:

- the sorted node-id set after the move equals the baseline set plus exactly commit 1's new test;
- each of the seven retargeted test files is compared under this **normalized** transform (a throwaway script, output
  recorded in §10) to its version at the start of the move commit — `24cbc91` for six of them, and commit 1's version
  for `tests/test_render_task.py`, which commit 1 extends: parse both; strip docstrings and ignore comments; flatten
  every `ImportFrom` into the multiset of `(module, name, asname)` per scope (removed from the body, compared as a
  multiset) after remapping the module of exactly the four moved names; rewrite exactly the six patch-target `Constant`
  strings of §6.2 (the script asserts the rewritten count is six); then require `ast.dump` equality. Negative controls,
  each of which must fail the check: one extra edit (a changed assertion operand), a moved-name import pointed at the
  wrong module, and a patch string with the wrong prefix. `tests/_seam_oracle.py` is a data edit and is checked
  separately (`STAYS` minus exactly the three names);
- `pytest` -> 3072 passed + 3 skipped plus the commit-1 test, `ruff check --select F,E9 .` clean.

### 6.2 Retargets (generated from `git grep -nE "compute_(pins|guide)_fingerprint|EMPTY_PINS_FINGERPRINT" -- tests`)

| File | Edit |
|---|---|
| `tests/test_asset_sourcer.py`, `tests/test_sourcer_pins_and_music.py` | split the `asset_sourcer` import block: the three pins names -> `render_output`; `SourcedAsset`, `_fp`, `resolve_or_reuse`, `LocalMusicSource`, ... stay; stale comment/docstring pointers (e.g. test_asset_sourcer.py:260-261) updated |
| `tests/test_publish_gate.py` | `EMPTY_PINS_FINGERPRINT`, `compute_pins_fingerprint`, `compute_pins_fingerprint_for_render` (lines 12-18) and `compute_guide_fingerprint` (line 5) -> `render_output` |
| `tests/test_guide_schema.py`, `tests/test_cuts_publish_router.py` (3 function-local imports) | `compute_guide_fingerprint` -> `render_output` |
| `tests/test_render_task.py` | patches at :262, :296 `worker.tasks.render.compute_*` -> `engine.render.render_output.compute_*` |
| `tests/test_publish_task.py` | patches at :135, :164, :338, :365 `engine.publish.gate.compute_*` -> `engine.render.render_output.compute_*` |
| `tests/_seam_oracle.py` | `STAYS` minus the three names; comment amended |

The six patch-target strings, exactly: `tests/test_render_task.py:262` `worker.tasks.render.compute_pins_fingerprint_for_render`, `:296` `worker.tasks.render.compute_guide_fingerprint`, and `tests/test_publish_task.py:135,338` `engine.publish.gate.compute_pins_fingerprint_for_render`, `:164,365` `engine.publish.gate.compute_guide_fingerprint`; each becomes `engine.render.render_output.<same name>`. Other stale test text (docstrings/comments, invisible to the normalized comparison) is edited in the same commit: `tests/test_guide_schema.py:1-5`, `tests/test_render_task.py:233-241,273`, `tests/test_publish_task.py:121`, `tests/test_cuts_publish_router.py:451`, `tests/test_sourcer_pins_and_music.py:1-2,10`, `tests/test_asset_sourcer.py:260-261`.

### 6.3 New tests

**Commit 1 (before the move, green on the old writer): wiring test in `tests/test_render_task.py`.** `render_cut` with a
mocked `composite_cut` returning **two distinct candidates**, a non-empty black-frame list and a subtitle path and a non-integral
`duration` of 17.25 (not the guide's beat sum of 18.0, so `sum(beats)`, `int()` and `round()` mutants differ). The test reads `out_path` from `composite_cut.call_args.kwargs["output_path"]` and the thumbnail base path from
`call_args.kwargs["thumbnail_path"]` (they are distinct), then asserts `cut.video_path == str(out_path)`,
`cut.duration_s == 17.25`, `cut.thumbnail_path == str(c0)`, `cut.thumbnail_candidates == [str(c0), str(c1)]` and the
already-pinned columns. `cut` is the file's `MagicMock`, which stores assigned values; stray extra writes are G-B's job.

**Commit 3 (after the move): `tests/test_render_output.py`.** `record` on a real `Cut` (`db_session`; `cut.id !=
cut.reel_id` via a decoy reel/cut so an id mix-up is visible); `RenderOutput` inputs use tuples and a non-integral `duration_s` (17.25):

- all eight columns written; `str` paths, `type(...) is list` for the JSON columns **before** flush; a flush + commit +
  `expire_all` + reload round trip (a leaked `Path` or tuple fails only at flush);
- `thumbnail_path == candidates[0]` with at least two distinct candidates; `thumbnail_candidates` keeps order;
- empty black-frame input -> `None`; `None` subtitle -> `None`;
- a second `record` replaces every column wholesale (prior non-empty black-frame list and subtitle are cleared);
- pins fingerprint is `EMPTY_PINS_FINGERPRINT` with no pins and the real hash with pins, computed for `cut.id` (not
  `reel_id`); guide fingerprint equals `compute_guide_fingerprint(cut.guide)`;
- atomicity, parametrized over {pins computation raises, guide computation raises, empty candidates ->
  `pytest.raises(IndexError)`} on a `Cut` pre-seeded with non-`None` sentinels on all eight columns: every column keeps
  its sentinel;
- `record` never commits, flushes, refreshes or expires (spies on `db.commit/flush/refresh/expire*`; meaningful only
  because `db_session` has `autoflush=False`, so the test also asserts `db.autoflush is False`);
- `RenderOutput` is frozen (`FrozenInstanceError`).

Staleness checks: `None` skips for pins and guide **without calling the compute function** (patched, `assert_not_called`)
even with live pins/guide; match passes; mismatch raises with `str(exc.value)` **equal to the literal old message** for
each function, plus cross-absence ("current guide" not in the pins message and vice versa) — the existing tests match
only the shared substring "Re-render before publishing"; black-frame sentinel still caught after a partial re-pin; a
patch of `render_output.compute_*` is seen by both checks; `gate.assert_video_matches_pins/guide` delegate (patching
`render_output.assert_*_not_stale` is seen).

Writer-to-reader round trips with the real `record` (existing gate tests hand-roll the writer): `record` then
`assert_pins_not_stale` passes with zero pins, passes with real pins, raises after a re-pin; `record` then
`assert_guide_not_stale` passes, raises after a guide edit.

G-A, G-B, G-C, G-D as in §4.5, each with controls.

### 6.4 Mutation battery (each must turn the named test red, then be reverted; the table goes into §10)

| Mutant | Killed by |
|---|---|
| R1 drop the `thumbnail_path` assignment · R2 `thumbnail_path = candidates[-1]` · R21 candidates stored reversed | G-B / `record` column test with two candidates; commit-1 wiring test |
| R3 drop `or None` on black frames · R4 store `"None"` for a missing subtitle · R24 conditional assignment (stale retention) | `record` empty/`None` tests and the wholesale-replace test |
| R33 drop the `list(...)` coercion on black frames (tuple input) | `type(...) is list` check before flush |
| R5 pins fingerprint via the raw function · R7 swap the two fingerprints · R27 pins computed with `cut.reel_id` · R6 guide fingerprint of a copy missing a key | `record` fingerprint tests (decoy ids, non-empty guide) |
| R8 assign before computing · R22 guide computed after the assignments · R23 `candidates[0]` evaluated mid-block | the three atomicity cases (R22 only by the guide-raises case) |
| R9 drop a name from `RENDER_ARTIFACT_COLUMNS` · R29 `record` omits one assignment while the tuple lists it | G-B (sentinel history), G-A |
| R10 `_assert_not_stale` raises on `None` · R11 drop the `!=` check · R12 swap the two messages · R25 eager compute | staleness-check tests (`assert_not_called`, literal messages, cross-absence) |
| R13 gate delegate calls the wrong check · R26 delegate via from-import | delegation tests |
| R14 `record` commits · R34 `record` flushes/refreshes/expires | spy test |
| R15 stray `cut.video_path =` in `cuts.py` · R36 stray `Cut(video_path=...)` · R16 an unclassified `Cut` column | G-C (attribute and keyword forms), G-A |
| R17 `compute_guide_fingerprint` without `sort_keys` | `tests/test_guide_schema.py` key-order test |
| R18 `render_cut` skips `record` · R19 adapter wires `video_path=thumb_path` · R20 adapter wires a wrong `duration_s` | the commit-1 wiring test (R18 also by the fingerprint tests at `test_render_task.py:262,296`) |
| R28 `record` before the `db.refresh`/posted check | `tests/test_render_task.py:137` |
| R37 `record` also writes another column (`caption`, `status`) · R38 `assert_guide_not_stale` reads the pins column, or the reverse · R39 `assert_pins_not_stale` (or another non-`record` function) stores to a column | G-B (no other column changed), the round-trip tests, G-C's "no other function in `render_output.py` stores" assertion |
| R30 an unstringified `Path`/tuple stored | `type(...)` checks and the round trip |
| R31 `RenderOutput` unfrozen · R32 a moved name re-exported from `asset_sourcer` · R35 `render_output` imports `guide_schema` | frozen test, G-D |

## 7. Risks and known gaps

- G-C is a tripwire, not a proof. It misses `setattr` with a non-literal name, subscript writes with a non-literal key, an
  aliased receiver other than `__dict__`/`vars(...)` for the `duration_s` subscript case, a `Cut` reached through an alias for
  the `duration_s` attribute allowlist, an aliased import of the model (`from api.models import Cut as C`, `C = Cut`) which evades the keyword rule,
  `Cut(**fields)`, `cut.__setattr__("video_path", x)`, `bulk_update_mappings(models.Cut, [...])`, writes through
  `Query.update()` with computed keys, and raw SQL (`text("UPDATE cuts ...")`). (A bare `Cut.<name>` dict key, as in
  `q.update({Cut.rendered_pins_fingerprint: None})`, is covered since code review round 1.) G-D does not see
  `importlib.import_module(...)` or `__import__(...)`. The columns are covered for the listed direct forms only.
- `thumbnail_path` has a second sanctioned writer (`choose_thumbnail`), allowlisted by function name; moving it into the
  module would be a reader/writer-split decision deliberately not taken (Q1).
- `RENDER_ARTIFACT_COLUMNS` and the `Cut` model remain two lists; G-A ties them but cannot stop an author classifying a
  new render artifact as `NOT_RENDER_OUTPUT`.
- Pre-existing and unchanged: `record` fingerprints `cut.guide` as re-read by `db.refresh(cut)` (render.py:150), not a
  snapshot taken at task start (render.py:37). The "nothing to race" argument in CLAUDE.md holds because the
  guide-editing routes 409 outside `in_review`. Carrying the guide fingerprint as a `RenderOutput` field computed from
  the build-time snapshot would remove that dependency; a separate decision, not taken here.
- Rollout: no schema change. Restart workers (they do not hot reload); old and new code never share a process; rollback
  is a code revert.

## 8. Documentation to update (grep each for the moved names; fix every hit)

CLAUDE.md: the module-layout entries that name the old homes (`render.py` :293-298, `guide_schema` :388-390,
`asset_sourcer` :519-525, `tts.py` :610 which names `_audio_duration()`, `gate` :712-721), a new `render_output`
entry, the Key-conventions bullets on the two staleness gates (:1772-1773 and neighbours), the test-list entries that name
the moved functions (~:1158-1159, :1310-1316, :1325, :1338-1341, :1419), the Data-model `cuts` bullet (~:1673), totals (the "78 files"
Commands line, the 3075 figure) and a Build-phase 8h entry. `docs/architecture.md` (lines ~131, 148, 168, 198, 450,
578-579, 642; a `render_output.py` row in the render module table near :168; the per-file test table after :198 gains
`test_render_output.py` and the changed `test_render_task.py` count; the "78 test files" count), `docs/data-model.md:48-49`,
`docs/design.md:178-201`, `README.md:347` test count, `CONTEXT.md` (a short "Render output" glossary section, as C1 and C2
added for their concepts; the existing "Related but distinct" staleness paragraph at :64-68 names the gates, not the moved
symbols, and is only re-read for accuracy). Edited in the move commit rather than the docs commit, because they name the
moved homes: the comments in `api/models.py:111,121` and `worker/tasks/render.py`; the moved docstrings (gate.py:48-79
and :92-115; asset_sourcer.py:840-896 — docstrings 841-856 and 889-895, comment 868-884; guide_schema.py:85-104); the
test-file comment pointers (§6.2). `docs/roadmap.md` and older design specs name the old homes as historical record and
are deliberately not edited (same policy as C2), and neither are the superseded plans under `docs/superpowers/plans/`.

## 9. Open questions

None. The grilling round settled Q1-Q7; review findings of rounds 1-3 are folded in (§11).

## 10. Commit plan and implementation record

Plan file: `docs/specs/2026-11-render-output-implementation-plan.md`.

0. Spec (+ plan), reviewed to zero findings.
1. `test: pin render_cut's writes before moving them` — the §6.3 wiring test only; green on the unmodified writer, run
   recorded below.
2. `refactor: move the render-output persistence and staleness checks into render_output` — new module, `render.py`,
   `gate.py`, `guide_schema.py` (imports), `asset_sourcer.py` (removal), `api/models.py` (comments only), docstring/comment pointer edits, mechanical test
   retargets, `STAYS` edit; **no new tests** (§6.1). Full suite and `ruff check --select F,E9 .` before the commit.
3. `test: pin record, the staleness checks and the column ledger` — §6.3 and §4.5; battery run and recorded.
4. `refactor: make tts.audio_duration public`.
5. `docs: ...` — §8 and this section.
Review-fix commits follow as needed. No push until the code-review rounds are clean; no merge without "check ci and merge".

Implementation record (filled in by the docs commit):

**Baseline.** `pytest --collect-only -q` at `24cbc91`: 3075 selected (3072 passed, 3 skipped; the golden-reel test deselected).

**Commits** (`git log --oneline d949361..HEAD`, after the spec/plan commit `d949361`):

| Commit | Message |
|---|---|
| `95f60f4` | `test: pin render_cut's writes before moving them` (the wiring test; +1 selected = 3076) |
| `84b6767` | `refactor: move the render-output persistence and staleness checks into render_output` (14 files: new module, `render.py`, `gate.py`, `guide_schema.py`, `asset_sourcer.py`, `api/models.py` comments, seven retargeted test files, `_seam_oracle.py`) |
| `46f3e24` | `test: pin record, the staleness checks and the column ledger` (`tests/test_render_output.py`, 78 tests = 3154 selected; 84 after code review round 1, see §11) |
| `0c3d331` | `refactor: make tts.audio_duration public` |
| (this commit) | `docs: document render_output and record the evidence` |

**T1, mutation check on the OLD writer** (`worker/tasks/render.py` at `24cbc91`, wiring test of commit `95f60f4`; each mutant applied, the test must go red, reverted): 10 mutants, all red — the `video_path`, `thumbnail_path`, `thumbnail_candidates` and `duration_s` assignments each dropped, `duration_s = 18.0` (the guide's beat sum instead of 17.25), the thumbnail taken from `candidates[-1]`, `video_path = thumb_path`, the candidates stored reversed, `thumbnail_path = thumb_path`, `duration_s = int(duration)`.

**Normalized-diff proof of the mechanical retargets** (throwaway script, `ast.dump` equality after stripping docstrings, ignoring comments, flattening `ImportFrom` into a `(module, name, asname)` multiset with the four-name module remap, and rewriting exactly the six patch-target strings): all seven files pass (`tests/test_asset_sourcer.py`, `tests/test_sourcer_pins_and_music.py`, `tests/test_publish_gate.py`, `tests/test_guide_schema.py`, `tests/test_cuts_publish_router.py`, `tests/test_render_task.py` against its commit-1 version, `tests/test_publish_task.py`); the rewritten patch-string count asserted to be six; 14 negative controls (the kinds required by the plan: an extra edit, a moved-name import pointed at the wrong module, a patch string with the wrong prefix) each detected. `tests/_seam_oracle.py` checked separately: `STAYS` minus exactly the three pins names. The move commit's sorted node-id set equals the baseline set plus exactly the commit-1 test.

**Final counts** (`24cbc91` -> HEAD): 3075 -> 3160 selected; the full run is 3157 passed, 3 skipped, 1 deselected (golden); 78 -> 79 test files; `tests/test_render_output.py` 84 tests (78 at commit `46f3e24`, +6 from code review round 1); `tests/test_render_task.py` 19 -> 20; `ruff check --select F,E9 .` clean.

**G-C hit list on the real tree** (eleven attribute stores of the eight names in production code): `api/routers/cuts.py:294` `choose_thumbnail` (`thumbnail_path`), `engine/generation/beat_enrichment.py:117` `_enrich_with_insight` (`duration_s` of a `BeatStub`), `worker/tasks/generate.py:352` `_enrich_standard_path_guide` (`duration_s` of a `Beat`), and the eight stores of `record()` in `engine/render/render_output.py`; none in `worker/tasks/render.py`.

**Mutation battery R1-R39** (§6.4; 52 runs because several mutants have variants: R13a/b, R20a-c, R24a/b, R30a-e, R34a-c, R37a/b, R38a/b, R39a/b; each applied, the named test turned red, reverted). All 52 killed:

| Run | Mutation | File mutated | First killer (tests failing) |
|---|---|---|---|
| R1 | drop the thumbnail_path assignment | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (10) |
| R2 | thumbnail_path = candidates[-1] | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (4) |
| R3 | drop `or None` on black frames | engine/render/render_output.py | `test_an_empty_black_frame_input_is_none_not_an_empty_list` (3) |
| R4 | store 'None' for a missing subtitle | engine/render/render_output.py | `test_a_missing_subtitle_is_none_not_the_string_none` (3) |
| R5 | pins fingerprint via the raw function | engine/render/render_output.py | `test_record_survives_a_flush_commit_expire_and_reload` (6) |
| R6 | guide fingerprint of a copy missing a key | engine/render/render_output.py | `test_record_survives_a_flush_commit_expire_and_reload` (6) |
| R7 | swap the two fingerprints | engine/render/render_output.py | `test_record_survives_a_flush_commit_expire_and_reload` (15) |
| R8 | assign before computing | engine/render/render_output.py | `test_record_is_atomic_in_memory[pins computation raises]` (4) |
| R9 | drop a name (subtitle_path) from RENDER_ARTIFACT_COLUMNS | engine/render/render_output.py | `test_the_sentinel_ledger_covers_exactly_the_artifact_columns` (10) |
| R10 | _assert_not_stale raises on None | engine/render/render_output.py | `test_a_none_pins_fingerprint_skips_without_computing_even_with_live_pins` (9) |
| R11 | drop the != check | engine/render/render_output.py | `test_a_pins_mismatch_raises_the_literal_message_and_not_the_guide_one` (16) |
| R12 | swap the two messages | engine/render/render_output.py | `test_a_pins_mismatch_raises_the_literal_message_and_not_the_guide_one` (4) |
| R13a | gate pins delegate calls the guide check | engine/publish/gate.py | `test_the_gate_pins_check_delegates_to_render_output` (6) |
| R13b | gate guide delegate calls the pins check | engine/publish/gate.py | `test_the_gate_guide_check_delegates_to_render_output` (5) |
| R14 | record commits | engine/render/render_output.py | `test_record_never_commits_flushes_refreshes_or_expires` (4) |
| R15 | stray cut.video_path = in cuts.py | api/routers/cuts.py | `test_G_C_the_real_tree_has_no_stray_writer` (2) |
| R16 | an unclassified Cut column | api/models.py | `test_G_A_every_cut_column_is_classified_exactly_once` (2) |
| R17 | compute_guide_fingerprint without sort_keys | engine/render/render_output.py | `test_record_survives_a_flush_commit_expire_and_reload` (4) |
| R18 | render_cut skips record | worker/tasks/render.py | `render_task::test_subtitle_path_is_populated_on_a_successful_render` (7) |
| R19 | adapter wires video_path=thumb_path | worker/tasks/render.py | `render_task::test_render_cut_records_every_render_artifact_column` (1) |
| R20a | adapter wires int(duration) | worker/tasks/render.py | `render_task::test_render_cut_records_every_render_artifact_column` (1) |
| R20b | adapter wires round(duration) | worker/tasks/render.py | `render_task::test_render_cut_records_every_render_artifact_column` (1) |
| R20c | adapter wires the beat-sum duration | worker/tasks/render.py | `render_task::test_render_cut_records_every_render_artifact_column` (1) |
| R21 | candidates stored reversed | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (4) |
| R22 | guide computed after the assignments | engine/render/render_output.py | `test_record_is_atomic_in_memory[guide computation raises]` (1) |
| R23 | candidates[0] evaluated mid-block | engine/render/render_output.py | `test_record_is_atomic_in_memory[empty candidates]` (1) |
| R24a | conditional black-frame assignment (stale retention) | engine/render/render_output.py | `test_an_empty_black_frame_input_is_none_not_an_empty_list` (3) |
| R24b | conditional subtitle assignment (stale retention) | engine/render/render_output.py | `test_a_missing_subtitle_is_none_not_the_string_none` (3) |
| R25 | eager compute | engine/render/render_output.py | `test_a_none_pins_fingerprint_skips_without_computing_even_with_live_pins` (7) |
| R26 | gate delegate via from-import | engine/publish/gate.py | `test_the_gate_pins_check_delegates_to_render_output` (1) |
| R27 | pins computed with cut.reel_id | engine/render/render_output.py | `test_the_pins_fingerprint_is_the_real_hash_of_this_cuts_own_pins` (7) |
| R28 | record before the db.refresh / posted check | worker/tasks/render.py | `render_task::test_a_render_that_a_publish_overtook_is_discarded` (1) |
| R29 | record omits one assignment while the tuple lists it | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (10) |
| R30a | unstringified Path video_path | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (10) |
| R30b | unstringified candidates (Paths in the list) | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (12) |
| R30c | tuple of strings as candidates | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (5) |
| R30d | unstringified Path thumbnail_path | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (12) |
| R30e | unstringified Path subtitle | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (10) |
| R31 | RenderOutput unfrozen | engine/render/render_output.py | `test_render_output_is_frozen` (1) |
| R32 | a moved name re-exported from asset_sourcer | engine/render/asset_sourcer.py | `test_G_D_the_moved_names_are_gone_from_their_old_homes` (1) |
| R33 | drop the list(...) coercion on black frames | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (2) |
| R34a | record flushes | engine/render/render_output.py | `test_record_never_commits_flushes_refreshes_or_expires` (4) |
| R34b | record refreshes | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (22) |
| R34c | record expires | engine/render/render_output.py | `test_record_writes_every_column_with_plain_json_types_before_any_flush` (22) |
| R35 | render_output imports guide_schema | engine/render/render_output.py | `test_G_D_render_output_imports_none_of_the_forbidden_modules` (1) |
| R36 | stray Cut(video_path=...) | api/routers/cuts.py | `test_G_C_the_real_tree_has_no_stray_writer` (2) |
| R37a | record also writes caption | engine/render/render_output.py | `test_G_B_record_changes_exactly_the_eight_columns_and_no_other` (3) |
| R37b | record also writes status | engine/render/render_output.py | `test_G_B_record_changes_exactly_the_eight_columns_and_no_other` (19) |
| R38a | assert_guide_not_stale reads the pins column | engine/render/render_output.py | `test_a_none_guide_fingerprint_skips_without_computing_even_with_a_live_guide` (8) |
| R38b | assert_pins_not_stale reads the guide column | engine/render/render_output.py | `test_a_none_pins_fingerprint_skips_without_computing_even_with_live_pins` (11) |
| R39a | assert_pins_not_stale stores to a column | engine/render/render_output.py | `test_G_C_the_real_tree_has_no_stray_writer` (2) |
| R39b | assert_guide_not_stale stores to a column | engine/render/render_output.py | `test_G_C_the_real_tree_has_no_stray_writer` (2) |

**Review.** Spec: four rounds of four personas (§11), round 4 clean. Code review rounds are recorded in §11 and in the pull request.

## 11. Review history

Round 1 (security, correctness, test quality, docs consistency): security clean (5 Low); the other three NOT CLEAN
(1 High: writer wiring unpinned for four columns; Mediums: stale baseline count, non-exhaustive retarget table, G-C
receiver rule contradiction, G-B false negatives, atomicity claim vs step list, single-mutant atomicity test, weak R2,
weak pure-move evidence, message swap undetectable, missing mutants, doc-scope omissions, fidelity-script description).
Folded into r2.

Round 2: all four NOT CLEAN. Common High/Medium: G-C as written fired on legitimate code (keyword arguments of other
callees, `"duration_s"` dict key, bare-name stores, `render.py:140`, the adapter) and its counts were wrong; the writer
characterization came only after the move (circular); the retarget `ast.dump` check was undefined for split imports.
Lows: sentinel requirement on fingerprint columns, subscript gap, mutant-to-test table, extra mutants (list coercion,
flush/refresh, G-D, `Cut(...)` kwargs), docstring/range corrections, CONTEXT.md decision, section order vs siblings,
plan file. Folded into r3.

Round 3: all four NOT CLEAN. High (security, correctness, test quality): the G-C subscript rule fired on three legitimate
`duration_s` dict-key stores (`render.py:111,144`, `guide_edit.py:56`) — rule narrowed, non-vacuity restated. Mediums: the
normalized-diff base for `test_render_task.py` must be commit 1's version; "nine" patch strings/files is six strings and
seven files; the whole of `render_output.py` was exempt from G-C instead of `record`'s body only. Lows: line ranges,
CLAUDE.md/architecture.md pointer lists, extra negative controls, R18/R28 attribution, a non-integral duration, mutants
R37-R38, the aliased-import gap, "up to seven columns". Folded into r4.

Round 4: all four personas clean (security, correctness, test quality, docs consistency); no findings. Implementation began on r4.

Code review round 1 (security, correctness, test quality, docs consistency on `main..HEAD`): security, correctness and docs
CLEAN (Lows); test quality NOT CLEAN — one Medium: `ensure_ascii=False` in the guide fingerprint survived all tests because
every fixture guide was pure ASCII (the persisted fingerprint format of any guide with an accented name, e.g. "Martínez",
was unpinned). Fixed with an accented character in the shared fixture guide. Lows fixed: a cleared guide, an empty-string
stored fingerprint and a last-character-only difference are each a mismatch (tests); four candidates (the production count)
and a two-element black-frame list through `render_cut` are pinned; a duplicated assertion removed; G-C also matches a bare
`Cut.<name>` dict key; spec §7 names the remaining G-C/G-D gaps; docs wording (`output`, design.md pointer sentence, the
"structural" overclaim, `api/models.py` comments).
