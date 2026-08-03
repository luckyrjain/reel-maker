"""Tests for engine/publish/gate.py — the safe_to_publish enforcement gate."""
import pytest

from api import models
from engine.publish.gate import assert_safe_to_publish, unsafe_assets


def _make_cut_with_asset(db, *, safe_to_publish: bool, source="wikipedia", license_="CC BY-SA"):
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    db.add(cut)
    db.flush()
    asset = models.Asset(
        type="photo", source=source, source_ref="ref1",
        local_path="/tmp/x.jpg", license=license_, safe_to_publish=safe_to_publish,
    )
    db.add(asset)
    db.flush()
    db.add(models.CutAsset(cut_id=cut.id, asset_id=asset.id, beat_index=0, order_in_beat=0))
    db.commit()
    return cut.id


def test_cut_with_no_assets_is_safe(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    db.add(cut)
    db.commit()

    assert unsafe_assets(db, cut.id) == []
    assert_safe_to_publish(db, cut.id)  # must not raise


def test_cut_with_all_safe_assets_passes(db_session):
    db = db_session
    cut_id = _make_cut_with_asset(db, safe_to_publish=True, source="pexels", license_="pexels_free")
    assert unsafe_assets(db, cut_id) == []
    assert_safe_to_publish(db, cut_id)  # must not raise


def test_cut_with_unsafe_asset_is_blocked(db_session):
    db = db_session
    cut_id = _make_cut_with_asset(db, safe_to_publish=False, source="wikipedia", license_="CC BY-SA")

    unsafe = unsafe_assets(db, cut_id)
    assert len(unsafe) == 1
    assert unsafe[0]["source"] == "wikipedia"

    with pytest.raises(ValueError, match="not cleared for publishing"):
        assert_safe_to_publish(db, cut_id)
