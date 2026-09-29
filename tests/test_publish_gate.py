"""Tests for engine/publish/gate.py — the safe_to_publish enforcement gate."""
import pytest

from api import models
from engine.generation.guide_schema import compute_guide_fingerprint
from engine.publish.gate import (
    assert_safe_to_publish,
    assert_video_matches_guide,
    assert_video_matches_pins,
    unsafe_assets,
)
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


# ---------------------------------------------------------------------------
# assert_video_matches_guide — the guide-content sibling of assert_video_matches_pins
# (docs/specs/2026-09-stale-video-on-failed-rerender-system-design.md)
# ---------------------------------------------------------------------------

_GUIDE = {
    "platform": "youtube_shorts", "target_length_s": 45.0,
    "beats": [{"index": 0, "type": "hook", "duration_s": 2.0, "visual_direction": "x",
               "on_screen_text": ["Hi"], "vo_script": "Original line.", "transition": "cut"}],
    "caption": "A caption", "hashtags": ["a", "b", "c", "d", "e"],
}
_EDITED_GUIDE = {**_GUIDE, "beats": [{**_GUIDE["beats"][0], "vo_script": "Edited line."}]}


def _make_cut_with_guide(db, guide=None):
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
        status=models.CutStatus.approved, guide=guide or _GUIDE,
    )
    db.add(cut)
    db.commit()
    return cut


def test_guide_matching_fingerprint_does_not_raise(db_session):
    db = db_session
    cut = _make_cut_with_guide(db)
    cut.rendered_guide_fingerprint = compute_guide_fingerprint(cut.guide)
    db.commit()

    assert_video_matches_guide(db, cut)  # must not raise


def test_guide_mismatched_fingerprint_raises_with_an_actionable_message(db_session):
    """The actual bug: operator edits the guide (PATCH /cuts/{id}) after a successful
    render, then a re-render fails before video_path catches up. video_path still points
    at the pre-edit video; cut.guide already reflects the edit."""
    db = db_session
    cut = _make_cut_with_guide(db)
    cut.rendered_guide_fingerprint = compute_guide_fingerprint(cut.guide)
    db.commit()

    cut.guide = _EDITED_GUIDE  # the PATCH edit
    db.commit()

    with pytest.raises(ValueError, match="Re-render before publishing"):
        assert_video_matches_guide(db, cut)


def test_guide_none_fingerprint_does_not_block_even_with_a_real_current_guide(db_session):
    """Same rollout-safety property as assert_video_matches_pins' own None-means-skip
    test: a legacy cut rendered before this column existed must not be blocked, even
    though its current guide computes to a real, non-None fingerprint."""
    db = db_session
    cut = _make_cut_with_guide(db)
    cut.rendered_guide_fingerprint = None
    db.commit()

    assert compute_guide_fingerprint(cut.guide) is not None  # sanity: not vacuous

    assert_video_matches_guide(db, cut)  # must not raise


def test_staleness_bug_sequence_a_guide_edit_after_render_then_a_failed_rerender_is_caught(db_session):
    """End-to-end reproduction of the exact failure sequence from the Open Issues entry:

    1. Render N succeeds: the guide is rendered, cut.video_path is set, and
       cut.rendered_guide_fingerprint is snapshotted from that guide -- exactly what
       worker/tasks/render.py::render_cut does at the end of a successful render.
    2. The operator edits the guide (PATCH /cuts/{id} -- only reachable while
       "in_review", which render_cut's own final transition() call leaves the cut in).
    3. A re-render starts and FAILS before reaching the end of the function, so
       video_path/rendered_guide_fingerprint are never updated -- cut.guide already
       reflects the edit, but video_path still points at the pre-edit file.
    4. assert_video_matches_guide(db, cut) must now raise -- "Retry publish" on the
       resulting "failed" card must not be allowed to ship the stale pre-edit video.
    """
    db = db_session
    cut = _make_cut_with_guide(db)

    # --- Render N: guide renders successfully. ---
    cut.video_path = "/video_store/1/youtube_shorts.mp4"
    cut.rendered_guide_fingerprint = compute_guide_fingerprint(cut.guide)
    db.commit()
    assert_video_matches_guide(db, cut)  # sanity: passes right after it finishes

    # --- Operator edits the guide (PATCH), then a re-render starts and fails before
    # touching video_path/rendered_guide_fingerprint. ---
    cut.guide = _EDITED_GUIDE
    db.commit()

    # --- The gate must catch this on the eventual "Retry publish" attempt. ---
    with pytest.raises(ValueError, match="Re-render before publishing"):
        assert_video_matches_guide(db, cut)


def test_guide_fingerprint_survives_a_real_db_round_trip(db_session):
    """Caught by review as a real, previously-untested gap: nothing proved the fingerprint
    render_cut writes from an in-memory guide dict actually matches the one
    assert_video_matches_guide recomputes at publish time from a FRESH read of the same
    row through the DB's own JSON column encode/decode cycle -- an unrelated-to-content
    round-trip difference (key ordering, float precision, None-vs-missing-key handling)
    silently blocking every publish would be a far worse regression than the staleness
    bug this whole feature exists to catch. Uses values most likely to expose exactly
    that class of drift: a non-terminating float, unicode text, smart quotes, an emoji,
    and an explicit None field."""
    db = db_session
    guide = {
        "platform": "youtube_shorts", "target_length_s": 33.333333333333336,
        "beats": [{"index": 0, "type": "hook", "duration_s": 2.5, "visual_direction": "café ⚽",
                   "on_screen_text": ["“Quoted” text"], "vo_script": "Iñárritu's move.",
                   "music_cue": None, "transition": "fade"}],
        "caption": "A caption", "hashtags": ["a", "b", "c", "d", "e"],
    }
    reel = models.Reel(context="x", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
        status=models.CutStatus.approved, guide=guide,
    )
    db.add(cut)
    db.commit()
    cut_id = cut.id

    fingerprint_at_render_time = compute_guide_fingerprint(guide)

    db.expire_all()  # force the next access to issue a fresh SELECT, not reuse the cached object
    reloaded = db.get(models.Cut, cut_id)
    fingerprint_at_publish_time = compute_guide_fingerprint(reloaded.guide)

    assert fingerprint_at_publish_time == fingerprint_at_render_time
