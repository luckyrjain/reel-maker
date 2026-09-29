"""Enforces the safe_to_publish gate before any cut is published.

Wikipedia images are often CC-BY-SA (requires attribution) or non-free —
see asset_sourcer.py's license fetch, which computes safe_to_publish on
every Asset row. Pexels and HuggingFace-generated assets are always
safe_to_publish=True. Computing the field is not the same as enforcing it:
this module is the one place that actually blocks a publish on it.
"""
from api import models
from engine.generation.guide_schema import compute_guide_fingerprint
from engine.render.asset_sourcer import compute_pins_fingerprint_for_render


def unsafe_assets(db, cut_id: int) -> list[dict]:
    """Return one dict per asset bound to this cut that isn't safe_to_publish."""
    rows = (
        db.query(models.CutAsset, models.Asset)
        .join(models.Asset, models.CutAsset.asset_id == models.Asset.id)
        .filter(models.CutAsset.cut_id == cut_id)
        .all()
    )
    return [
        {
            "beat_index": cut_asset.beat_index,
            "source": asset.source,
            "source_ref": asset.source_ref,
            "license": asset.license,
        }
        for cut_asset, asset in rows
        if not asset.safe_to_publish
    ]


def assert_safe_to_publish(db, cut_id: int) -> None:
    """Raise ValueError (deterministic, not retried) if any bound asset isn't cleared."""
    unsafe = unsafe_assets(db, cut_id)
    if unsafe:
        details = "; ".join(
            f"beat {u['beat_index']}: {u['source']}/{u['source_ref']} ({u['license'] or 'unknown license'})"
            for u in unsafe
        )
        raise ValueError(
            f"Cannot publish — {len(unsafe)} asset(s) are not cleared for publishing: {details}"
        )


def assert_video_matches_pins(db, cut: "models.Cut") -> None:
    """Raise ValueError (deterministic, not retried — same class as assert_safe_to_publish)
    if the CURRENT CutAsset pins for this cut no longer match the pins that built
    cut.video_path.

    A mismatch means a render after the one that produced cut.video_path re-pinned at
    least one beat's asset and then failed before finishing — the classic staleness hole:
    the gate would otherwise check only the current (possibly different, possibly safer or
    less safe) pins while the video actually shipping was built from the earlier ones. See
    docs/roadmap.md's Open Issues entry and
    docs/specs/2026-09-video-pins-staleness-gate-system-design.md for the full failure
    sequence this closes.

    cut.rendered_pins_fingerprint is None means "no completed render has ever written this
    column" — either never rendered at all, or rendered before this column existed (a
    legacy row) — treated as "unknown, don't block" rather than a mismatch. This is a
    deliberate rollout-safety decision (design §7), not an oversight: it lets this check
    ship without retroactively blocking every already-rendered cut in the database, at the
    cost of not protecting legacy rows until their next successful re-render. See
    CLAUDE.md's Key conventions entry for the full reasoning.

    Deliberately NOT the same as "this cut's most recent successful render bound zero real
    assets" (every beat black-framed) — that case must still write a real, comparable value
    (compute_pins_fingerprint_for_render()'s EMPTY_PINS_FINGERPRINT sentinel), not None, or
    a cut whose asset sourcing keeps failing would be permanently exempt from this check on
    every future publish attempt, not just until its next re-render. See that function's
    docstring for why conflating the two was a real gap caught by independent review.

    Takes the Cut object directly (not cut_id, unlike assert_safe_to_publish) since the
    caller already has it loaded and this avoids a redundant fetch — publish_cut is the
    sole caller of both functions and already has both forms available, so this asymmetry
    costs nothing in practice.
    """
    if cut.rendered_pins_fingerprint is None:
        return
    current = compute_pins_fingerprint_for_render(db, cut.id)
    if current != cut.rendered_pins_fingerprint:
        raise ValueError(
            "Cannot publish — the rendered video no longer matches the currently pinned "
            "assets (a render after this video was built changed at least one beat's "
            "asset and then failed before completing). Re-render before publishing."
        )


def assert_video_matches_guide(db, cut: "models.Cut") -> None:
    """Raise ValueError (deterministic, not retried — same class as
    assert_safe_to_publish/assert_video_matches_pins) if the CURRENT `cut.guide` no longer
    matches the guide content that built `cut.video_path`.

    Closes the "'Retry publish' on a failed cut can ship a stale pre-edit video" Open
    Issues item: an operator can edit `guide` (PATCH /cuts/{id}, or a hook-variant swap —
    both gated to "in_review") and then trigger a re-render that FAILS before completing.
    `video_path` still points at the PRE-EDIT video, `cut.guide` already reflects the edit,
    and `CUT_TRANSITIONS["failed"]` intentionally allows retrying publish straight from the
    "failed" card (so a failed *publish* doesn't force an unrelated full re-render) — which
    would ship the stale video with no check that it still matches what the operator most
    recently approved editing. See docs/specs/2026-09-stale-video-on-failed-rerender-
    system-design.md for the full failure sequence.

    Same rollout-safety null-handling as assert_video_matches_pins():
    `cut.rendered_guide_fingerprint is None` means "no completed render has ever written
    this column" (never rendered, or rendered before this column existed) — treated as
    "unknown, don't block" rather than a mismatch, so this ships without retroactively
    blocking every already-rendered cut in the database. Self-heals on that cut's next
    successful re-render.

    Takes the Cut object directly, same asymmetry as assert_video_matches_pins and for the
    identical reason: publish_cut is the sole caller of all three gate functions and already
    has the Cut loaded."""
    if cut.rendered_guide_fingerprint is None:
        return
    current = compute_guide_fingerprint(cut.guide)
    if current != cut.rendered_guide_fingerprint:
        raise ValueError(
            "Cannot publish — the rendered video no longer matches the current guide "
            "(the guide was edited after this video was built, and the re-render that "
            "should have caught up either failed or hasn't run yet). Re-render before "
            "publishing."
        )
