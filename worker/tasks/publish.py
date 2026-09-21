from datetime import datetime, timezone

from api import models
from api.state import CUT_TRANSITIONS, transition
from engine.observability import record_stage
from engine.publish.attribution import build_published_caption
from engine.publish.gate import assert_safe_to_publish
from engine.publish.registry import credential_provider_for_platform, get_publisher
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat, job_task


def _load_cut(db, job):
    cut = db.get(models.Cut, job.cut_id)
    if cut is None:
        raise ValueError(f"Cut {job.cut_id} no longer exists")
    return cut


@celery_app.task(bind=True, max_retries=2)
@job_task("publish", prepare=_load_cut)
def publish_cut(self, db, job, cut):
    # cut.status is set to "publishing" by the router before this task is
    # enqueued (mirrors trigger_render / render_cut) — this task does not
    # gate on cut.status itself. A transient failure resets job.status to
    # pending for retry without touching cut.status, so a retried run lands
    # back here directly rather than tripping a status guard.
    if not cut.video_path:
        raise ValueError("Cut has no rendered video — render and approve it before publishing")

    assert_safe_to_publish(db, cut.id)
    heartbeat(db, job, 20)

    provider_name = credential_provider_for_platform(cut.platform.value)
    credential = (
        db.query(models.Credential)
        .filter(models.Credential.provider == provider_name)
        .first()
    )
    if credential is None:
        raise ValueError(
            f"No connected {provider_name} account — connect one at /api/credentials "
            "before publishing"
        )

    heartbeat(db, job, 35)

    publisher = get_publisher(cut.platform.value)
    caption = build_published_caption(db, cut)
    with record_stage(db, cut.reel_id, "publish", cut_id=cut.id, provider=cut.platform.value) as ev:
        result = publisher.publish(cut, credential, db, caption=caption)
        ev.detail["platform_post_id"] = result.platform_post_id

    cut.platform_post_id = result.platform_post_id
    cut.published_at = datetime.now(timezone.utc)
    transition(cut, "published", CUT_TRANSITIONS)
