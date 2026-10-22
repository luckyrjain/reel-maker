"""Tests for engine/analytics/correlation.py."""
import itertools
import math
from types import SimpleNamespace

import pytest

from api import models
from engine.analytics.correlation import (
    MIN_SAMPLE,
    _hook_vo_for_max_views_cut,
    load_reel_stats,
    quality_engagement_correlation,
    top_bottom_performers,
)
from engine.generation.guide_schema import Beat, PlatformGuide


def _reel(db, niche="football", context="x"):
    reel = models.Reel(context=context, niche=niche, status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    return reel


def _quality_job(db, reel_id, score):
    job = models.Job(
        type=models.JobType.generate,
        reel_id=reel_id,
        status=models.JobStatus.done,
        meta={"quality_score": score},
    )
    db.add(job)
    db.flush()
    return job


def _guide_dict(hook_vo="Could this be the greatest comeback ever?"):
    guide = PlatformGuide(
        platform="youtube_shorts",
        target_length_s=30.0,
        caption="caption",
        hashtags=["tag"] * 6,
        beats=[
            Beat(index=0, type="hook", duration_s=3, visual_direction="v", on_screen_text=[], vo_script=hook_vo),
            Beat(index=1, type="body", duration_s=8, visual_direction="v", on_screen_text=[], vo_script="body"),
            Beat(index=2, type="cta", duration_s=5, visual_direction="v", on_screen_text=[], vo_script="cta"),
        ],
    )
    return guide.model_dump()


def _cut(db, reel_id, views=None, guide=None):
    cut = models.Cut(
        reel_id=reel_id,
        platform=models.CutPlatform.youtube_shorts,
        status=models.CutStatus.published,
        views=views,
        guide=guide,
    )
    db.add(cut)
    db.flush()
    return cut


def _dataset(db, qualities, views):
    """Create len(qualities) reels, each with one quality_score job and one cut
    whose views is the matching entry in `views`."""
    for q, v in zip(qualities, views):
        reel = _reel(db)
        _quality_job(db, reel.id, q)
        _cut(db, reel.id, views=v)


# ── quality_engagement_correlation ──────────────────────────────────────────

def test_below_min_sample_returns_none_with_sample_size(db_session):
    db = db_session
    assert MIN_SAMPLE > 3, "test fixture assumes 3 reels is below MIN_SAMPLE"
    _dataset(db, [10, 20, 30], [100, 200, 300])
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.r is None
    assert result.sample_size == 3
    assert result.insufficient_variance is False


def test_zero_variance_quality_scores_returns_insufficient_variance(db_session):
    db = db_session
    _dataset(db, [50, 50, 50, 50, 50], [100, 200, 300, 400, 500])
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.r is None
    assert result.sample_size == 5
    assert result.insufficient_variance is True


def test_zero_variance_views_returns_insufficient_variance(db_session):
    db = db_session
    _dataset(db, [10, 20, 30, 40, 50], [100, 100, 100, 100, 100])
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.r is None
    assert result.sample_size == 5
    assert result.insufficient_variance is True


def test_known_synthetic_dataset_matches_hand_computed_r(db_session):
    """x=[1,2,3,4,5], y=[2,4,5,4,5]. Hand-computed (not via numpy):
    mean_x=3, mean_y=4; dx=[-2,-1,0,1,2], dy=[-2,0,1,0,1];
    sum(dx*dy)=4+0+0+0+2=6; sum(dx^2)=10; sum(dy^2)=6;
    r = 6 / sqrt(10*6) = sqrt(0.6) = 0.7745966692414834."""
    db = db_session
    _dataset(db, [1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.sample_size == 5
    assert result.insufficient_variance is False
    assert abs(result.r - math.sqrt(0.6)) < 1e-9


def test_reel_with_quality_but_no_views_excluded(db_session):
    db = db_session
    _dataset(db, [1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    # Extra reel: has a quality score, no cut with views at all.
    extra = _reel(db)
    _quality_job(db, extra.id, 99)
    _cut(db, extra.id, views=None)
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.sample_size == 5  # extra reel excluded, not counted as a 6th pair


def test_reel_with_views_but_no_quality_excluded(db_session):
    db = db_session
    _dataset(db, [1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    # Extra reel: has views, no generate job with a quality_score at all.
    extra = _reel(db)
    _cut(db, extra.id, views=1000)
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.sample_size == 5


def test_multiple_cuts_per_reel_uses_max_views_cut(db_session):
    """Reel #4 (quality=4) gets two cuts: views=4 (matches the hand-computed dataset)
    and a lower-views second cut (views=1). If the max-views cut weren't used (e.g. if
    views were summed instead), the aggregate would be 5, not 4, and the result would
    diverge from the independently hand-computed r for this dataset."""
    db = db_session
    qualities = [1, 2, 3, 4, 5]
    views = [2, 4, 5, 4, 5]
    for i, (q, v) in enumerate(zip(qualities, views)):
        reel = _reel(db)
        _quality_job(db, reel.id, q)
        _cut(db, reel.id, views=v)
        if q == 4:
            _cut(db, reel.id, views=1)  # a second, lower-views cut on the same reel
    db.commit()

    result = quality_engagement_correlation(db)
    assert result.sample_size == 5
    assert abs(result.r - math.sqrt(0.6)) < 1e-9


# ── top_bottom_performers ────────────────────────────────────────────────────

def test_n_less_than_6_returns_combined_list_not_two(db_session):
    db = db_session
    _dataset(db, [10, 20, 30, 40, 50], [100, 200, 300, 400, 500])
    db.commit()

    result = top_bottom_performers(db, k=3)
    assert isinstance(result, list)
    assert len(result) == 5
    assert [p.views for p in result] == [500, 400, 300, 200, 100]


def test_n_at_least_2k_returns_top_and_bottom_tuple_no_overlap(db_session):
    db = db_session
    _dataset(db, list(range(1, 7)), [10, 20, 30, 40, 50, 60])
    db.commit()

    result = top_bottom_performers(db, k=3)
    assert isinstance(result, tuple)
    top, bottom = result
    assert [p.views for p in top] == [60, 50, 40]
    assert [p.views for p in bottom] == [10, 20, 30]  # ascending — worst first
    top_ids = {p.reel_id for p in top}
    bottom_ids = {p.reel_id for p in bottom}
    assert not (top_ids & bottom_ids)


def test_ties_in_views_do_not_crash_and_preserve_count(db_session):
    db = db_session
    _dataset(db, [1, 2, 3, 4, 5, 6], [50, 50, 50, 50, 50, 50])
    db.commit()

    result = top_bottom_performers(db, k=3)
    assert isinstance(result, tuple)
    top, bottom = result
    assert len(top) == 3
    assert len(bottom) == 3


def test_reel_whose_max_views_cut_has_no_guide_defends_against_crash(db_session):
    db = db_session
    _dataset(db, [1, 2, 3, 4], [10, 20, 30, 40])
    # Fifth reel: has a cut with views but no guide at all (defensive — shouldn't
    # happen post-render, but the hook-text lookup must not crash).
    reel = _reel(db)
    _quality_job(db, reel.id, 5)
    _cut(db, reel.id, views=50, guide=None)
    db.commit()

    result = top_bottom_performers(db, k=3)
    # n=5 < 2*3=6 → combined list
    assert isinstance(result, list)
    no_guide_performer = next(p for p in result if p.views == 50)
    assert no_guide_performer.hook_vo is None


def test_hook_vo_extracted_from_max_views_cut_guide(db_session):
    db = db_session
    _dataset(db, [1, 2, 3], [10, 20, 30])
    reel = _reel(db)
    _quality_job(db, reel.id, 40)
    _cut(db, reel.id, views=100, guide=_guide_dict(hook_vo="Nobody talks about this weakness."))
    db.commit()

    result = top_bottom_performers(db, k=3)
    assert isinstance(result, list)
    top_performer = next(p for p in result if p.views == 100)
    assert top_performer.hook_vo == "Nobody talks about this weakness."


# ── load_reel_stats / sharing one load between the two analyses ──────────────

def test_load_reel_stats_keeps_only_reels_with_both_quality_and_views(db_session):
    db = db_session
    both = _reel(db)
    _quality_job(db, both.id, 70)
    _cut(db, both.id, views=10)
    _cut(db, both.id, views=400)          # max across cuts wins
    no_views = _reel(db)
    _quality_job(db, no_views.id, 80)
    _cut(db, no_views.id, views=None)
    no_quality = _reel(db)
    _cut(db, no_quality.id, views=999)
    db.commit()

    stats = load_reel_stats(db)
    assert [(s.reel.id, s.quality, s.views) for s in stats] == [(both.id, 70, 400)]


def test_both_analyses_given_the_same_stats_match_their_self_loading_results(db_session):
    db = db_session
    _dataset(db, [10, 20, 30, 40, 50, 60, 70], [2, 4, 5, 4, 5, 9, 12])
    db.commit()

    stats = load_reel_stats(db)
    assert quality_engagement_correlation(db, stats) == quality_engagement_correlation(db)
    assert top_bottom_performers(db, stats=stats) == top_bottom_performers(db)


def test_analyses_given_stats_issue_no_further_queries(db_session):
    from sqlalchemy import event

    db = db_session
    _dataset(db, [10, 20, 30, 40, 50, 60, 70], [2, 4, 5, 4, 5, 9, 12])
    db.commit()
    stats = load_reel_stats(db)

    seen = []

    def record(conn, cursor, statement, *rest):
        seen.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", record)
    try:
        quality_engagement_correlation(db, stats)
        top_bottom_performers(db, stats=stats)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", record)
    assert [q for q in seen if q.lstrip().upper().startswith("SELECT")] == []


def test_empty_stats_are_respected_not_reloaded(db_session):
    """`stats=[]` means "no qualifying reels", not "load them yourself" -- an `if not stats`
    check would silently reload on an empty page."""
    db = db_session
    _dataset(db, [10, 20, 30, 40, 50], [1, 2, 3, 4, 5])
    db.commit()

    assert quality_engagement_correlation(db, []).sample_size == 0
    assert top_bottom_performers(db, stats=[]) == []


# ── behaviour the shared load must keep (previously unpinned) ────────────────

def test_k_sets_the_size_of_both_lists(db_session):
    db = db_session
    _dataset(db, [10, 20, 30, 40, 50], [1, 2, 3, 4, 5])
    db.commit()

    top, bottom = top_bottom_performers(db, k=2)
    assert [p.views for p in top] == [5, 4]
    assert [p.views for p in bottom] == [1, 2]
    # 5 reels < 2*3: with the default k the same data collapses to one combined list
    assert isinstance(top_bottom_performers(db), list)


def test_latest_quality_score_wins_by_job_creation_time(db_session):
    from datetime import datetime, timedelta, timezone

    db = db_session
    reel = _reel(db)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # inserted newest-first so only the ORDER BY (not insertion order) can pick the latest
    for score, age in ((90, 2), (40, 0)):
        job = _quality_job(db, reel.id, score)
        job.created_at = t0 + timedelta(hours=age)
    _cut(db, reel.id, views=10)
    db.commit()

    [stat] = load_reel_stats(db)
    assert stat.quality == 90


def test_hook_comes_from_the_max_views_cut_not_the_first_cut(db_session):
    db = db_session
    reel = _reel(db, niche="finance")
    _quality_job(db, reel.id, 70)
    # the max-views cut sits in the MIDDLE: reel.cuts has no guaranteed order (SQLite returns it
    # newest-first), so only a middle maximum is neither first nor last in either direction
    _cut(db, reel.id, views=5, guide=_guide_dict("Low-view cut hook?"))
    _cut(db, reel.id, views=500, guide=_guide_dict("High-view cut hook?"))
    _cut(db, reel.id, views=6, guide=_guide_dict("Other low-view cut hook?"))
    db.commit()

    [p] = top_bottom_performers(db)
    assert p.hook_vo == "High-view cut hook?"
    assert (p.niche, p.quality_score, p.views) == ("finance", 70, 500)


@pytest.mark.parametrize("order", list(itertools.permutations([0, 1, 2])))
def test_hook_is_the_max_views_cuts_whatever_order_the_cuts_come_in(order):
    """reel.cuts has no guaranteed order (SQLite returns it in an arbitrary one), so the
    max-views cut must be found at any position: first, middle or last."""
    cuts = [
        SimpleNamespace(views=5, guide=_guide_dict("Low-view hook?")),
        SimpleNamespace(views=500, guide=_guide_dict("High-view hook?")),
        SimpleNamespace(views=6, guide=_guide_dict("Other low-view hook?")),
    ]
    reel = SimpleNamespace(cuts=[cuts[i] for i in order])
    assert _hook_vo_for_max_views_cut(reel) == "High-view hook?"


# ── zero values are data, not "missing" (round-2 review) ─────────────────────

def test_a_reel_with_zero_views_is_kept(db_session):
    db = db_session
    reel = _reel(db)
    _quality_job(db, reel.id, 50)
    _cut(db, reel.id, views=0)
    db.commit()

    [stat] = load_reel_stats(db)
    assert (stat.quality, stat.views) == (50, 0)


def test_a_reel_with_zero_quality_is_kept(db_session):
    db = db_session
    reel = _reel(db)
    _quality_job(db, reel.id, 0)
    _cut(db, reel.id, views=7)
    db.commit()

    [stat] = load_reel_stats(db)
    assert (stat.quality, stat.views) == (0, 7)


def test_zero_views_cut_still_counts_when_picking_the_max_views_cut():
    """views=0 is a measured value: a reel whose only measured cut has 0 views still has a hook."""
    reel = SimpleNamespace(cuts=[SimpleNamespace(views=0, guide=_guide_dict("Zero-view hook?"))])
    assert _hook_vo_for_max_views_cut(reel) == "Zero-view hook?"


# ── ranking and sign ─────────────────────────────────────────────────────────

def test_performers_are_ranked_by_views_not_by_quality(db_session):
    db = db_session
    # quality FALLS as views RISE, so a sort on the wrong key gives the opposite order
    _dataset(db, [60, 50, 40, 30, 20, 10], [10, 20, 30, 40, 50, 60])
    db.commit()

    top, bottom = top_bottom_performers(db)
    assert [p.views for p in top] == [60, 50, 40]
    assert [p.views for p in bottom] == [10, 20, 30]


def test_a_negative_relationship_gives_a_negative_r(db_session):
    db = db_session
    _dataset(db, [1, 2, 3, 4, 5], [50, 40, 30, 20, 10])
    db.commit()

    assert quality_engagement_correlation(db).r == pytest.approx(-1.0)


def test_hook_comes_only_from_the_max_views_cut_even_when_that_cut_has_no_guide():
    """The page says the hook line is from the cut that drew the most views: a lower-view cut that
    does have a guide must not stand in for it."""
    reel = SimpleNamespace(cuts=[
        SimpleNamespace(views=500, guide=None),
        SimpleNamespace(views=5, guide=_guide_dict("Low-view hook?")),
    ])
    assert _hook_vo_for_max_views_cut(reel) is None


# ── _hook_vo_for_max_views_cut degrades to None rather than raising ──────────

def _reel_with_guide(guide):
    return SimpleNamespace(cuts=[SimpleNamespace(views=10, guide=guide)])


def test_hook_is_none_when_the_max_views_cut_has_no_guide():
    assert _hook_vo_for_max_views_cut(_reel_with_guide(None)) is None
    assert _hook_vo_for_max_views_cut(_reel_with_guide({})) is None


def test_hook_is_none_for_a_malformed_guide():
    assert _hook_vo_for_max_views_cut(_reel_with_guide({"beats": "not a list"})) is None
    assert _hook_vo_for_max_views_cut(_reel_with_guide(["junk"])) is None   # TypeError, not ValidationError


def test_hook_is_none_when_the_first_beat_is_not_a_hook():
    guide = _guide_dict("Not a hook")
    guide["beats"][0]["type"] = "body"
    assert _hook_vo_for_max_views_cut(_reel_with_guide(guide)) is None


def test_hook_is_none_not_an_empty_string_for_a_blank_hook_line():
    assert _hook_vo_for_max_views_cut(_reel_with_guide(_guide_dict(""))) is None


def test_hook_is_none_when_no_cut_has_views():
    reel = SimpleNamespace(cuts=[SimpleNamespace(views=None, guide=_guide_dict("Unmeasured?"))])
    assert _hook_vo_for_max_views_cut(reel) is None

