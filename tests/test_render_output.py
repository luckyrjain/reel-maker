"""Tests for ``engine/render/render_output.py`` -- the one owner of the eight Cut columns a successful
render writes, of how the two snapshot fingerprints are computed, and of the publish-time staleness checks
(docs/specs/2026-11-render-output-module-design.md, 4.5 guards G-A..G-D and 6.3 "Commit 3").

Layout
  record()            on a real Cut (decoy rows so ``cut.id != cut.reel_id``), tuple inputs, a non-integral
                      duration, sentinels on every column, atomicity, no commit/flush/refresh/expire.
  staleness checks    ``None`` skips without computing, match passes, mismatch raises the literal old message,
                      the gate delegates, and writer-to-reader round trips through the REAL record().
  G-A  column ledger  every Cut column is classified exactly once.
  G-B  exact write set   record() changes the eight columns and no other (SQLAlchemy attribute history).
  G-C  no stray writer   an AST walk of api/ engine/ worker/ for any other writer of the eight names; the
                         checker has a synthetic snippet for every rule that must fire and every exclusion.
  G-D  module seam    render_output imports none of engine.generation / engine.publish / asset_sourcer / worker,
                      and the four moved names are gone from their old homes.

Run with ``python -m pytest`` (a bare ``pytest`` from a worktree can import the main checkout).
"""
import ast
import dataclasses
import hashlib
import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import inspect as sa_inspect

from api import models
from engine.generation import guide_schema
from engine.publish import gate
from engine.render import asset_sourcer, render_output
from engine.render.render_output import (
    EMPTY_PINS_FINGERPRINT,
    RENDER_ARTIFACT_COLUMNS,
    RenderOutput,
    assert_guide_not_stale,
    assert_pins_not_stale,
    compute_guide_fingerprint,
    compute_pins_fingerprint,
    compute_pins_fingerprint_for_render,
    record,
)
from tests import _seam_ast as S

_REPO = Path(__file__).resolve().parent.parent
_RENDER_OUTPUT_PATH = "engine/render/render_output.py"
assert Path(render_output.__file__).resolve().is_relative_to(_REPO), (
    f"engine.render.render_output was imported from {render_output.__file__}, outside {_REPO}: "
    "run `python -m pytest` from the repository root"
)

EIGHT = frozenset(RENDER_ARTIFACT_COLUMNS)

# The literal old messages of engine/publish/gate.py at 24cbc91 (the staleness checks moved without edit).
PINS_MESSAGE = (
    "Cannot publish — the rendered video no longer matches the currently pinned "
    "assets (a render after this video was built changed at least one beat's "
    "asset and then failed before completing). Re-render before publishing."
)
GUIDE_MESSAGE = (
    "Cannot publish — the rendered video no longer matches the current guide "
    "(the guide was edited after this video was built, and the re-render that "
    "should have caught up either failed or hasn't run yet). Re-render before "
    "publishing."
)

# Deliberately NOT in canonical key order, so a fingerprint built without sort_keys differs.
GUIDE = {
    "title": "Seeded guide",
    "niche": "football",
    "cuts": [{"platform": "youtube_shorts", "caption": "c", "hashtags": ["#b", "#a"], "beats": [
        {"index": 0, "vo_script": "Hello there.", "duration_s": 3.0},
        {"index": 1, "vo_script": "Second beat.", "duration_s": 4.0},
    ]}],
}

SENTINELS = {
    "video_path": "SENTINEL-video.mp4",
    "duration_s": 999.5,
    "thumbnail_path": "SENTINEL-thumb.jpg",
    "thumbnail_candidates": ["SENTINEL-cand-0.jpg", "SENTINEL-cand-1.jpg"],
    "black_frame_beat_indices": [98, 99],
    "subtitle_path": "SENTINEL.srt",
    "rendered_pins_fingerprint": "SENTINEL-pins-fingerprint",
    "rendered_guide_fingerprint": "SENTINEL-guide-fingerprint",
}

# Values for the other 15 columns that no plausible stray write would reproduce (attribute history reports
# "no change" when an assigned value equals the loaded one).
OTHER_SEED = {
    "target_length_s": 31.5,
    "caption": "SEED caption",
    "hashtags": ["#seed", "#only"],
    "hook_variants": ["seed hook one", "seed hook two"],
    "status": models.CutStatus.approved,
    "published_at": datetime(2001, 2, 3, 4, 5, 6),
    "platform_post_id": "seed-post-id",
    "views": 1234,
    "likes": 56,
    "comments": 7,
    "metrics_updated_at": datetime(2002, 3, 4, 5, 6, 7),
}

NOT_RENDER_OUTPUT = frozenset({
    "id", "reel_id", "platform", "target_length_s", "guide", "caption", "hashtags", "hook_variants", "status",
    "published_at", "platform_post_id", "views", "likes", "comments", "metrics_updated_at",
})

CAND_A = Path("/out/9/thumb_a.jpg")
CAND_B = Path("/out/9/thumb_b.jpg")
CAND_C = Path("/out/9/thumb_c.jpg")


def test_the_sentinel_ledger_covers_exactly_the_artifact_columns():
    assert set(SENTINELS) == EIGHT


def _output(**over):
    """A RenderOutput with TUPLE sequences (so list coercion is observable) and a non-integral duration."""
    base = dict(
        video_path=Path("/out/9/youtube_shorts.mp4"),
        thumbnail_candidates=(CAND_A, CAND_B, CAND_C),
        duration_s=17.25,
        black_frame_beat_indices=(1, 3),
        subtitle_path=Path("/out/9/captions.srt"),
    )
    base.update(over)
    return RenderOutput(**base)


