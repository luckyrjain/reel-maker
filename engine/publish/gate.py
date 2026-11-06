"""Enforces the safe_to_publish gate before any cut is published.

Wikipedia images are often CC-BY-SA (requires attribution) or non-free —
see asset_sourcer.py's license fetch, which computes safe_to_publish on
every Asset row. Pexels and HuggingFace-generated assets are always
safe_to_publish=True. Computing the field is not the same as enforcing it:
this module is the one place that actually blocks a publish on it.
"""
from api import models
from engine.render import render_output


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
    cut.video_path. The logic, the rollout-safety `None` skip and the message live in
    engine/render/render_output.py::assert_pins_not_stale(); publish_cut is the caller."""
    render_output.assert_pins_not_stale(db, cut)


def assert_video_matches_guide(db, cut: "models.Cut") -> None:
    """Raise ValueError (deterministic, not retried) if the CURRENT `cut.guide` no longer
    matches the guide content that built `cut.video_path`. The logic, the rollout-safety
    `None` skip and the message live in
    engine/render/render_output.py::assert_guide_not_stale(); publish_cut is the caller."""
    render_output.assert_guide_not_stale(db, cut)
