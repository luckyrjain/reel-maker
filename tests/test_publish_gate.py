"""Tests for engine/publish/gate.py — the safe_to_publish enforcement gate."""
import pytest

from api import models
from engine.publish.gate import assert_safe_to_publish, assert_video_matches_pins, unsafe_assets
from engine.render.asset_sourcer import (
    EMPTY_PINS_FINGERPRINT,
    SourcedAsset,
    compute_pins_fingerprint,
    compute_pins_fingerprint_for_render,
    resolve_or_reuse,
)


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


# ---------------------------------------------------------------------------
# assert_video_matches_pins — staleness gate (docs/specs/2026-09-video-pins-
# staleness-gate-system-design.md)
# ---------------------------------------------------------------------------

def _make_cut_with_pin(db, *, asset_source_ref="ref1"):
    """A cut with one bound CutAsset (beat 0), returning the Cut object (not just its id) —
    assert_video_matches_pins takes a Cut, unlike assert_safe_to_publish's cut_id."""
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved)
    db.add(cut)
    db.flush()
    asset = models.Asset(
        type="photo", source="pexels", source_ref=asset_source_ref,
        local_path="/tmp/x.jpg", license="pexels_free", safe_to_publish=True,
    )
    db.add(asset)
    db.flush()
    db.add(models.CutAsset(cut_id=cut.id, asset_id=asset.id, beat_index=0, order_in_beat=0))
    db.commit()
    return cut


def test_matching_fingerprint_does_not_raise(db_session):
    db = db_session
    cut = _make_cut_with_pin(db)
    cut.rendered_pins_fingerprint = compute_pins_fingerprint_for_render(db, cut.id)
    db.commit()

    assert_video_matches_pins(db, cut)  # must not raise


def test_mismatched_fingerprint_raises_with_an_actionable_message(db_session):
    db = db_session
    cut = _make_cut_with_pin(db)
    # Snapshot a fingerprint, then re-pin the beat to a different asset — simulating a
    # later render that changed the pin and then failed before video_path caught up.
    cut.rendered_pins_fingerprint = compute_pins_fingerprint_for_render(db, cut.id)
    db.commit()

    new_asset = models.Asset(
        type="photo", source="pexels", source_ref="ref2",
        local_path="/tmp/y.jpg", license="pexels_free", safe_to_publish=True,
    )
    db.add(new_asset)
    db.flush()
    db.query(models.CutAsset).filter(
        models.CutAsset.cut_id == cut.id, models.CutAsset.beat_index == 0,
    ).delete()
    db.add(models.CutAsset(cut_id=cut.id, asset_id=new_asset.id, beat_index=0, order_in_beat=0))
    db.commit()

    with pytest.raises(ValueError, match="Re-render before publishing"):
        assert_video_matches_pins(db, cut)


def test_none_fingerprint_does_not_block_even_with_real_current_pins(db_session):
    """CRITICAL — the rollout-safety property design §7 hinges on: a legacy cut rendered
    before this column existed (rendered_pins_fingerprint is None) must NOT be blocked, even
    though its current CutAsset pins compute to a real, non-None fingerprint. Mutation-
    tested: temporarily changing the guard to fire on None too (i.e. `if cut.
    rendered_pins_fingerprint is None or current != cut.rendered_pins_fingerprint: raise`)
    makes this specific test fail; reverting to the `is None: return` early-out makes it
    pass again — confirmed by hand during implementation, see the PR description."""
    db = db_session
    cut = _make_cut_with_pin(db)
    cut.rendered_pins_fingerprint = None
    db.commit()

    # Sanity: current pins really do compute to a real, non-None fingerprint — this test
    # would be vacuous if they didn't.
    assert compute_pins_fingerprint(db, cut.id) is not None

    assert_video_matches_pins(db, cut)  # must not raise