@pytest.fixture
def world(db_session):
    """A real target Cut with ``cut.id != cut.reel_id``, a decoy before it and a look-alike whose id equals the
    target's reel_id (so an id mix-up reads another cut's pins), every artifact column holding a sentinel."""
    db = db_session
    reels = [models.Reel(context=f"ctx{i}", status=models.ReelStatus.guide_ready) for i in range(3)]
    db.add_all(reels)
    db.flush()

    def mk(reel, **extra):
        fields = {"status": models.CutStatus.in_review, **extra}
        cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, **fields)
        db.add(cut)
        db.flush()
        return cut

    decoy = mk(reels[0], guide={"decoy": True})
    cut = mk(reels[2], guide=GUIDE, **OTHER_SEED, **SENTINELS)
    lookalike = mk(reels[0], guide={"lookalike": True})
    assert cut.id != cut.reel_id
    assert lookalike.id == cut.reel_id
    return SimpleNamespace(db=db, cut=cut, decoy=decoy, lookalike=lookalike)


def _asset(db, ref):
    asset = models.Asset(type="photo", source="pexels", source_ref=ref, local_path=f"/tmp/{ref}.jpg",
                         license="pexels_free", safe_to_publish=True)
    db.add(asset)
    db.flush()
    return asset


def _pin(db, cut, asset, beat=0, order=0):
    db.add(models.CutAsset(cut_id=cut.id, asset_id=asset.id, beat_index=beat, order_in_beat=order))
    db.flush()


def _expected_pins_fp(rows):
    canonical = "|".join(f"{b}:{o}:{a}" for b, o, a in sorted(rows))
    return hashlib.sha256(canonical.encode()).hexdigest()[:64]


def _expected_guide_fp(guide):
    return hashlib.sha256(json.dumps(guide, sort_keys=True).encode()).hexdigest()[:64]


def _reload(db, cut_id):
    db.flush()
    db.commit()
    db.expire_all()
    return db.get(models.Cut, cut_id)


# ======================================================================================== record()

def test_record_writes_every_column_with_plain_json_types_before_any_flush(world):
    cut = world.cut
    record(world.db, cut, _output())

    assert cut.video_path == "/out/9/youtube_shorts.mp4" and type(cut.video_path) is str
    assert cut.thumbnail_path == "/out/9/thumb_a.jpg" and type(cut.thumbnail_path) is str
    assert type(cut.thumbnail_candidates) is list
    assert cut.thumbnail_candidates == ["/out/9/thumb_a.jpg", "/out/9/thumb_b.jpg", "/out/9/thumb_c.jpg"]
    assert all(type(p) is str for p in cut.thumbnail_candidates)
    assert cut.duration_s == 17.25
    assert type(cut.black_frame_beat_indices) is list
    assert cut.black_frame_beat_indices == [1, 3]
    assert cut.subtitle_path == "/out/9/captions.srt" and type(cut.subtitle_path) is str


def test_record_survives_a_flush_commit_expire_and_reload(world):
    """A leaked Path or tuple only fails at flush (JSON / String binding), so go all the way to the database."""
    record(world.db, world.cut, _output())
    cut = _reload(world.db, world.cut.id)

    assert cut.video_path == "/out/9/youtube_shorts.mp4"
    assert cut.thumbnail_path == "/out/9/thumb_a.jpg"
    assert cut.thumbnail_candidates == ["/out/9/thumb_a.jpg", "/out/9/thumb_b.jpg", "/out/9/thumb_c.jpg"]
    assert cut.duration_s == 17.25
    assert cut.black_frame_beat_indices == [1, 3]
    assert cut.subtitle_path == "/out/9/captions.srt"
    assert cut.rendered_pins_fingerprint == EMPTY_PINS_FINGERPRINT
    assert cut.rendered_guide_fingerprint == _expected_guide_fp(GUIDE)


def test_thumbnail_path_is_the_first_candidate_and_the_order_is_kept(world):
    record(world.db, world.cut, _output(thumbnail_candidates=(CAND_B, CAND_C, CAND_A)))
    assert world.cut.thumbnail_path == str(CAND_B)
    assert world.cut.thumbnail_candidates == [str(CAND_B), str(CAND_C), str(CAND_A)]


def test_a_single_candidate_is_both_the_thumbnail_and_the_list(world):
    record(world.db, world.cut, _output(thumbnail_candidates=(CAND_C,)))
    assert world.cut.thumbnail_path == str(CAND_C)
    assert world.cut.thumbnail_candidates == [str(CAND_C)]


def test_an_empty_black_frame_input_is_none_not_an_empty_list(world):
    record(world.db, world.cut, _output(black_frame_beat_indices=()))
    assert world.cut.black_frame_beat_indices is None


def test_a_missing_subtitle_is_none_not_the_string_none(world):
    record(world.db, world.cut, _output(subtitle_path=None))
    assert world.cut.subtitle_path is None


def test_a_second_record_replaces_every_column_wholesale(world):
    record(world.db, world.cut, _output())
    assert world.cut.black_frame_beat_indices == [1, 3] and world.cut.subtitle_path

    second = RenderOutput(
        video_path=Path("/out/10/second.mp4"),
        thumbnail_candidates=[Path("/out/10/z0.jpg")],
        duration_s=9.5,
        black_frame_beat_indices=[],
        subtitle_path=None,
    )
    record(world.db, world.cut, second)
    cut = world.cut
    assert cut.video_path == "/out/10/second.mp4"
    assert cut.thumbnail_path == "/out/10/z0.jpg"
    assert cut.thumbnail_candidates == ["/out/10/z0.jpg"]
    assert cut.duration_s == 9.5
    assert cut.black_frame_beat_indices is None        # the prior non-empty list is cleared
    assert cut.subtitle_path is None                   # the prior subtitle is cleared


