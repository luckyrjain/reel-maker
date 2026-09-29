"""Tests for the reel list/detail HTML routes in api/routers/reels.py."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app
from api.routers.reels import latest_failed_job_for_reel


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c._session_factory = TestingSessionLocal
        yield c
    app.dependency_overrides.clear()


def _make_reel(session_factory, **overrides):
    db = session_factory()
    try:
        reel = models.Reel(
            context=overrides.get("context", "A test reel about something"),
            niche=overrides.get("niche", "football"),
            status=overrides.get("status", models.ReelStatus.guide_ready),
        )
        db.add(reel)
        db.flush()
        cut = models.Cut(
            reel_id=reel.id,
            platform=models.CutPlatform.youtube_shorts,
            target_length_s=45.0,
            status=overrides.get("cut_status", models.CutStatus.draft),
            video_path=overrides.get("video_path"),
        )
        db.add(cut)
        db.commit()
        db.refresh(reel)
        return reel.id
    finally:
        db.close()


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_latest_failed_job_for_reel_is_none_for_a_status_that_is_not_failed(db_session):
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db_session.add(reel)
    db_session.commit()
    assert latest_failed_job_for_reel(db_session, reel) is None


def test_latest_failed_job_for_reel_falls_back_to_none_without_a_matching_row(db_session):
    reel = models.Reel(context="x", status=models.ReelStatus.failed)
    db_session.add(reel)
    db_session.commit()
    assert latest_failed_job_for_reel(db_session, reel) is None


def test_latest_failed_job_for_reel_is_type_agnostic_and_picks_the_most_recent(db_session):
    """Mirrors latest_failed_job_for_cut()'s own type-agnostic contract: a reel can
    fail either enrichment or generation, and the row that explains the CURRENT
    "failed" state is whichever failed most recently, not a fixed type."""
    reel = models.Reel(context="x", status=models.ReelStatus.failed)
    db_session.add(reel)
    db_session.flush()
    now = datetime.now(timezone.utc)
    older_enrich_failure = models.Job(
        type=models.JobType.enrich, reel_id=reel.id,
        status=models.JobStatus.failed, error="enrichment LLM timed out",
        created_at=now,
    )
    newer_generate_failure = models.Job(
        type=models.JobType.generate, reel_id=reel.id,
        status=models.JobStatus.failed, error="guide failed schema validation",
        created_at=now + timedelta(seconds=5),
    )
    db_session.add_all([older_enrich_failure, newer_generate_failure])
    db_session.commit()

    found = latest_failed_job_for_reel(db_session, reel)
    assert found is not None
    assert found.id == newer_generate_failure.id
    assert found.error == "guide failed schema validation"


def test_list_reels_empty_state(client):
    resp = client.get("/api/reels")
    assert resp.status_code == 200
    assert "No reels yet" in resp.text


def test_list_reels_shows_created_reels(client):
    _make_reel(client._session_factory, context="First reel about tactics")
    _make_reel(client._session_factory, context="Second reel about transfers", niche="finance")

    resp = client.get("/api/reels")
    assert resp.status_code == 200
    assert "First reel about tactics" in resp.text
    assert "Second reel about transfers" in resp.text
    assert "finance" in resp.text


def test_list_reels_pagination_flags(client):
    for i in range(3):
        _make_reel(client._session_factory, context=f"Reel number {i}")

    resp = client.get("/api/reels?page=1")
    assert resp.status_code == 200
    # 3 reels fit on one page (page size 50) — no "Older" link
    assert "Older" not in resp.text


def test_list_reels_shows_quality_and_views_columns(client):
    reel_id = _make_reel(client._session_factory, context="Reel with metrics")

    db = client._session_factory()
    try:
        job = models.Job(
            type=models.JobType.generate,
            reel_id=reel_id,
            status=models.JobStatus.done,
            progress=100,
            meta={"quality_score": 87},
        )
        db.add(job)
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        cut.views = 4200
        db.commit()
    finally:
        db.close()

    resp = client.get("/api/reels")
    assert resp.status_code == 200
    assert "87" in resp.text
    assert "4,200" in resp.text


def test_list_reels_shows_dash_when_no_metrics_yet(client):
    _make_reel(client._session_factory, context="Reel without metrics")

    resp = client.get("/api/reels")
    assert resp.status_code == 200
    # Both the Quality and Views cells render a placeholder dash.
    assert resp.text.count(">—<") >= 2


def test_reel_detail_links_back_to_list(client):
    reel_id = _make_reel(client._session_factory)
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200


def test_reel_detail_embeds_live_render_status_for_a_rendering_cut(client):
    """Closes the "Cut page does not poll while rendering" Open Issues item: a fresh
    GET /api/reels/{id} for a cut mid-render must embed the same self-polling
    render_status.html fragment the triggering POST /render itself returns, not the
    old static "refresh to update" message."""
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.rendering)
    db = client._session_factory()
    try:
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        job = models.Job(
            type=models.JobType.render, reel_id=reel_id, cut_id=cut.id,
            status=models.JobStatus.running, progress=45,
        )
        db.add(job)
        db.commit()
        job_id, cut_id = job.id, cut.id
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert f"/api/cuts/{cut_id}/render-status?job_id={job_id}" in resp.text
    assert "Rendering in progress — refresh to update." not in resp.text


def test_reel_detail_embeds_live_publish_status_for_a_publishing_cut(client):
    """Same fix, publish side."""
    reel_id = _make_reel(
        client._session_factory, cut_status=models.CutStatus.publishing,
        video_path="/data/videos/1/youtube_shorts.mp4",
    )
    db = client._session_factory()
    try:
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        job = models.Job(
            type=models.JobType.publish, reel_id=reel_id, cut_id=cut.id,
            status=models.JobStatus.running, progress=60,
        )
        db.add(job)
        db.commit()
        job_id, cut_id = job.id, cut.id
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert f"/api/cuts/{cut_id}/publish-status?job_id={job_id}" in resp.text
    assert "Publishing in progress — refresh to update." not in resp.text


def test_reel_detail_falls_back_to_static_message_without_an_active_job_row(client):
    """Defensive branch: a rendering cut with no matching Job row (not expected in
    normal operation — see active_job_for_cut()'s docstring) still renders a sensible
    message instead of crashing or leaving the section blank."""
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.rendering)
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "Rendering in progress — refresh to update." in resp.text


def test_reel_detail_only_the_rendering_cut_gets_a_live_status_fragment(client):
    """Multi-cut isolation: active_jobs is threaded per-cut via {% set active_job =
    active_jobs.get(cut.id) %} in reel.html -- a reel with TWO rendering cuts, only one
    of which has a matching Job row, must not leak cut_a's job onto cut_b's card (or
    vice versa). Caught as an untested gap by an independent review round; verified by
    rendering the real page through the real app rather than reasoning about Jinja
    scoping in the abstract. Deliberately uses two cuts BOTH in "rendering" (not one
    rendering + one draft) -- a mutation that threads a single shared active_job value
    to every cut in the loop would otherwise go undetected here, since a non-rendering
    cut never even reaches the branch that reads active_job at all."""
    db = client._session_factory()
    try:
        reel = models.Reel(context="Multi-cut isolation reel", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut_with_job = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
            status=models.CutStatus.rendering,
        )
        cut_without_job = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.instagram_reels,
            status=models.CutStatus.rendering,
        )
        db.add_all([cut_with_job, cut_without_job])
        db.flush()
        job = models.Job(
            type=models.JobType.render, reel_id=reel.id, cut_id=cut_with_job.id,
            status=models.JobStatus.running, progress=55,
        )
        db.add(job)
        db.commit()
        reel_id, with_job_id, without_job_id, job_id = (
            reel.id, cut_with_job.id, cut_without_job.id, job.id,
        )
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    # Exactly one live-status fragment on the whole page, naming cut_with_job's own job.
    assert resp.text.count("hx-get=\"/api/cuts/") == 1
    assert f"/api/cuts/{with_job_id}/render-status?job_id={job_id}" in resp.text
    # cut_without_job's own card must show its status branch's static fallback, not
    # cut_with_job's live fragment or job id bleeding across the loop iteration.
    without_job_card = resp.text.split(f'id="cut-card-{without_job_id}"')[1]
    with_job_card = resp.text.split(f'id="cut-card-{with_job_id}"')[1].split(
        f'id="cut-card-{without_job_id}"'
    )[0]
    assert "Rendering in progress — refresh to update." in without_job_card
    assert f"job_id={job_id}" not in without_job_card
    assert f"job_id={job_id}" in with_job_card


def test_reel_detail_surfaces_the_real_failure_reason_for_a_failed_cut(client):
    """Closes the "Failure reason is not shown after a page refresh" Open Issues item:
    job.error used to be rendered only inside render_status.html/publish_status.html,
    the polling fragment of the tab that happened to be open when the job failed. A
    fresh GET /api/reels/{id} for a failed cut must now show the actual error text,
    not just the generic hard-coded "Failed. Retry render..." message."""
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.failed)
    db = client._session_factory()
    try:
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        job = models.Job(
            type=models.JobType.render, reel_id=reel_id, cut_id=cut.id,
            status=models.JobStatus.failed,
            error="FFmpeg text/audio pass failed (exit 234): Stray % near ' today'",
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    # Jinja HTML-autoescapes the apostrophe as &#39; -- assert on the un-escapable core.
    assert "Stray % near" in resp.text
    assert "exit 234" in resp.text


def test_reel_detail_failed_cut_without_a_job_row_shows_only_the_generic_message(client):
    """Defensive fallback: a failed cut with no matching Job row (not expected in
    normal operation) must not error or show a blank/broken error paragraph -- only
    the pre-existing generic message."""
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.failed)
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "Failed. Retry render" in resp.text


def test_reel_detail_only_the_matching_failed_cut_shows_its_own_error(client):
    """Multi-cut isolation for failed_jobs, mirroring the active_jobs isolation test
    above -- two failed cuts, each with its own distinct error, must never show each
    other's error text."""
    db = client._session_factory()
    try:
        reel = models.Reel(context="Multi-failure isolation reel", status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        cut_a = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
            status=models.CutStatus.failed,
        )
        cut_b = models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.instagram_reels,
            status=models.CutStatus.failed,
        )
        db.add_all([cut_a, cut_b])
        db.flush()
        job_a = models.Job(
            type=models.JobType.render, reel_id=reel.id, cut_id=cut_a.id,
            status=models.JobStatus.failed, error="cut A distinctive render error",
        )
        job_b = models.Job(
            type=models.JobType.publish, reel_id=reel.id, cut_id=cut_b.id,
            status=models.JobStatus.failed, error="cut B distinctive publish error",
        )
        db.add_all([job_a, job_b])
        db.commit()
        reel_id, cut_a_id, cut_b_id = reel.id, cut_a.id, cut_b.id
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    card_a = resp.text.split(f'id="cut-card-{cut_a_id}"')[1].split(f'id="cut-card-{cut_b_id}"')[0]
    card_b = resp.text.split(f'id="cut-card-{cut_b_id}"')[1]
    assert "cut A distinctive render error" in card_a
    assert "cut B distinctive publish error" not in card_a
    assert "cut B distinctive publish error" in card_b
    assert "cut A distinctive render error" not in card_b