def test_black_frame_render_still_catches_a_later_partial_repin(db_session, tmp_path):
    """CRITICAL — independent review caught a real gap the first version of this fix had:
    a genuinely SUCCESSFUL render whose every beat black-framed (resolve_beat_assets()'s
    whole fallback chain came up empty) used to write cut.rendered_pins_fingerprint = None
    (compute_pins_fingerprint()'s raw "zero pins" return), which is indistinguishable from
    "never rendered" — so assert_video_matches_pins's `is None: return` legacy-skip would
    ALSO silently skip this cut forever, even after a later render pinned real assets and
    then crashed before finishing. That's not a bounded rollout gap like the true legacy
    case — it can recur indefinitely for a niche/topic where asset sourcing keeps failing.

    Fixed by compute_pins_fingerprint_for_render()'s EMPTY_PINS_FINGERPRINT sentinel: a
    completed zero-pin render now writes a real, comparable value instead of None, so the
    exact same staleness detection that already works for a normal re-pin also works here.
    """
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved,
    )
    db.add(cut)
    db.flush()

    # --- Render N "succeeds" with zero real pins (every beat black-framed) — no CutAsset
    # rows exist for this cut at all. render_cut still writes a real fingerprint. ---
    assert compute_pins_fingerprint(db, cut.id) is None  # sanity: genuinely zero pins
    cut.video_path = "/video_store/1/youtube_shorts.mp4"
    cut.rendered_pins_fingerprint = compute_pins_fingerprint_for_render(db, cut.id)
    db.commit()
    assert cut.rendered_pins_fingerprint == EMPTY_PINS_FINGERPRINT

    # Sanity: the black-frame video passes the gate right after it "finishes."
    assert_video_matches_pins(db, cut)  # must not raise

    # --- Render N+1 pins a real asset to beat 0 (asset sourcing recovered), then "crashes"
    # before reaching the video_path/rendered_pins_fingerprint assignment. ---
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(
        db, cut=cut, beat_index=0, visual_direction="Messi through-ball",
        min_duration_s=5.0, sourcer=sourcer,
    )
    # video_path and rendered_pins_fingerprint deliberately NOT touched.

    # --- The gate must catch this: current pins are now non-empty, but the stale
    # video_path was built from zero pins. Without the sentinel fix, both sides of this
    # comparison would have been None and this would have wrongly passed. ---
    with pytest.raises(ValueError, match="Re-render before publishing"):
        assert_video_matches_pins(db, cut)


# ---------------------------------------------------------------------------
# T6 — end-to-end regression test reproducing the EXACT bug sequence from
# docs/roadmap.md's Open Issues entry / docs/specs/2026-09-video-pins-staleness-gate-
# system-design.md §1. This is the single most important test in this rollout: T2's and
# T4's own unit tests above prove compute_pins_fingerprint() and assert_video_matches_pins()
# each behave correctly in isolation against synthetic inputs — this test proves the two
# functions TOGETHER actually close the real hole a real render_cut()/resolve_or_reuse()
# sequence produced.
# ---------------------------------------------------------------------------

class _StubSourcer:
    """Stands in for PexelsVideoSource — returns a distinct asset per query, same as
    tests/test_asset_sourcer.py's helper of the same shape."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.queries = []

    def search(self, query, min_duration_s):
        self.queries.append(query)
        ref = f"vid_{len(self.queries)}"
        return SourcedAsset(
            source="pexels", source_ref=ref, local_path=self.tmp_path / f"{ref}.mp4",
            license_str="pexels_free", safe_to_publish=True, duration_s=10.0,
        )


def test_staleness_bug_sequence_a_repin_that_fails_before_video_path_updates_is_caught(db_session, tmp_path):
    """Reproduces the exact failure sequence from the bug report:

    1. Render N succeeds: a beat is resolved and pinned, cut.video_path is set, and
       cut.rendered_pins_fingerprint is snapshotted from those pins — exactly what
       worker/tasks/render.py::render_cut does at the end of a successful render.
    2. Render N+1 starts, re-pins the SAME beat to a DIFFERENT asset (visual_direction
       changed) via the real resolve_or_reuse() — exactly what render_cut's beat loop
       does — but then fails before reaching the end of the function, so video_path and
       rendered_pins_fingerprint are never updated. This is the "crash mid-render, old
       pin committed, new pin also committed, video_path stuck on the old build" gap
       resolve_or_reuse()'s own incremental-commit design deliberately allows.
    3. assert_video_matches_pins(db, cut) must now raise for the still-stale video_path —
       the gate must not trust the (new, different) current pins as if they built the
       video that would actually ship.
    """
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved,
    )
    db.add(cut)
    db.flush()

    # --- Render N: resolve + pin beat 0, "finish" the render. ---
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(
        db, cut=cut, beat_index=0, visual_direction="Romero tackle",
        min_duration_s=5.0, sourcer=sourcer,
    )
    cut.video_path = "/video_store/1/youtube_shorts.mp4"
    cut.rendered_pins_fingerprint = compute_pins_fingerprint_for_render(db, cut.id)
    db.commit()

    # Sanity: the video render N produced passes the gate right after it finishes.
    assert_video_matches_pins(db, cut)  # must not raise

    # --- Render N+1: re-pins the same beat to a different asset, then "crashes" before
    # reaching the video_path/rendered_pins_fingerprint assignment at the end of render_cut. ---
    resolve_or_reuse(
        db, cut=cut, beat_index=0, visual_direction="Messi through-ball",
        min_duration_s=5.0, sourcer=sourcer,
    )
    # video_path and rendered_pins_fingerprint are deliberately NOT touched here — this is
    # the exact state a died-mid-render_cut leaves the database in.

    # --- The gate must now catch the mismatch: current pins != what built video_path. ---
    with pytest.raises(ValueError, match="Re-render before publishing"):
        assert_video_matches_pins(db, cut)
