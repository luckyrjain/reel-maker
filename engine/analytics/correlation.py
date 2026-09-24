"""Quality-vs-engagement analytics for the /api/insights page.

New package (not engine/observability.py) — that module stays scoped to
per-reel instrumentation (StageEvent bookkeeping); this is the first
cross-reel "insights" feature, reading data that already exists rather than
computing anything new to store.
"""
from dataclasses import dataclass

import numpy as np
from sqlalchemy.orm import joinedload

from api import models
from engine.generation.guide_schema import PlatformGuide
from engine.observability import latest_quality_scores

MIN_SAMPLE = 5  # below this, a Pearson r is more noise than signal — refuse to compute one


@dataclass
class CorrelationResult:
    r: float | None          # None when sample_size < MIN_SAMPLE or either series has zero variance
    sample_size: int
    insufficient_variance: bool  # True when r is None because scores (or views) were all identical


def quality_engagement_correlation(db) -> CorrelationResult:
    reels = db.query(models.Reel).options(joinedload(models.Reel.cuts)).all()
    reel_ids = [r.id for r in reels]
    jobs = (
        db.query(models.Job)
        .filter(models.Job.reel_id.in_(reel_ids))
        .order_by(models.Job.created_at)
        .all()
    )   # loads every job type (enrich/generate/render/publish), not just generate — matches
        # _reel_list_metrics()/_pipeline_summary()'s existing query shape exactly; harmless
        # since latest_quality_scores() only reads meta["quality_score"] (generate jobs only
        # ever set it), but a `Job.type == generate` filter would be a cheap follow-up if this
        # ever shows up in a query-count budget
    quality_by_reel = latest_quality_scores(jobs)

    pairs = []
    for r in reels:
        q = quality_by_reel.get(r.id)
        views = [c.views for c in r.cuts if c.views is not None]
        v = max(views) if views else None
        if q is not None and v is not None:
            pairs.append((q, v))

    n = len(pairs)
    if n < MIN_SAMPLE:
        return CorrelationResult(r=None, sample_size=n, insufficient_variance=False)

    qs = np.array([p[0] for p in pairs], dtype=float)
    vs = np.array([p[1] for p in pairs], dtype=float)
    if np.std(qs) == 0 or np.std(vs) == 0:
        # np.corrcoef on a zero-variance input returns NaN with a RuntimeWarning —
        # guard explicitly rather than let NaN leak into a formatted string as "nan".
        return CorrelationResult(r=None, sample_size=n, insufficient_variance=True)

    r = float(np.corrcoef(qs, vs)[0, 1])
    return CorrelationResult(r=r, sample_size=n, insufficient_variance=False)


@dataclass
class Performer:
    reel_id: int
    niche: str | None
    quality_score: int
    views: int
    hook_vo: str | None   # None when the max-views cut has no guide/beats, or beats[0] isn't type "hook"


def _hook_vo_for_max_views_cut(reel: models.Reel) -> str | None:
    """The hook beat's vo_script from whichever of the reel's cuts has the max views —
    the same cut that contributed the reel's `views` figure. Degrades to None rather
    than raising for any malformed/missing data — this is a read-only report."""
    cuts_with_views = [c for c in reel.cuts if c.views is not None]
    if not cuts_with_views:
        return None
    max_cut = max(cuts_with_views, key=lambda c: c.views)
    if not max_cut.guide:
        return None
    try:
        guide = PlatformGuide(**max_cut.guide)
    except Exception:
        return None
    if not guide.beats:
        return None
    hook = guide.beats[0]
    if hook.type != "hook":
        return None
    return hook.vo_script or None


def top_bottom_performers(db, k: int = 3):
    """Returns (top_k, bottom_k) sorted by views descending/ascending, or one combined
    views-descending list when n < 2*k (top/bottom would otherwise overlap)."""
    reels = db.query(models.Reel).options(joinedload(models.Reel.cuts)).all()
    reel_ids = [r.id for r in reels]
    jobs = (
        db.query(models.Job)
        .filter(models.Job.reel_id.in_(reel_ids))
        .order_by(models.Job.created_at)
        .all()
    )
    quality_by_reel = latest_quality_scores(jobs)

    performers: list[Performer] = []
    for r in reels:
        q = quality_by_reel.get(r.id)
        views = [c.views for c in r.cuts if c.views is not None]
        v = max(views) if views else None
        if q is None or v is None:
            continue
        performers.append(Performer(
            reel_id=r.id,
            niche=r.niche,
            quality_score=q,
            views=v,
            hook_vo=_hook_vo_for_max_views_cut(r),
        ))

    performers.sort(key=lambda p: p.views, reverse=True)

    if len(performers) < 2 * k:
        return performers

    top = performers[:k]
    bottom = list(reversed(performers[-k:]))
    return top, bottom