def test_reel_detail_surfaces_the_real_failure_reason_for_a_failed_reel(client):
    """The reel-level sibling of the cut-level fix above: a failed generate/enrich job
    rolls the REEL (not a cut) back to "failed" via JOB_IN_FLIGHT -- found as a
    symmetric, previously-missed gap during review of the cut-level fix. Before this,
    reel.html showed only the bare "failed" badge with no reason, since
    pipeline_status.html (the fragment that DOES show job.error) is only ever returned
    by POST /api/reels and GET /active-job-fragment directly, never included in
    reel.html itself."""
    reel_id = _make_reel(client._session_factory, status=models.ReelStatus.failed)
    db = client._session_factory()
    try:
        job = models.Job(
            type=models.JobType.generate, reel_id=reel_id,
            status=models.JobStatus.failed,
            error="Guide generation failed schema validation after 3 attempts",
        )
        db.add(job)
        db.commit()
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "Guide generation failed schema validation after 3 attempts" in resp.text


def test_reel_detail_failed_reel_without_a_job_row_shows_no_error_paragraph(client):
    """Defensive fallback, reel level: a failed reel with no matching Job row must not
    error or show a broken/empty error paragraph."""
    reel_id = _make_reel(client._session_factory, status=models.ReelStatus.failed)
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    # No stray error paragraph from the reel-level block specifically (the cut card's
    # own unrelated "Failed. Retry render..." text is a separate, expected paragraph).
    assert resp.text.count('<p class="error" style="margin-top:6px;">') == 0