def test_the_pins_fingerprint_is_the_empty_sentinel_when_no_pin_is_bound(world):
    record(world.db, world.cut, _output())
    assert world.cut.rendered_pins_fingerprint == EMPTY_PINS_FINGERPRINT
    assert EMPTY_PINS_FINGERPRINT == "no-pins-bound"


def test_the_pins_fingerprint_is_the_real_hash_of_this_cuts_own_pins(world):
    """The look-alike cut's id equals the target's reel_id and carries a different pin, so reading the pins of
    ``cut.reel_id`` (or of any other cut) gives a different fingerprint."""
    db, cut = world.db, world.cut
    a1, a2, a3 = _asset(db, "r1"), _asset(db, "r2"), _asset(db, "r3")
    _pin(db, cut, a2, beat=1, order=0)
    _pin(db, cut, a1, beat=0, order=0)
    _pin(db, world.lookalike, a3, beat=0, order=0)
    _pin(db, world.decoy, a3, beat=5, order=2)

    record(db, cut, _output())

    expected = _expected_pins_fp([(0, 0, a1.id), (1, 0, a2.id)])
    assert cut.rendered_pins_fingerprint == expected
    assert expected != compute_pins_fingerprint(db, world.lookalike.id)
    assert expected not in (EMPTY_PINS_FINGERPRINT, None)


def test_the_guide_fingerprint_equals_the_hash_of_the_cuts_guide(world):
    record(world.db, world.cut, _output())
    assert world.cut.rendered_guide_fingerprint == compute_guide_fingerprint(GUIDE)
    assert world.cut.rendered_guide_fingerprint == _expected_guide_fp(GUIDE)


def test_the_two_fingerprints_land_in_their_own_columns(world):
    db, cut = world.db, world.cut
    _pin(db, cut, _asset(db, "only"))
    record(db, cut, _output())
    assert cut.rendered_pins_fingerprint != cut.rendered_guide_fingerprint
    assert cut.rendered_pins_fingerprint == compute_pins_fingerprint_for_render(db, cut.id)
    assert cut.rendered_guide_fingerprint == compute_guide_fingerprint(cut.guide)


def test_a_cut_without_a_guide_gets_a_none_guide_fingerprint(world):
    world.cut.guide = None
    record(world.db, world.cut, _output())
    assert world.cut.rendered_guide_fingerprint is None


def _raise_in_pins(*a, **k):
    raise RuntimeError("pins read failed")


def _raise_in_guide(*a, **k):
    raise RuntimeError("guide hash failed")


@pytest.mark.parametrize("case", ["pins computation raises", "guide computation raises", "empty candidates"])
def test_record_is_atomic_in_memory(world, case):
    """If anything raises while computing, NO column has been touched (each keeps its non-None sentinel)."""
    cut = world.cut
    out = _output()
    with ExitStack() as stack:
        if case == "pins computation raises":
            stack.enter_context(patch("engine.render.render_output.compute_pins_fingerprint_for_render",
                                      side_effect=_raise_in_pins))
            stack.enter_context(pytest.raises(RuntimeError, match="pins read failed"))
        elif case == "guide computation raises":
            stack.enter_context(patch("engine.render.render_output.compute_guide_fingerprint",
                                      side_effect=_raise_in_guide))
            stack.enter_context(pytest.raises(RuntimeError, match="guide hash failed"))
        else:
            out = _output(thumbnail_candidates=())
            stack.enter_context(pytest.raises(IndexError))
        record(world.db, cut, out)

    for column, sentinel in SENTINELS.items():
        assert getattr(cut, column) == sentinel, f"{column} was touched although record() raised ({case})"
    assert _changed_columns(cut) == set(), "no attribute may even register a change"


def test_record_never_commits_flushes_refreshes_or_expires(world):
    db = world.db
    assert db.autoflush is False, "the spies below only prove 'no implicit flush' because autoflush is off"
    with ExitStack() as stack:
        spies = {name: stack.enter_context(patch.object(db, name))
                 for name in ("commit", "flush", "refresh", "expire", "expire_all", "rollback")}
        record(db, world.cut, _output())
    for name, spy in spies.items():
        assert not spy.called, f"record() called db.{name}()"
    assert world.cut in db.dirty, "the assignments are left pending for the caller's commit"


def test_render_output_is_frozen():
    out = _output()
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.duration_s = 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.video_path = Path("/elsewhere.mp4")
    assert dataclasses.is_dataclass(RenderOutput)
    assert {f.name for f in dataclasses.fields(RenderOutput)} == {
        "video_path", "thumbnail_candidates", "duration_s", "black_frame_beat_indices", "subtitle_path"}


# ================================================================================== staleness checks

def _recorded_world(world, pins=0):
    db = world.db
    for n in range(pins):
        _pin(db, world.cut, _asset(db, f"pin{n}"), beat=n)
    record(db, world.cut, _output())
    return world


