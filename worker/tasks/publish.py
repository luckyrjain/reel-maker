import logging
from datetime import datetime, timezone

from api import models
from api.state import CUT_TRANSITIONS, transition
from engine.observability import record_stage
from engine.publish.attribution import build_published_caption
from engine.publish.gate import assert_safe_to_publish
from engine.publish.registry import credential_provider_for_platform, get_publisher
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat, job_task, time_limits

_log = logging.getLogger(__name__)


def _load_cut(db, job):
    cut = db.get(models.Cut, job.cut_id)
    if cut is None:
        raise ValueError(f"Cut {job.cut_id} no longer exists")
    return cut


_MAX_RUNTIME_S = 60 * 60


# max_retries=0 on purpose: publishing is an irreversible external side effect. A transient
# error (e.g. a read timeout) can arrive AFTER the platform accepted the upload, and an
# automatic retry would then post the video twice. The operator retries via the UI instead.
# The platform_post_id guard below only covers failures AFTER the post was recorded; a timeout
# inside publisher.publish() happens before any id exists, so the operator should check the
# platform before retrying (the failed-cut card says so).
@celery_app.task(bind=True, max_retries=0, **time_limits(_MAX_RUNTIME_S))
# release_on_shutdown=False: a shutdown mid-upload may have been accepted by the platform; a
# redelivered run would upload again. The job stays running and the reaper fails it instead.
@job_task("publish", prepare=_load_cut, max_runtime_s=_MAX_RUNTIME_S, release_on_shutdown=False)
def publish_cut(self, db, job, cut):
    # cut.status is set to "publishing" by the router before this task is
    # enqueued (mirrors trigger_render / render_cut) — this task does not
    # gate on cut.status itself. Any failure fails the job and rolls the cut back
    # from "publishing"; the operator retries from the UI (see max_retries above).
    if not cut.video_path:
        raise ValueError("Cut has no rendered video — render and approve it before publishing")

    heartbeat(db, job, 20)

    if cut.platform_post_id:
        # A previous run already posted this cut but did not finish recording it (reaped, or
        # died before the done-stamp). Finalize without uploading again. The safety gate guards
        # what goes OUT to a platform; nothing is uploaded here, and blocking the finalize would
        # leave a live post unrecorded with no way for the operator to clear it.
        _log.warning("cut %s already has platform_post_id %s; skipping upload", cut.id, cut.platform_post_id)
    else:
        assert_safe_to_publish(db, cut.id)   # before any credential lookup or upload
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
        db.commit()   # end the read transaction: it would sit idle for the whole upload
        with record_stage(db, cut.reel_id, "publish", cut_id=cut.id, provider=cut.platform.value) as ev:
            result = publisher.publish(cut, credential, db, caption=caption)
            ev.detail["platform_post_id"] = result.platform_post_id

        cut.platform_post_id = result.platform_post_id
        cut.published_at = datetime.now(timezone.utc)
        # The one deliberate early commit: the post is live and irreversible, so record its id
        # now rather than only in the done-stamp. If anything below fails, or the job is
        # reaped before finishing, a retry sees this id and does not post a second time.
        db.commit()

    transition(cut, "published", CUT_TRANSITIONS)