def test_failed_cut_card_has_no_duplicate_ids_and_valid_hx_targets(client):
    """Regression test: a "Retry publish" button once targeted #cut-card-{id}
    with outerHTML while the endpoint actually returns the small publish_status
    fragment — silently destroying the card on retry. Every hx-target here must
    resolve to an id that actually exists in the same render, and no id may be
    duplicated (htmx swaps get undefined/wrong-element behavior otherwise).
    """
    import re

    reel_id = _make_reel(
        client._session_factory,
        cut_status=models.CutStatus.failed,
        video_path="/data/videos/1/youtube_shorts.mp4",
    )
    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    html = resp.text

    ids = re.findall(r'id="([^"]+)"', html)
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate DOM ids: {duplicates}"

    targets = re.findall(r'hx-target="#([^"]+)"', html)
    assert "publish-section-" in "".join(targets)  # sanity: the retry-publish button is present
    for target in targets:
        assert target in ids, f"hx-target=#{target} has no matching id=\"{target}\" in the page"


def test_published_cut_card_shows_engagement_stats(client):
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.published)

    db = client._session_factory()
    try:
        cut = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).first()
        cut.views = 1234
        cut.likes = 56
        cut.comments = 7
        cut.metrics_updated_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        db.commit()
    finally:
        db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "1,234" in resp.text
    assert "views" in resp.text
    assert "56" in resp.text
    assert "as of 2026-01-01 12:00 UTC" in resp.text


