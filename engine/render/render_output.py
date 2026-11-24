"""What a successful render produced, and the check that it still describes the cut.

Eight `Cut` columns share one lifecycle: one successful render writes them together, the next
render replaces them wholesale, and the last two are compared against fresh values at publish
time. This module is the one owner of that lifecycle:

- `RENDER_ARTIFACT_COLUMNS` names the eight columns.
- `RenderOutput` is what a render produced; `record()` is the only code that writes the eight
  columns for a render (the operator's thumbnail pick, `api/routers/cuts.py::choose_thumbnail`,
  is the one other sanctioned writer, of `thumbnail_path` only).
- `compute_pins_fingerprint_for_render()` and `compute_guide_fingerprint()` are how the two
  snapshots are computed; `assert_pins_not_stale()` and `assert_guide_not_stale()` recompute
  them at publish time and compare. `engine/publish/gate.py` delegates to the two checks.

Depends on `api.models` and the standard library only (no `engine.generation`,
`engine.publish`, `asset_sourcer` or `worker`). See
docs/specs/2026-11-render-output-module-design.md.
"""
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from api import models


def compute_pins_fingerprint(db, cut_id: int) -> str | None:
    """Deterministic fingerprint of every CutAsset currently bound to this cut, across all
    beats. Used by render_cut (worker/tasks/render.py, through record()) to snapshot what
    actually built a render, and by assert_pins_not_stale() (reached from
    engine/publish/gate.py::assert_video_matches_pins()) to detect a later
    re-render that re-pinned an asset and then failed before video_path caught up — see
    docs/specs/2026-09-video-pins-staleness-gate-system-design.md.

    Returns None (not an empty-string hash) when the cut has zero bound CutAsset rows —
    "nothing to fingerprint yet" (every beat black-framed, or not rendered at all), the same
    nullable-render-artifact semantics as black_frame_beat_indices/thumbnail_candidates.

    Order-independent w.r.t. query result ordering: (beat_index, order_in_beat, asset_id) are
    plain ints, so sorting the tuples before hashing guarantees the same pin set always
    produces the same fingerprint regardless of how the DB returns rows — but NOT order-
    independent in the sense of ignoring which asset plays in which beat; a genuine change to
    any one pin changes the fingerprint.
    """
    rows = (
        db.query(models.CutAsset.beat_index, models.CutAsset.order_in_beat, models.CutAsset.asset_id)
        .filter(models.CutAsset.cut_id == cut_id)
        .all()
    )
    if not rows:
        return None
    canonical = "|".join(f"{b}:{o}:{a}" for b, o, a in sorted(rows))
    return hashlib.sha256(canonical.encode()).hexdigest()[:64]


# Sentinel for "a render completed successfully but bound zero real pins" (every beat
# black-framed — resolve_beat_assets()'s whole fallback chain came up empty). Deliberately
# NOT a valid sha256 hexdigest shape, so it can never collide with a real fingerprint.
#
# Why this needs to be distinct from compute_pins_fingerprint()'s own `None` return: that
# `None` is overloaded to mean two different things — "nothing pinned yet" and "this cut's
# render never happened at all" — and assert_pins_not_stale() (engine/publish/gate.py's
# assert_video_matches_pins() delegates to it) treats `cut.rendered_pins_fingerprint is None`
# as "legacy row, unknown, don't block" (see that function's docstring). If render_cut wrote
# compute_pins_fingerprint()'s raw `None` for a genuinely successful all-black-frame render,
# that render's cut would look indistinguishable from a never-rendered/pre-migration one —
# and a LATER render that pins one or more beats to real assets and then fails before
# finishing would leave live pins non-empty while cut.rendered_pins_fingerprint stayed
# `None`, silently exempted from the mismatch check forever (or until a render happens to
# also produce zero pins again). That is not a bounded rollout gap the way the true
# legacy-row case is — a black-frame outcome can recur indefinitely for a niche/topic where
# asset sourcing keeps failing, so this would permanently defeat the staleness gate for
# exactly the cuts most likely to need it.
EMPTY_PINS_FINGERPRINT = "no-pins-bound"


def compute_pins_fingerprint_for_render(db, cut_id: int) -> str:
    """Like compute_pins_fingerprint(), but never returns None — a render that completes
    with zero bound pins gets EMPTY_PINS_FINGERPRINT instead, so a completed render is
    always distinguishable from "never rendered." Both record() (writer) and
    assert_pins_not_stale() (reader) must use THIS function, not the raw
    compute_pins_fingerprint(), for Cut.rendered_pins_fingerprint — using the raw function
    at either site reopens the black-frame staleness hole described above.
    """
    return compute_pins_fingerprint(db, cut_id) or EMPTY_PINS_FINGERPRINT


