"""Enforces the safe_to_publish gate before any cut is published.

Wikipedia images are often CC-BY-SA (requires attribution) or non-free —
see asset_sourcer.py's license fetch, which computes safe_to_publish on
every Asset row. Pexels and HuggingFace-generated assets are always
safe_to_publish=True. Computing the field is not the same as enforcing it:
this module is the one place that actually blocks a publish on it.
"""
from api import models
from engine.render.asset_sourcer import compute_pins_fingerprint


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

    cut.rendered_pins_fingerprint is None means either "not yet rendered" or "rendered
    before this column existed" (a legacy row) — treated as "unknown, don't block" rather
    than a mismatch. This is a deliberate rollout-safety decision (design §7), not an
    oversight: it lets this check ship without retroactively blocking every already-
    rendered cut in the database, at the cost of not protecting legacy rows until their
    next successful re-render. See CLAUDE.md's Key conventions entry for the full
    reasoning.

    Takes the Cut object directly (not cut_id, unlike assert_safe_to_publish) since the
    caller already has it loaded and this avoids a redundant fetch — publish_cut is the
    sole caller of both functions and already has both forms available, so this asymmetry
    costs nothing in practice.
    """
    if cut.rendered_pins_fingerprint is None:
        return
    current = compute_pins_fingerprint(db, cut.id)
    if current != cut.rendered_pins_fingerprint:
        raise ValueError(
            "Cannot publish — the rendered video no longer matches the currently pinned "
            "assets (a render after this video was built changed at least one beat's "
            "asset and then failed before completing). Re-render before publishing."
        )
