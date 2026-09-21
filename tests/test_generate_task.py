"""Tests for the generate_guide task's failure handling — missing rows and retries."""
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from api import models
from engine.generation.script_parser import BeatStub
from worker.tasks.generate import _stubs_to_platform_guide


def _job(job_id=1, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.reel_id = reel_id
    job.cut_id = None
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.meta = {}
    job.error = None
    return job


def _reel():
    reel = MagicMock()
    reel.id = 10
    reel.context = "Argentina squad review."
    reel.enriched_context = None
    reel.niche = "football"
    reel.voiceover_mode = "voiceover"
    reel.status.value = "generating"
    return reel


def _cut():
    cut = MagicMock()
    cut.platform.value = "youtube_shorts"
    cut.target_length_s = 45.0
    return cut


def test_missing_reel_fails_job_with_actionable_message():
    """A deleted reel must produce an operator-readable error, not an AttributeError."""
    from worker.tasks.generate import generate_guide

    job = _job(reel_id=99)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.common.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Reel 99 no longer exists"):
            generate_guide(1)

    assert job.status == models.JobStatus.failed
    assert "Reel 99 no longer exists" in job.error
    assert "AttributeError" not in job.error


def test_reel_rolled_back_to_failed_is_not_generated():
    """enrich_context rolls the reel back when it could not confirm the enqueue; a message that
    still reached the broker must not spend paid LLM calls on a failed reel."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    reel.status.value = "failed"
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider") as mock_llm,
    ):
        with pytest.raises(ValueError, match="not 'generating'"):
            generate_guide(1)

    mock_llm.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_paid_call_budget_exceeded_fails_without_retry():
    """A reel that already hit its paid-call cap must fail cleanly, not retry."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=20),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="Paid LLM call budget exceeded"):
            generate_guide(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_structured_path_skipped_when_parse_disagrees_with_is_structured():
    """resolve_generation_path()'s "auto" branch decides via is_structured()
    alone (it has no stubs to check — it's shared with the pre-generation cost
    estimate endpoint, which never parses anything). parse() runs the same
    is_structured() check first but can still return None afterward. If those
    two ever disagree, generate_guide must not call
    _generate_from_structured_script with stubs=None — it must fall straight
    to the standard path instead of wasting an attempt on a guaranteed
    TypeError that then gets silently swallowed."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.script_parser.parse", return_value=None),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script") as mock_structured,
        # get_llm_provider() is called unconditionally before either path is
        # selected — let it succeed, and fail the standard path itself
        # instead, right after it records job.meta["path"] = "standard".
        patch("worker.tasks.generate.build_messages",
              side_effect=ValueError("standard path reached")),
        patch.object(generate_guide, "retry", side_effect=Retry()),
    ):
        with pytest.raises(ValueError, match="standard path reached"):
            generate_guide(1)

    mock_structured.assert_not_called()
    assert job.meta.get("path") == "standard"


# ── _stubs_to_platform_guide — music_cue defaulting ──────────────────────────

def _stub(index, beat_type, vo="Some voiceover line here."):
    return BeatStub(
        index=index, beat_type=beat_type, section="", player="",
        vo_script=vo, duration_s=5.0, on_screen_text=["Text"],
    )


def test_structured_path_hook_beat_gets_a_default_music_cue():
    """Structured-script beats never carry an LLM-written music_cue — without a
    default, render_cut would never find a cue to look up a music track for and
    the structured path would silently never get background music.
    """
    stubs = [_stub(0, "hook"), _stub(1, "body"), _stub(2, "cta")]
    guide = _stubs_to_platform_guide(
        stubs, visuals={}, platform="youtube_shorts", target_length_s=30.0,
        caption="Caption", hashtags=["tag"] * 6,
    )
    assert guide.beats[0].type == "hook"
    assert guide.beats[0].music_cue == "upbeat energetic"


def test_structured_path_non_hook_beats_have_no_music_cue():
    """Only one cue is needed — render_cut picks the first non-empty cue across
    all beats for the whole cut's music track, so body/cta beats stay None.
    """
    stubs = [_stub(0, "hook"), _stub(1, "body"), _stub(2, "cta")]
    guide = _stubs_to_platform_guide(
        stubs, visuals={}, platform="youtube_shorts", target_length_s=30.0,
        caption="Caption", hashtags=["tag"] * 6,
    )
    assert guide.beats[1].music_cue is None
    assert guide.beats[2].music_cue is None