def test_a_none_pins_fingerprint_skips_without_computing_even_with_live_pins(world):
    db, cut = world.db, world.cut
    _pin(db, cut, _asset(db, "live"))
    cut.rendered_pins_fingerprint = None
    with patch("engine.render.render_output.compute_pins_fingerprint_for_render") as compute:
        assert_pins_not_stale(db, cut)
    compute.assert_not_called()


def test_a_none_guide_fingerprint_skips_without_computing_even_with_a_live_guide(world):
    cut = world.cut
    cut.rendered_guide_fingerprint = None
    assert cut.guide
    with patch("engine.render.render_output.compute_guide_fingerprint") as compute:
        assert_guide_not_stale(world.db, cut)
    compute.assert_not_called()


def test_matching_fingerprints_do_not_raise(world):
    _recorded_world(world, pins=2)
    assert_pins_not_stale(world.db, world.cut)
    assert_guide_not_stale(world.db, world.cut)


def test_a_pins_mismatch_raises_the_literal_message_and_not_the_guide_one(world):
    _recorded_world(world, pins=1)
    _pin(world.db, world.cut, _asset(world.db, "extra"), beat=7)
    with pytest.raises(ValueError) as exc:
        assert_pins_not_stale(world.db, world.cut)
    assert str(exc.value) == PINS_MESSAGE
    assert "current guide" not in str(exc.value)


def test_a_guide_mismatch_raises_the_literal_message_and_not_the_pins_one(world):
    _recorded_world(world)
    world.cut.guide = {**GUIDE, "title": "edited after the render"}
    with pytest.raises(ValueError) as exc:
        assert_guide_not_stale(world.db, world.cut)
    assert str(exc.value) == GUIDE_MESSAGE
    assert "pinned" not in str(exc.value)


def test_the_literal_messages_are_the_old_gate_texts():
    """The two texts above are what engine/publish/gate.py raised at 24cbc91; they are distinct."""
    assert PINS_MESSAGE != GUIDE_MESSAGE
    assert PINS_MESSAGE.startswith("Cannot publish — ") and GUIDE_MESSAGE.startswith("Cannot publish — ")
    assert PINS_MESSAGE.endswith("Re-render before publishing.") and GUIDE_MESSAGE.endswith("Re-render before publishing.")


def test_a_black_frame_render_is_still_caught_after_a_partial_repin(world):
    """The all-black-frame render wrote EMPTY_PINS_FINGERPRINT (not None), so a later partial re-pin that failed
    before finishing is a mismatch, not a legacy-row skip."""
    _recorded_world(world, pins=0)
    assert world.cut.rendered_pins_fingerprint == EMPTY_PINS_FINGERPRINT
    _pin(world.db, world.cut, _asset(world.db, "late"))
    with pytest.raises(ValueError) as exc:
        assert_pins_not_stale(world.db, world.cut)
    assert str(exc.value) == PINS_MESSAGE


def test_a_patch_of_the_pins_compute_function_is_seen_by_the_check(world):
    cut = world.cut
    cut.rendered_pins_fingerprint = "stored"
    with patch("engine.render.render_output.compute_pins_fingerprint_for_render", return_value="other"):
        with pytest.raises(ValueError):
            assert_pins_not_stale(world.db, cut)
    with patch("engine.render.render_output.compute_pins_fingerprint_for_render", return_value="stored") as compute:
        assert_pins_not_stale(world.db, cut)
    compute.assert_called_once_with(world.db, cut.id)


def test_a_patch_of_the_guide_compute_function_is_seen_by_the_check(world):
    cut = world.cut
    cut.rendered_guide_fingerprint = "stored"
    with patch("engine.render.render_output.compute_guide_fingerprint", return_value="other"):
        with pytest.raises(ValueError):
            assert_guide_not_stale(world.db, cut)
    with patch("engine.render.render_output.compute_guide_fingerprint", return_value="stored") as compute:
        assert_guide_not_stale(world.db, cut)
    compute.assert_called_once_with(cut.guide)


def test_the_gate_pins_check_delegates_to_render_output(world):
    with (patch("engine.render.render_output.assert_pins_not_stale") as pins,
          patch("engine.render.render_output.assert_guide_not_stale") as guide):
        gate.assert_video_matches_pins(world.db, world.cut)
    pins.assert_called_once_with(world.db, world.cut)
    guide.assert_not_called()


def test_the_gate_guide_check_delegates_to_render_output(world):
    with (patch("engine.render.render_output.assert_pins_not_stale") as pins,
          patch("engine.render.render_output.assert_guide_not_stale") as guide):
        gate.assert_video_matches_guide(world.db, world.cut)
    guide.assert_called_once_with(world.db, world.cut)
    pins.assert_not_called()


def test_the_gate_delegates_propagate_the_real_errors(world):
    _recorded_world(world, pins=1)
    _pin(world.db, world.cut, _asset(world.db, "moved"), beat=4)
    world.cut.guide = {**GUIDE, "niche": "edited"}
    with pytest.raises(ValueError) as pins_exc:
        gate.assert_video_matches_pins(world.db, world.cut)
    with pytest.raises(ValueError) as guide_exc:
        gate.assert_video_matches_guide(world.db, world.cut)
    assert str(pins_exc.value) == PINS_MESSAGE
    assert str(guide_exc.value) == GUIDE_MESSAGE


# ---- writer-to-reader round trips with the REAL record() ---------------------------------------

def test_roundtrip_zero_pins_pass_then_a_new_pin_is_caught(world):
    record(world.db, world.cut, _output())
    assert_pins_not_stale(world.db, world.cut)
    _pin(world.db, world.cut, _asset(world.db, "appears-later"))
    with pytest.raises(ValueError):
        assert_pins_not_stale(world.db, world.cut)


