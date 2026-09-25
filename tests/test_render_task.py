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
    cut.platform_post_id = None   # not posted (a bare MagicMock attribute would be truthy)
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


def test_an_already_posted_cut_cannot_be_re_rendered():
    """Re-rendering would change the video while the cut still points at the old live post."""
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    cut.platform_post_id = "yt-live"
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else _reel()
    )

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.render.composite_cut") as mock_composite,
    ):
        with pytest.raises(ValueError, match="already posted"):
            render_cut(1)

    mock_composite.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_a_render_that_a_publish_overtook_is_discarded():
    """Retry render and Retry publish were both started from a failed cut; the publish posted first.
    Recording this render would let a later finalize mark new content as the posted video."""
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else reel
    )
    db.refresh.side_effect = lambda obj: setattr(obj, "platform_post_id", "yt-posted-meanwhile") if obj is cut else None

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer"),
        patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"),
        patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])),
    ):
        with pytest.raises(ValueError, match="posted while it rendered"):
            render_cut(1)

    assert job.status == models.JobStatus.failed
    assert not isinstance(cut.video_path, str), "the discarded render must not be recorded on the cut"


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
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])),
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
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])) as mock_composite,
    ):
        render_cut(1)

    fake_sourcer.find.assert_called_once_with("tense minimal")
    assert mock_composite.call_args.kwargs["music_path"] is fake_track


def test_reel_tts_voice_is_passed_to_get_tts_provider():
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    reel.tts_voice = "en-US-JennyNeural"
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
        patch("worker.tasks.render.get_tts_provider") as mock_get_tts,
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])),
    ):
        render_cut(1)

    assert mock_get_tts.call_args.kwargs["voice"] == "en-US-JennyNeural"


def test_reel_text_color_is_passed_to_composite_cut():
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    reel.text_color = "yellow"
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
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])) as mock_composite,
    ):
        render_cut(1)

    assert mock_composite.call_args.kwargs["text_color"] == "yellow"


def test_reel_without_text_color_uses_the_default():
    from worker.tasks.render import DEFAULT_TEXT_COLOR, render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    reel.text_color = None
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
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])) as mock_composite,
    ):
        render_cut(1)

    assert mock_composite.call_args.kwargs["text_color"] == DEFAULT_TEXT_COLOR


def test_black_frame_beats_are_flagged_on_the_cut():
    """resolve_beat_assets()'s sentinel for 'nothing found anywhere in the fallback
    chain' is [(None, None)] — a beat whose paths are all None got no real footage and
    rendered as a black frame for its full duration. Beat 1 (of 0/1/2) is the one
    flagged here; 0 and 2 resolve normally."""
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else reel
    )

    def fake_resolve(db, cut, beat_index, **kwargs):
        if beat_index == 1:
            return [(None, None)]
        return [(MagicMock(), MagicMock())]

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer"),
        patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"),
        patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.resolve_or_reuse", side_effect=fake_resolve),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])),
    ):
        render_cut(1)

    assert cut.black_frame_beat_indices == [1]


def test_no_black_frame_flag_when_every_beat_has_real_media():
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
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), MagicMock())]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])),
    ):
        render_cut(1)

    assert cut.black_frame_beat_indices is None


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
        patch("worker.tasks.render.composite_cut", return_value=(18.0, ["thumb.jpg"])) as mock_composite,
    ):
        render_cut(1)

    fake_sourcer.find.assert_not_called()
    assert mock_composite.call_args.kwargs["music_path"] is None