def test_published_cut_card_shows_not_pulled_yet_before_first_metrics_pull(client):
    reel_id = _make_reel(client._session_factory, cut_status=models.CutStatus.published)

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "not pulled yet" in resp.text


def test_reel_detail_404_for_missing_reel(client):
    resp = client.get("/api/reels/999999")
    assert resp.status_code == 404


def test_reel_detail_surfaces_pipeline_cost_and_quality(client):
    db = client._session_factory()
    reel = models.Reel(context="A reel with pipeline data", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()

    job = models.Job(
        type=models.JobType.generate,
        reel_id=reel.id,
        status=models.JobStatus.done,
        meta={"quality_score": 82},
    )
    db.add(job)

    db.add(models.StageEvent(
        reel_id=reel.id, stage="generate", provider="nvidia",
        latency_ms=1200, tokens_in=500, tokens_out=200, cost_usd=0.0021, ok=True,
    ))
    db.add(models.StageEvent(
        reel_id=reel.id, stage="judge", provider="nvidia",
        latency_ms=800, tokens_in=100, tokens_out=50, cost_usd=0.0004, ok=True,
    ))
    db.add(models.StageEvent(
        reel_id=reel.id, stage="judge", provider="nvidia",
        latency_ms=900, ok=False, detail={"error": "boom"},
    ))
    db.commit()
    reel_id = reel.id
    db.close()

    resp = client.get(f"/api/reels/{reel_id}")
    assert resp.status_code == 200
    assert "82" in resp.text  # quality score
    assert "$0.0025" in resp.text  # total cost (0.0021 + 0.0004)
    assert "generate" in resp.text
    assert "judge" in resp.text


def test_pipeline_summary_aggregates_stages_and_failures(client):
    from api.routers.reels import _pipeline_summary

    db = client._session_factory()
    reel = models.Reel(context="Aggregation test", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", cost_usd=0.001, latency_ms=500, ok=True))
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", cost_usd=0.002, latency_ms=700, ok=False))
    db.commit()

    summary = _pipeline_summary(db, reel.id)
    assert summary["stage_summary"]["enrich"]["count"] == 2
    assert summary["stage_summary"]["enrich"]["failures"] == 1
    assert round(summary["total_cost"], 3) == 0.003
    assert summary["total_latency_ms"] == 1200
    db.close()


def test_pipeline_summary_excludes_instagram_metrics_from_headline_totals_but_not_the_stage_table(client):
    """instagram_metrics fires every 6h for as long as a published Instagram cut
    stays published (unlike every generation/render stage, which fires a
    bounded number of times) — it must still show up in the per-stage
    breakdown table with its own count/latency/cost/failures, but must NOT be
    folded into the two headline total_cost/total_latency_ms sums, or "total
    LLM time" would silently and permanently inflate on any reel with a
    long-lived published Instagram cut. See
    docs/specs/2026-09-instagram-metrics-drift-system-design.md §8."""
    from api.routers.reels import _pipeline_summary

    db = client._session_factory()
    reel = models.Reel(context="Instagram metrics exclusion test", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    db.add(models.StageEvent(reel_id=reel.id, stage="generate", cost_usd=0.01, latency_ms=2000, ok=True))
    db.add(models.StageEvent(
        reel_id=reel.id, stage="instagram_metrics", cost_usd=0.5, latency_ms=99999, ok=False,
        detail={"missing_metrics": ["comments"]},
    ))
    db.commit()

    summary = _pipeline_summary(db, reel.id)

    # Per-stage table stays unfiltered — instagram_metrics gets its own row.
    assert summary["stage_summary"]["instagram_metrics"]["count"] == 1
    assert summary["stage_summary"]["instagram_metrics"]["failures"] == 1
    assert summary["stage_summary"]["instagram_metrics"]["cost_usd"] == 0.5
    assert summary["stage_summary"]["instagram_metrics"]["latency_ms"] == 99999
    assert summary["stage_summary"]["generate"]["count"] == 1

    # Headline totals reflect only the "generate" row — instagram_metrics excluded.
    assert round(summary["total_cost"], 3) == 0.01
    assert summary["total_latency_ms"] == 2000
    db.close()


def test_estimate_endpoint_returns_fragment_for_unstructured_context(client):
    resp = client.post(
        "/api/reels/estimate",
        data={"context": "A loose paragraph about a topic.", "generation_path": "auto"},
    )
    assert resp.status_code == 200
    assert "standard path" in resp.text
    assert "paid LLM calls" in resp.text


def test_create_reel_defaults_to_youtube_and_instagram(client):
    with patch("api.routers.reels.enrich_context") as mock_enrich:
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured"},
        )
    assert resp.status_code == 200
    mock_enrich.delay.assert_called_once()
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    platforms = {c.platform.value for c in reel.cuts}
    assert platforms == {"youtube_shorts", "instagram_reels"}
    db.close()


def test_create_reel_that_cannot_be_enqueued_fails_fast_instead_of_polling_forever(client):
    with patch("api.routers.reels.enrich_context") as mock_enrich:
        mock_enrich.delay.side_effect = ConnectionError("broker down")
        resp = client.post("/api/reels", data={"context": "A" * 60})
    assert resp.status_code == 503
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.status == models.ReelStatus.failed
    job = db.query(models.Job).filter(models.Job.reel_id == reel.id).one()
    assert job.status == models.JobStatus.failed and "could not enqueue" in job.error
    db.close()


def test_the_enrich_job_is_committed_before_it_is_enqueued(client):
    """Same shape as the cuts router's equivalent test: no transaction of ours may be open across
    .delay() (a slow broker failure would otherwise outlive an idle-in-transaction timeout), and the
    worker must already be able to see the job row it's handed."""
    from api.db import get_db
    from api.main import app

    sessions = []
    real_override = app.dependency_overrides[get_db]

    def tracking_override():
        gen = real_override()
        db = next(gen)
        sessions.append(db)
        try:
            yield db
        finally:
            try:
                next(gen)
            except StopIteration:
                pass

    seen = {}

    def delay(job_id):
        assert isinstance(job_id, int)
        seen["in_transaction_during_delay"] = sessions[-1].in_transaction()
        other = client._session_factory()
        job = other.get(models.Job, job_id)
        seen["visible"] = job is not None and job.status == models.JobStatus.pending
        other.close()

    app.dependency_overrides[get_db] = tracking_override
    try:
        with patch("api.routers.reels.enrich_context") as mock_enrich:
            mock_enrich.delay.side_effect = delay
            resp = client.post("/api/reels", data={"context": "A" * 60})
    finally:
        app.dependency_overrides[get_db] = real_override
    assert resp.status_code == 200, resp.text
    assert seen["visible"] is True
    assert seen["in_transaction_during_delay"] is False


def test_create_reel_honors_explicit_platform_selection(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={
                "context": "A" * 60,
                "generation_path": "structured",
                "platforms": ["youtube_shorts", "tiktok"],
            },
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    platforms = {c.platform.value for c in reel.cuts}
    assert platforms == {"youtube_shorts", "tiktok"}
    db.close()


def test_create_reel_ignores_unknown_platform_values(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={
                "context": "A" * 60,
                "generation_path": "structured",
                "platforms": ["not_a_real_platform"],
            },
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    # Falls back to the default pair since nothing valid was submitted.
    platforms = {c.platform.value for c in reel.cuts}
    assert platforms == {"youtube_shorts", "instagram_reels"}
    db.close()


def test_create_reel_honors_explicit_tts_voice_selection(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured", "tts_voice": "en-US-JennyNeural"},
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.tts_voice == "en-US-JennyNeural"
    db.close()


def test_create_reel_ignores_unknown_tts_voice_value(client):
    """Same 'drop, don't 422' policy as unknown platform values — falls back to None
    (the provider default) rather than failing the whole submission."""
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured", "tts_voice": "not-a-real-voice"},
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.tts_voice is None
    db.close()


def test_create_reel_without_tts_voice_defaults_to_none(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post("/api/reels", data={"context": "A" * 60, "generation_path": "structured"})
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.tts_voice is None
    db.close()


def test_create_reel_honors_explicit_text_color_selection(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured", "text_color": "yellow"},
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.text_color == "yellow"
    db.close()


def test_create_reel_ignores_unknown_text_color_value(client):
    """Same 'drop, don't 422' policy as unknown platform/voice values — falls back to
    None (the compositor default) rather than failing the whole submission or storing
    an unvalidated value that would reach the ffmpeg filter graph unescaped."""
    with patch("api.routers.reels.enrich_context"):
        resp = client.post(
            "/api/reels",
            data={"context": "A" * 60, "generation_path": "structured", "text_color": "not_a_real_color"},
        )
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.text_color is None
    db.close()


def test_create_reel_without_text_color_defaults_to_none(client):
    with patch("api.routers.reels.enrich_context"):
        resp = client.post("/api/reels", data={"context": "A" * 60, "generation_path": "structured"})
    assert resp.status_code == 200
    db = client._session_factory()
    reel = db.query(models.Reel).order_by(models.Reel.id.desc()).first()
    assert reel.text_color is None
    db.close()


def test_estimate_endpoint_detects_structured_script(client):
    structured_ctx = (
        "GOALKEEPER\nMartinez saves penalties.\n"
        "DEFENSE\nRomero leads the line.\n"
        "MIDFIELD\nDe Paul is the engine."
    )
    resp = client.post(
        "/api/reels/estimate",
        data={"context": structured_ctx, "generation_path": "auto"},
    )
    assert resp.status_code == 200
    assert "structured path" in resp.text