def test_roundtrip_real_pins_pass_then_a_repin_is_caught(world):
    db, cut = world.db, world.cut
    first = _asset(db, "first")
    _pin(db, cut, first)
    record(db, cut, _output())
    assert_pins_not_stale(db, cut)

    row = db.query(models.CutAsset).filter_by(cut_id=cut.id, beat_index=0, order_in_beat=0).one()
    row.asset_id = _asset(db, "replacement").id
    db.flush()
    with pytest.raises(ValueError):
        assert_pins_not_stale(db, cut)


def test_roundtrip_the_guide_passes_then_an_edit_is_caught_while_the_pins_still_pass(world):
    _recorded_world(world, pins=1)
    assert_guide_not_stale(world.db, world.cut)

    world.cut.guide = {**GUIDE, "title": "operator edit"}
    with pytest.raises(ValueError):
        assert_guide_not_stale(world.db, world.cut)
    assert_pins_not_stale(world.db, world.cut)          # a guide edit does not trip the pins check


def test_roundtrip_survives_a_commit_and_reload(world):
    _recorded_world(world, pins=1)
    cut = _reload(world.db, world.cut.id)
    assert_pins_not_stale(world.db, cut)
    assert_guide_not_stale(world.db, cut)


# ==================================================================================== G-A column ledger

def _ledger_problems(actual, render, other):
    problems = []
    if render & other:
        problems.append(f"classified twice: {sorted(render & other)}")
    if actual - (render | other):
        problems.append(f"unclassified Cut column(s): {sorted(actual - (render | other))}")
    if (render | other) - actual:
        problems.append(f"classified but not a Cut column: {sorted((render | other) - actual)}")
    return problems


def test_G_A_every_cut_column_is_classified_exactly_once():
    actual = {c.name for c in models.Cut.__table__.columns}
    assert len(NOT_RENDER_OUTPUT) == 15
    assert len(RENDER_ARTIFACT_COLUMNS) == len(set(RENDER_ARTIFACT_COLUMNS)) == 8
    assert _ledger_problems(actual, set(RENDER_ARTIFACT_COLUMNS), set(NOT_RENDER_OUTPUT)) == []
    # the mapped attribute keys are the column names, so attribute history in G-B speaks about the same set
    assert {a.key for a in sa_inspect(models.Cut).column_attrs} == actual


def test_G_A_control_a_new_unclassified_column_fails():
    actual = {c.name for c in models.Cut.__table__.columns} | {"brand_new_column"}
    problems = _ledger_problems(actual, set(RENDER_ARTIFACT_COLUMNS), set(NOT_RENDER_OUTPUT))
    assert problems == ["unclassified Cut column(s): ['brand_new_column']"]


def test_G_A_control_a_column_in_both_sets_or_in_neither_world_fails():
    actual = {c.name for c in models.Cut.__table__.columns}
    both = _ledger_problems(actual, set(RENDER_ARTIFACT_COLUMNS), set(NOT_RENDER_OUTPUT) | {"video_path"})
    assert any(p.startswith("classified twice") for p in both)
    stale = _ledger_problems(actual, set(RENDER_ARTIFACT_COLUMNS) | {"ghost"}, set(NOT_RENDER_OUTPUT))
    assert stale == ["classified but not a Cut column: ['ghost']"]
    dropped = _ledger_problems(actual, set(RENDER_ARTIFACT_COLUMNS) - {"subtitle_path"}, set(NOT_RENDER_OUTPUT))
    assert dropped == ["unclassified Cut column(s): ['subtitle_path']"]


# ================================================================================== G-B exact write set

def _changed_columns(cut):
    state = sa_inspect(cut)
    return {attr.key for attr in sa_inspect(models.Cut).column_attrs if state.attrs[attr.key].history.has_changes()}


def test_G_B_record_changes_exactly_the_eight_columns_and_no_other(world):
    """The Cut is flushed but never refreshed and every column holds a value that differs from anything record()
    writes (history reports "no change" for an equal value), so a dropped or a stray assignment shows."""
    cut = world.cut
    assert _changed_columns(cut) == set(), "precondition: the seeded cut is clean"
    assert cut.rendered_pins_fingerprint and cut.rendered_guide_fingerprint, "non-None fingerprint sentinels"
    assert cut.guide and cut.hashtags and cut.hook_variants

    record(world.db, cut, _output())

    assert _changed_columns(cut) == set(RENDER_ARTIFACT_COLUMNS)


def test_G_B_control_a_stray_extra_write_is_reported(world):
    cut = world.cut

    def record_and_touch_caption(db, c, out):
        record(db, c, out)
        c.caption = "stray write"

    record_and_touch_caption(world.db, cut, _output())
    assert _changed_columns(cut) == set(RENDER_ARTIFACT_COLUMNS) | {"caption"}


def test_G_B_control_a_dropped_assignment_is_reported(world):
    cut = world.cut
    record(world.db, cut, _output())
    # revert one column to its loaded value the way a dropped assignment would leave it
    cut.subtitle_path = SENTINELS["subtitle_path"]
    assert _changed_columns(cut) == set(RENDER_ARTIFACT_COLUMNS) - {"subtitle_path"}


def test_G_B_the_other_columns_keep_their_values(world):
    cut = world.cut
    record(world.db, cut, _output())
    for column, value in OTHER_SEED.items():
        assert getattr(cut, column) == value
    assert cut.guide == GUIDE