def compute_guide_fingerprint(guide: dict | None) -> str | None:
    """Deterministic fingerprint of a serialized `PlatformGuide` dict (`cuts.guide`, see
    api/models.py::Cut). Used by record() (called from render_cut, worker/tasks/render.py)
    to snapshot what guide content actually built a render, and by assert_guide_not_stale()
    (engine/publish/gate.py::assert_video_matches_guide() delegates to it) to detect a later
    guide edit (PATCH /cuts/{id}, or a hook-variant swap) followed by a re-render that fails
    before `video_path` catches up — see docs/specs/2026-09-stale-video-on-failed-rerender-
    system-design.md. The sibling of compute_pins_fingerprint(), same shape, different
    content: pins track which ASSETS are bound, this tracks the guide's own text/timing
    content.

    `json.dumps(guide, sort_keys=True)` — `sort_keys` makes dict-key ordering irrelevant at
    every level, but deliberately does NOT touch list ordering: `beats` stays in its
    meaningful sequence (beat order is semantic), and any genuine content change — including
    a reordered `hashtags` list — changes the fingerprint. This mirrors
    compute_pins_fingerprint()'s own "a genuine change to any one pin changes the
    fingerprint" philosophy applied to guide content instead of asset pins.

    Returns None for a falsy `guide` (not expected in practice — every call site here has
    already confirmed `cut.guide` is set before a render can even be triggered — but matches
    compute_pins_fingerprint()'s own None-in/None-out defensiveness rather than raising)."""
    if not guide:
        return None
    canonical = json.dumps(guide, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:64]


# The Cut columns one successful render writes together and the next render replaces wholesale.
# record() is the only writer of all eight; tests/test_render_output.py ties this tuple to the
# model and walks the tree for any other writer.
RENDER_ARTIFACT_COLUMNS: tuple[str, ...] = (
    "video_path",
    "duration_s",
    "thumbnail_path",
    "thumbnail_candidates",
    "black_frame_beat_indices",
    "subtitle_path",
    "rendered_pins_fingerprint",
    "rendered_guide_fingerprint",
)


@dataclass(frozen=True)
class RenderOutput:
    """What one successful render produced. `thumbnail_path` is not a field: it is
    `thumbnail_candidates[0]` by definition (the operator's later pick in
    api/routers/cuts.py::choose_thumbnail is a partial overwrite)."""

    video_path: Path
    thumbnail_candidates: Sequence[Path]      # must be non-empty; [0] becomes thumbnail_path
    duration_s: float
    black_frame_beat_indices: Sequence[int]   # empty means "every beat resolved real media"
    subtitle_path: Path | None


def record(db, cut, output: RenderOutput) -> None:
    """Write the eight render-artifact columns of `cut` from `output`, all or none.

    Everything is computed into locals first and only then assigned in one block, so if a
    fingerprint computation (or `thumbnail_candidates[0]` on an empty sequence, an
    IndexError) raises, no column has been touched. Does not commit, flush, refresh or
    expire, and does not check `platform_post_id` or move the cut's status: those stay with
    the caller (render_cut), which must call this AFTER composite_cut() has returned and
    after its "posted while it rendered" refresh. That call-site rule is what keeps the
    fingerprints honest: every beat's resolve_or_reuse() call (and its commit) for this
    render has landed by then, so the pins read sees them (SessionLocal has autoflush=False,
    so pending assignments are not visible to that read either way).

    The pins fingerprint is always compute_pins_fingerprint_for_render(), never the raw
    compute_pins_fingerprint(): a successful all-black-frame render must still write a real,
    comparable value (EMPTY_PINS_FINGERPRINT), not None, or it becomes indistinguishable from
    "never rendered" and permanently exempt from the staleness check. `cut.guide` is the
    exact guide THIS render ran against: trigger_render() moves cut.status to "rendering"
    before enqueueing, and the guide-editing endpoints (update_cut, choose_hook_variant) both
    409 outside "in_review" — no PATCH can land while a render is in flight.
    """
    pins_fp = compute_pins_fingerprint_for_render(db, cut.id)
    guide_fp = compute_guide_fingerprint(cut.guide)
    thumbnail_path = str(output.thumbnail_candidates[0])
    candidates_json = [str(p) for p in output.thumbnail_candidates]
    video = str(output.video_path)
    black = list(output.black_frame_beat_indices) or None
    subtitle = str(output.subtitle_path) if output.subtitle_path else None

    cut.video_path = video
    cut.thumbnail_path = thumbnail_path
    cut.thumbnail_candidates = candidates_json
    cut.duration_s = output.duration_s
    cut.black_frame_beat_indices = black
    cut.subtitle_path = subtitle
    cut.rendered_pins_fingerprint = pins_fp
    cut.rendered_guide_fingerprint = guide_fp


