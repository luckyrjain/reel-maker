"""Celery beat task: pulls engagement metrics for already-published cuts.

Read-only against each platform's read API — never touches publish state,
never runs under the job_task Job lifecycle that publish_cut uses (this is a
best-effort background refresh, not a pipeline stage a Cut's status depends
on). A fetch failure for one cut is logged and skipped so it doesn't abort
the rest of the batch.
"""
import logging
from datetime import datetime, timezone

from api import models
from api.db import SessionLocal
from engine.publish.registry import credential_provider_for_platform, get_metrics_fetcher
from worker.celery_app import celery_app

_log = logging.getLogger(__name__)


@celery_app.task
def pull_publish_metrics():
    db = SessionLocal()
    try:
        cuts = (
            db.query(models.Cut)
            .filter(
                models.Cut.status == models.CutStatus.published,
                models.Cut.platform_post_id.isnot(None),
            )
            .all()
        )
        for cut in cuts:
            _pull_one(db, cut)
    finally:
        db.close()


def _pull_one(db, cut: "models.Cut") -> None:
    fetcher = get_metrics_fetcher(cut.platform.value)
    if fetcher is None:
        return

    provider_name = credential_provider_for_platform(cut.platform.value)
    credential = (
        db.query(models.Credential)
        .filter(models.Credential.provider == provider_name)
        .first()
    )
    if credential is None:
        return

    try:
        result = fetcher.fetch(cut, credential, db)
    except Exception:
        _log.exception(
            "Failed to pull metrics for cut_id=%s platform=%s", cut.id, cut.platform.value
        )
        return
    if result is None:
        return

    if result.views is not None:
        cut.views = result.views
    if result.likes is not None:
        cut.likes = result.likes
    if result.comments is not None:
        cut.comments = result.comments
    cut.metrics_updated_at = datetime.now(timezone.utc)
    db.commit()