# ================================================================================== G-C no stray writer

_SEVEN = EIGHT - {"duration_s"}
_KW_ATTR_CALLEES = {"Cut", "values", "update", "insert"}
_DICT_ARG_CALLEES = {"update", "values"}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclasses.dataclass(frozen=True)
class Hit:
    path: str
    line: int
    func: str | None
    name: str
    form: str


def _enclosing(node):
    parent = getattr(node, "_parent", None)
    while parent is not None:
        if isinstance(parent, _FUNCS):
            return parent.name
        parent = getattr(parent, "_parent", None)
    return None


def _str_const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_dunder_dict_receiver(node):
    return (isinstance(node, ast.Attribute) and node.attr == "__dict__") or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars")


def stray_hits(source, path):
    """Every write to one of the eight names in `source`, by the exact rules of spec 4.5 (before any allowlist)."""
    tree = S.parse(source, path)
    hits = []

    def add(node, name, form):
        hits.append(Hit(path, node.lineno, _enclosing(node), name, form))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in EIGHT and isinstance(node.ctx, (ast.Store, ast.Del)):
                parent = node._parent
                if isinstance(parent, ast.AnnAssign) and parent.value is None and node._field == "target":
                    continue
                add(node, node.attr, "attr")
        elif isinstance(node, ast.Subscript):
            key = _str_const(node.slice)
            if key in EIGHT and isinstance(node.ctx, (ast.Store, ast.Del)):
                if key != "duration_s" or _is_dunder_dict_receiver(node.value):
                    add(node, key, "subscript")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2:
                if _str_const(node.args[1]) in EIGHT:
                    add(node, _str_const(node.args[1]), "setattr")
            callee = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            kw_callee = (isinstance(func, ast.Name) and func.id in {"Cut", "insert"}) or (
                isinstance(func, ast.Attribute) and func.attr in _KW_ATTR_CALLEES)
            if kw_callee:
                for kw in node.keywords:
                    if kw.arg in EIGHT:
                        add(node, kw.arg, "keyword")
            if isinstance(func, ast.Attribute) and callee in _DICT_ARG_CALLEES:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Dict):
                        for k in arg.keys:
                            if _str_const(k) in EIGHT:
                                add(arg, _str_const(k), "dict")
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                if (isinstance(k, ast.Attribute) and k.attr in EIGHT and isinstance(k.value, ast.Attribute)
                        and k.value.attr == "Cut"):
                    add(node, k.attr, "dict")
    return hits


ALLOWED = frozenset({
    ("api/routers/cuts.py", "choose_thumbnail", "thumbnail_path", "attr"),
    ("engine/generation/beat_enrichment.py", "_enrich_with_insight", "duration_s", "attr"),
    ("worker/tasks/generate.py", "_enrich_standard_path_guide", "duration_s", "attr"),
})


def violations(hits):
    """The hits that neither belong to record()'s own body nor to the three allowlisted writers."""
    return [h for h in hits
            if not (h.path == _RENDER_OUTPUT_PATH and h.func == "record" and h.form == "attr")
            and (h.path, h.func, h.name, h.form) not in ALLOWED]


def record_store_problems(hits):
    """In render_output.py: record() holds exactly eight attribute stores (one per name) and nothing else stores."""
    problems = []
    in_record = [h for h in hits if h.func == "record"]
    if sorted(h.name for h in in_record if h.form == "attr") != sorted(EIGHT) or len(in_record) != 8:
        problems.append(f"record() must hold exactly eight attribute stores, found {sorted((h.form, h.name) for h in in_record)}")
    elsewhere = [h for h in hits if h.func != "record"]
    if elsewhere:
        problems.append(f"stores outside record(): {sorted((h.func, h.name) for h in elsewhere)}")
    return problems