def _assert_not_stale(stored: str | None, compute_current: Callable[[], str | None], message: str) -> None:
    """`stored is None` means "no completed render has ever written this column" (never
    rendered, or rendered before the column existed): unknown, don't block — a deliberate
    rollout-safety decision, so the check ships without retroactively blocking every
    already-rendered cut. `compute_current` is not called on that path (no query). Otherwise
    a differing current value raises ValueError (deterministic, not retried)."""
    if stored is None:
        return
    if compute_current() != stored:
        raise ValueError(message)


def assert_pins_not_stale(db, cut: "models.Cut") -> None:
    """Raise ValueError if the CURRENT CutAsset pins for this cut no longer match the pins
    that built cut.video_path.

    A mismatch means a render after the one that produced cut.video_path re-pinned at
    least one beat's asset and then failed before finishing — the classic staleness hole:
    the safe_to_publish gate would otherwise check only the current (possibly different,
    possibly safer or less safe) pins while the video actually shipping was built from the
    earlier ones. See docs/roadmap.md's Open Issues entry and
    docs/specs/2026-09-video-pins-staleness-gate-system-design.md for the full failure
    sequence this closes.

    cut.rendered_pins_fingerprint is None means "no completed render has ever written this
    column" — treated as "unknown, don't block" (see _assert_not_stale). Deliberately NOT
    the same as "this cut's most recent successful render bound zero real assets" (every
    beat black-framed) — that case writes EMPTY_PINS_FINGERPRINT, not None, or a cut whose
    asset sourcing keeps failing would be permanently exempt from this check.

    Takes the Cut object directly (not cut_id, unlike gate.assert_safe_to_publish) since the
    caller already has it loaded.
    """
    _assert_not_stale(
        cut.rendered_pins_fingerprint,
        lambda: compute_pins_fingerprint_for_render(db, cut.id),
        "Cannot publish — the rendered video no longer matches the currently pinned "
        "assets (a render after this video was built changed at least one beat's "
        "asset and then failed before completing). Re-render before publishing.",
    )


def assert_guide_not_stale(db, cut: "models.Cut") -> None:
    """Raise ValueError if the CURRENT `cut.guide` no longer matches the guide content that
    built `cut.video_path`.

    Closes the "'Retry publish' on a failed cut can ship a stale pre-edit video" Open
    Issues item: an operator can edit `guide` (PATCH /cuts/{id}, or a hook-variant swap —
    both gated to "in_review") and then trigger a re-render that FAILS before completing.
    `video_path` still points at the PRE-EDIT video, `cut.guide` already reflects the edit,
    and `CUT_TRANSITIONS["failed"]` intentionally allows retrying publish straight from the
    "failed" card (so a failed *publish* doesn't force an unrelated full re-render) — which
    would ship the stale video with no check that it still matches what the operator most
    recently approved editing. See docs/specs/2026-09-stale-video-on-failed-rerender-
    system-design.md for the full failure sequence.

    Same rollout-safety null-handling as assert_pins_not_stale():
    `cut.rendered_guide_fingerprint is None` means "no completed render has ever written
    this column" (never rendered, or rendered before this column existed) — treated as
    "unknown, don't block" rather than a mismatch, so this ships without retroactively
    blocking every already-rendered cut in the database. Self-heals on that cut's next
    successful re-render.

    Takes the Cut object directly, same asymmetry as assert_pins_not_stale and for the
    identical reason: the caller already has the Cut loaded."""
    _assert_not_stale(
        cut.rendered_guide_fingerprint,
        lambda: compute_guide_fingerprint(cut.guide),
        "Cannot publish — the rendered video no longer matches the current guide "
        "(the guide was edited after this video was built, and the re-render that "
        "should have caught up either failed or hasn't run yet). Re-render before "
        "publishing.",
    )
