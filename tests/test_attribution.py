"""Tests for engine/publish/attribution.py."""
from api import models
from engine.publish.attribution import build_attribution_block, build_published_caption


def _make_cut_with_asset(db, *, source="wikipedia", attribution=None, license_=None):
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    db.add(cut)
    db.flush()
    asset = models.Asset(
        type="photo", source=source, source_ref="ref1",
        local_path="/tmp/x.jpg", license=license_, attribution=attribution, safe_to_publish=(source != "wikipedia"),
    )
    db.add(asset)
    db.flush()
    db.add(models.CutAsset(cut_id=cut.id, asset_id=asset.id, beat_index=0, order_in_beat=0))
    db.commit()
    return cut


def test_no_assets_returns_empty_block(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    db.add(cut)
    db.commit()

    assert build_attribution_block(db, cut.id) == ""


def test_non_wikipedia_assets_never_get_attribution(db_session):
    db = db_session
    cut = _make_cut_with_asset(db, source="pexels", attribution="Should not appear", license_="pexels_free")
    assert build_attribution_block(db, cut.id) == ""


def test_wikipedia_asset_without_attribution_text_produces_nothing(db_session):
    db = db_session
    cut = _make_cut_with_asset(db, source="wikipedia", attribution=None, license_="CC BY-SA")
    assert build_attribution_block(db, cut.id) == ""


def test_wikipedia_asset_with_attribution_is_included(db_session):
    db = db_session
    cut = _make_cut_with_asset(db, source="wikipedia", attribution="Jane Doe", license_="CC BY-SA 4.0")
    block = build_attribution_block(db, cut.id)
    assert block == "Image credit: Jane Doe (CC BY-SA 4.0)"


def test_duplicate_attributions_are_deduped(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    db.add(cut)
    db.flush()
    for i in range(2):
        asset = models.Asset(
            type="photo", source="wikipedia", source_ref=f"ref{i}",
            local_path=f"/tmp/{i}.jpg", license="CC BY-SA", attribution="Jane Doe", safe_to_publish=False,
        )
        db.add(asset)
        db.flush()
        db.add(models.CutAsset(cut_id=cut.id, asset_id=asset.id, beat_index=i, order_in_beat=0))
    db.commit()

    block = build_attribution_block(db, cut.id)
    assert block.count("Jane Doe") == 1


# ── build_published_caption ──────────────────────────────────────────────────

def test_published_caption_appends_block_after_caption(db_session):
    db = db_session
    cut = _make_cut_with_asset(db, source="wikipedia", attribution="Jane Doe", license_="CC BY-SA")
    cut.caption = "Check out this reel!"
    db.commit()

    caption = build_published_caption(db, cut)
    assert caption == "Check out this reel!\n\nImage credit: Jane Doe (CC BY-SA)"


def test_published_caption_with_no_attribution_is_unchanged(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
        status=models.CutStatus.approved, caption="Check out this reel!",
    )
    db.add(cut)
    db.commit()

    assert build_published_caption(db, cut) == "Check out this reel!"


def test_published_caption_falls_back_to_block_when_caption_empty(db_session):
    db = db_session
    cut = _make_cut_with_asset(db, source="wikipedia", attribution="Jane Doe", license_="CC BY-SA")
    cut.caption = None
    db.commit()

    assert build_published_caption(db, cut) == "Image credit: Jane Doe (CC BY-SA)"