def production_files():
    files = {}
    for top in ("api", "engine", "worker"):
        for p in sorted((_REPO / top).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            files[p.relative_to(_REPO).as_posix()] = p.read_text()
    return files


@pytest.fixture(scope="module")
def tree_hits():
    return {path: stray_hits(src, path) for path, src in production_files().items()}


def test_G_C_the_real_tree_has_no_stray_writer(tree_hits):
    found = [h for hits in tree_hits.values() for h in violations(hits)]
    assert found == [], "\n".join(f"{h.path}:{h.line} {h.form} write of {h.name} in {h.func}()" for h in found)


def test_G_C_non_vacuity_the_real_tree_has_exactly_the_three_allowed_writers_outside_render_output(tree_hits):
    outside = sorted((h.path, h.func, h.name, h.form)
                     for path, hits in tree_hits.items() if path != _RENDER_OUTPUT_PATH for h in hits)
    assert outside == sorted(ALLOWED)
    assert tree_hits["worker/tasks/render.py"] == [], "render_cut no longer assigns any of the eight columns"
    assert len(tree_hits) > 50, "the walk really covers api/, engine/ and worker/"


def test_G_C_render_output_stores_the_eight_columns_in_record_only(tree_hits):
    assert record_store_problems(tree_hits[_RENDER_OUTPUT_PATH]) == []


def _fires(snippet, expect, path="engine/example.py"):
    got = [(h.name, h.form) for h in stray_hits(snippet, path)]
    assert got == expect, got


def _quiet(snippet, path="engine/example.py"):
    assert stray_hits(snippet, path) == []


@pytest.mark.parametrize("name", sorted(EIGHT))
def test_G_C_control_an_attribute_assignment_fires_for_every_name(name):
    _fires(f"def f(cut):\n    cut.{name} = 1\n", [(name, "attr")])


def test_G_C_control_every_attribute_store_form_fires():
    _fires("def f(cut):\n    cut.video_path += 'x'\n", [("video_path", "attr")])
    _fires("def f(cut):\n    cut.video_path: str = 'x'\n", [("video_path", "attr")])
    _fires("def f(cut, a, b):\n    cut.video_path, cut.duration_s = a, b\n",
           [("video_path", "attr"), ("duration_s", "attr")])
    _fires("def f(cut, a):\n    [cut.subtitle_path, *cut.thumbnail_candidates] = a\n",
           [("subtitle_path", "attr"), ("thumbnail_candidates", "attr")])
    _fires("def f(cut, xs):\n    for cut.thumbnail_path in xs:\n        pass\n", [("thumbnail_path", "attr")])
    _fires("def f(cut, m):\n    with m() as cut.black_frame_beat_indices:\n        pass\n",
           [("black_frame_beat_indices", "attr")])
    _fires("def f(cut):\n    del cut.rendered_pins_fingerprint\n", [("rendered_pins_fingerprint", "attr")])
    _fires("async def f(cut):\n    cut.rendered_guide_fingerprint = None\n", [("rendered_guide_fingerprint", "attr")])


def test_G_C_control_string_literal_subscript_stores_fire_for_the_seven_names():
    for name in sorted(_SEVEN):
        _fires(f"def f(d):\n    d[{name!r}] = 1\n", [(name, "subscript")])
    _fires("def f(d):\n    d['video_path'] += 'x'\n", [("video_path", "subscript")])


def test_G_C_control_a_duration_s_subscript_fires_only_on_a_dunder_dict_or_vars_receiver():
    _fires("def f(cut):\n    cut.__dict__['duration_s'] = 1\n", [("duration_s", "subscript")])
    _fires("def f(cut):\n    vars(cut)['duration_s'] = 1\n", [("duration_s", "subscript")])
    _quiet("def f(d):\n    d['duration_s'] = 1\n")
    _quiet("def f(ev, v):\n    ev.detail['duration_s'] = v\n")
    _quiet("def f(beat):\n    beat['duration_s'] = 2.0\n")


def test_G_C_control_setattr_with_a_literal_name_fires():
    _fires("def f(cut):\n    setattr(cut, 'video_path', 'x')\n", [("video_path", "setattr")])
    _quiet("def f(cut, n):\n    setattr(cut, n, 'x')\n")                      # a known gap (non-literal name)
    _quiet("def f(cut):\n    setattr(cut, 'caption', 'x')\n")


def test_G_C_control_keyword_arguments_fire_only_for_Cut_values_update_and_insert():
    _fires("def f():\n    return Cut(video_path='x', reel_id=1)\n", [("video_path", "keyword")])
    _fires("def f():\n    return models.Cut(duration_s=1.0)\n", [("duration_s", "keyword")])
    _fires("def f(q):\n    q.update(subtitle_path=None)\n", [("subtitle_path", "keyword")])
    _fires("def f(q):\n    q.values(thumbnail_path='x')\n", [("thumbnail_path", "keyword")])
    _fires("def f():\n    insert(Cut).values(video_path='x')\n", [("video_path", "keyword")])
    _fires("def f():\n    insert(video_path='x')\n", [("video_path", "keyword")])


def test_G_C_control_dict_literals_fire_only_as_update_values_arguments_or_Cut_keys():
    _fires("def f(q):\n    q.update({'video_path': 'x'})\n", [("video_path", "dict")])
    _fires("def f(q):\n    q.values({'duration_s': 1.0})\n", [("duration_s", "dict")])
    _fires("def f(q):\n    q.update({models.Cut.thumbnail_path: 'x'})\n", [("thumbnail_path", "dict")])
    _fires("def f():\n    return {models.Cut.rendered_guide_fingerprint: 'x'}\n", [("rendered_guide_fingerprint", "dict")])
    _quiet("def f(s):\n    return {'duration_s': s.duration_s}\n")
    _quiet("def f(s):\n    return {'video_path': s.video_path}\n")
    _quiet("def f(d):\n    d.setdefault({'video_path': 1})\n")


def test_G_C_control_the_documented_exclusions_do_not_fire():
    _quiet("def f():\n    composite_cut(thumbnail_path=t, output_path=o)\n")
    _quiet("def f():\n    return composite_cut(video_path=p)\n")
    _quiet("def f():\n    duration, thumbnail_candidates, subtitle_path = composite_cut()\n")   # bare-name stores
    _quiet("def f():\n    video_path = 'x'\n    duration_s = 1\n")
    _quiet("def f():\n    return SourcedAsset(duration_s=1.0)\n")
    _quiet("def f():\n    return Beat(duration_s=2.0)\n")
    _quiet("def f():\n    return RenderOutput(video_path=p, duration_s=1.0, subtitle_path=None)\n")
    _quiet("def f(cut):\n    return cut.video_path, cut.duration_s\n")                       # loads are not writes
    _quiet("def f(cut):\n    cut.video_path: str\n")                                           # annotation only
    _quiet("def f(cut):\n    cut.caption = 'x'\n    cut.status = 'y'\n")                      # other columns


def test_G_C_the_allowlist_is_keyed_by_file_and_function_and_name():
    ok = "async def choose_thumbnail(cut):\n    cut.thumbnail_path = cut.thumbnail_candidates[0]\n"
    assert violations(stray_hits(ok, "api/routers/cuts.py")) == []
    assert len(violations(stray_hits(ok, "api/routers/other.py"))) == 1
    wrong_fn = ok.replace("choose_thumbnail", "other_fn")
    assert len(violations(stray_hits(wrong_fn, "api/routers/cuts.py"))) == 1
    wrong_name = "async def choose_thumbnail(cut):\n    cut.video_path = 'x'\n"
    assert len(violations(stray_hits(wrong_name, "api/routers/cuts.py"))) == 1
    wrong_form = "async def choose_thumbnail(cut):\n    setattr(cut, 'thumbnail_path', 'x')\n"
    assert len(violations(stray_hits(wrong_form, "api/routers/cuts.py"))) == 1

    dur = "def _enrich_with_insight(stub):\n    stub.duration_s = 3.0\n"
    assert violations(stray_hits(dur, "engine/generation/beat_enrichment.py")) == []
    assert len(violations(stray_hits(dur, "engine/generation/other.py"))) == 1
    dur2 = "def _enrich_standard_path_guide(beat):\n    beat.duration_s = 3.0\n"
    assert violations(stray_hits(dur2, "worker/tasks/generate.py")) == []
    assert len(violations(stray_hits(dur2, "worker/tasks/render.py"))) == 1


def test_G_C_control_a_stray_writer_in_the_places_that_matter_fires():
    # render.py writing a column again, cuts.py writing a second column, a Cut(...) constructor anywhere
    assert len(violations(stray_hits("def render_cut(cut):\n    cut.video_path = 'x'\n", "worker/tasks/render.py"))) == 1
    assert len(violations(stray_hits("async def choose_thumbnail(cut):\n    cut.video_path = 'x'\n",
                                     "api/routers/cuts.py"))) == 1
    assert len(violations(stray_hits("def mk(db):\n    db.add(models.Cut(video_path='x'))\n", "api/routers/reels.py"))) == 1


def test_G_C_record_exemption_covers_record_only_and_requires_exactly_eight_stores():
    eight = "def record(db, cut, out):\n" + "".join(f"    cut.{n} = 1\n" for n in sorted(EIGHT))
    hits = stray_hits(eight, _RENDER_OUTPUT_PATH)
    assert violations(hits) == [] and record_store_problems(hits) == []

    seven = "def record(db, cut, out):\n" + "".join(f"    cut.{n} = 1\n" for n in sorted(EIGHT)[1:])
    assert record_store_problems(stray_hits(seven, _RENDER_OUTPUT_PATH))

    nine = eight + "    cut.video_path = 2\n"
    assert record_store_problems(stray_hits(nine, _RENDER_OUTPUT_PATH))

    other_fn = eight + "def assert_pins_not_stale(db, cut):\n    cut.rendered_pins_fingerprint = None\n"
    hits = stray_hits(other_fn, _RENDER_OUTPUT_PATH)
    assert len(violations(hits)) == 1 and violations(hits)[0].func == "assert_pins_not_stale"
    assert record_store_problems(hits)

    # the exemption is for render_output.py only
    assert len(violations(stray_hits(eight, "engine/render/other.py"))) == 8
    # and only for attribute stores: a record() that used setattr would not be exempt
    sneaky = "def record(db, cut, out):\n    setattr(cut, 'video_path', 'x')\n"
    assert len(violations(stray_hits(sneaky, _RENDER_OUTPUT_PATH))) == 1


# ======================================================================================== G-D module seam

_FORBIDDEN = ("engine.generation", "engine.publish", "engine.render.asset_sourcer", "worker")


def test_G_D_render_output_imports_none_of_the_forbidden_modules():
    source = (_REPO / _RENDER_OUTPUT_PATH).read_text()
    assert S.g5_import_problems(source, _RENDER_OUTPUT_PATH, _FORBIDDEN) == []


@pytest.mark.parametrize("snippet", [
    "from engine.generation.guide_schema import compute_guide_fingerprint\n",
    "from engine.generation import guide_schema\n",
    "import engine.publish.gate\n",
    "from engine.render import asset_sourcer\n",
    "from engine.render.asset_sourcer import _fp\n",
    "import worker.tasks.render\n",
    "from worker.tasks import common\n",
    "from ..generation import guide_schema\n",
    "def f():\n    from engine.publish import gate\n",
])
def test_G_D_control_each_forbidden_import_form_fires(snippet):
    problems = S.g5_import_problems(snippet, _RENDER_OUTPUT_PATH, _FORBIDDEN)
    assert problems and all(p.rule == "G5" for p in problems)


@pytest.mark.parametrize("snippet", [
    "from api import models\n",
    "import hashlib\nimport json\n",
    "from engine.render import safe_fetch\n",
    "from engine.generation_other import x\n",
])
def test_G_D_control_allowed_imports_do_not_fire(snippet):
    assert S.g5_import_problems(snippet, _RENDER_OUTPUT_PATH, _FORBIDDEN) == []


def test_G_D_the_moved_names_are_gone_from_their_old_homes():
    for name in ("compute_pins_fingerprint", "compute_pins_fingerprint_for_render", "EMPTY_PINS_FINGERPRINT"):
        assert not hasattr(asset_sourcer, name), f"asset_sourcer still exposes {name}"
        assert hasattr(render_output, name)
    assert not hasattr(guide_schema, "compute_guide_fingerprint")
    assert hasattr(render_output, "compute_guide_fingerprint")


def test_G_D_the_gate_keeps_its_two_public_names():
    assert callable(gate.assert_video_matches_pins) and callable(gate.assert_video_matches_guide)
