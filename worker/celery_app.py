from celery import Celery
from api.config import settings

celery_app = Celery(
    "reel_maker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "worker.tasks.enrich_context",
        "worker.tasks.generate",
        "worker.tasks.render",
        "worker.tasks.publish",
        "worker.tasks.maintenance",
        "worker.tasks.metrics",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Reliability: ack only after the task returns so a killed worker
    # requeues the message rather than silently losing it.
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Redis visibility timeout must exceed the worst-case task duration.
    # Renders can take 10+ min on slow machines; 2 h gives headroom.
    broker_transport_options={"visibility_timeout": 7200},

    # Prevent any one worker from hoarding multiple long tasks.
    worker_prefetch_multiplier=1,

    task_track_started=True,

    # Restart render workers after N tasks to reclaim MoviePy/ffmpeg memory.
    worker_max_tasks_per_child=10,

    task_routes={
        "worker.tasks.render.render_cut":                   {"queue": "rendering"},
        "worker.tasks.generate.generate_guide":             {"queue": "generation"},
        "worker.tasks.enrich_context.enrich_context":       {"queue": "generation"},
        # I/O-bound (network upload), not CPU-bound — belongs with generation, not rendering.
        "worker.tasks.publish.publish_cut":                 {"queue": "generation"},
        "worker.tasks.metrics.pull_publish_metrics":        {"queue": "generation"},
    },

    # Celery beat schedule for periodic maintenance.
    beat_schedule={
        "reap-stuck-jobs": {
            "task": "worker.tasks.maintenance.reap_stuck_jobs",
            "schedule": 60.0,  # every 60 s
        },
        "pull-publish-metrics": {
            "task": "worker.tasks.metrics.pull_publish_metrics",
            "schedule": 6 * 60 * 60.0,  # every 6 h — engagement doesn't need finer granularity
        },
    },
)
