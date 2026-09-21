"""Tests for the render_cut task's failure handling and success-path cleanup."""
from unittest.mock import MagicMock, patch

import pytest

from api import models

_GUIDE = {
    "platform": "youtube_shorts",
    "target_length_s": 30.0,
    "caption": "Argentina's one weak spot could decide the tournament.",
    "hashtags": ["football"] * 10,
    "beats": [
        {"index": 0, "type": "hook", "duration_s": 3.0,
         "visual_direction": "Argentina squad walking out",
         "on_screen_text": ["One weak spot"], "vo_script": "One position could cost them everything."},
        {"index": 1, "type": "body", "duration_s": 10.0,
         "visual_direction": "Cristian Romero aggressive tackle",
         "on_screen_text": ["Romero presses"], "vo_script": "Romero's press lets them defend higher."},
        {"index": 2, "type": "cta", "duration_s": 5.0,
         "visual_direction": "Argentina squad celebrating",
         "on_screen_text": ["Your call"], "vo_script": "So what do you think? Drop it below."},
    ],
}

_GUIDE_WITH_MUSIC_CUE = {
    **_GUIDE,
    "beats": [
        {**_GUIDE["beats"][0], "music_cue": "tense minimal"},
        _GUIDE["beats"][1],
        _GUIDE["beats"][2],
    ],
}


def _job(job_id=1, cut_id=5, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.cut_id = cut_id
    job.reel_id = reel_id
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.error = "stale error from a previous attempt"
    return job


def _cut():
    cut = MagicMock()
    cut.id = 5
    cut.reel_id = 10
    cut.guide = _GUIDE
    cut.platform.value = "youtube_shorts"
    cut.status.value = "rendering"
    return cut


def _reel():
    reel = MagicMock()
    reel.id = 10
    reel.voiceover_mode = "silent"
    return reel


def test_missing_cut_fails_job_with_actionable_message():
    """A deleted cut must not crash on cut.reel_id."""
    from worker.tasks.render import render_cut

    job = _job(cut_id=77)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.common.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Cut 77 no longer exists"):
            render_cut(1)

    assert job.status == models.JobStatus.failed
    assert "Cut 77 no longer exists" in job.error


def test_successful_render_clears_stale_error():
    """A retried-then-successful render must not leave an error in the UI."""
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else reel
    )

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer"),
        patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"),
        patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=18.0),
    ):
        render_cut(1)

    assert job.status == models.JobStatus.done
    assert job.error is None


def test_matching_music_cue_is_passed_to_composite_cut():
    """The hook beat's music_cue should resolve to a track and reach composite_cut."""
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    cut.guide = _GUIDE_WITH_MUSIC_CUE
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else reel
    )

    fake_track = MagicMock(name="fake_music_path")
    fake_sourcer = MagicMock()
    fake_sourcer.find.return_value = fake_track

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer"),
        patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"),
        patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.get_music_sourcer", return_value=fake_sourcer),
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=18.0) as mock_composite,
    ):
        render_cut(1)

    fake_sourcer.find.assert_called_once_with("tense minimal")
    assert mock_composite.call_args.kwargs["music_path"] is fake_track


def test_no_music_cue_passes_none_without_querying_sourcer():
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()  # _GUIDE — no beat has music_cue
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else reel
    )

    fake_sourcer = MagicMock()

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer"),
        patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"),
        patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.get_music_sourcer", return_value=fake_sourcer),
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=18.0) as mock_composite,
    ):
        render_cut(1)

    fake_sourcer.find.assert_not_called()
    assert mock_composite.call_args.kwargs["music_path"] is None
