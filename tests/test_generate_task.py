"""Tests for the generate_guide task's failure handling — missing rows and retries."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry

from api import models
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
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


def test_the_soft_time_limit_in_the_structured_path_does_not_fall_back_to_the_standard_path():
    """The structured path catches Exception and falls back to a second, paid LLM path; that would
    swallow the runtime limit and keep running to the hard kill."""
    from celery.exceptions import SoftTimeLimitExceeded
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider"),
        patch("worker.tasks.generate.script_parser.parse", return_value=[MagicMock()]),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script", side_effect=SoftTimeLimitExceeded()),
        patch("worker.tasks.generate.build_messages") as standard_path,
    ):
        with pytest.raises(SoftTimeLimitExceeded):
            generate_guide(1)

    standard_path.assert_not_called()
    assert job.status == models.JobStatus.failed


def test_generate_caption_hashtags_does_not_swallow_the_soft_time_limit():
    """A bare except Exception around the blocking llm.complete() call would silently launder a
    runtime-limit breach into 'no caption, use the fallback template' and let the task run on to a
    normal-looking completion instead of failing visibly."""
    from celery.exceptions import SoftTimeLimitExceeded
    from worker.tasks.generate import _generate_caption_hashtags

    llm = MagicMock()
    llm.complete.side_effect = SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        _generate_caption_hashtags([], "football", llm)


def test_generate_caption_hashtags_still_falls_back_on_an_ordinary_error():
    from worker.tasks.generate import _generate_caption_hashtags

    llm = MagicMock()
    llm.complete.side_effect = ValueError("bad json")
    assert _generate_caption_hashtags([], "football", llm) == ("", [])


# ── §3.5 performance-note wiring regressions ─────────────────────────────────
# Both tests below were written first against the naive/buggy implementation
# (a plain `feedback = [...]` replace for #1; notes queried inside the
# standard-path-only `if guide is None:` block for #2), confirmed to fail
# against it, then confirmed to pass against the fix — the same
# mutation-testing discipline this codebase's history already uses (see
# CLAUDE.md's reap_stuck_jobs done-orphan sweep). See
# docs/specs/2026-09-phase5-quality-engagement-feedback.md §3.5 and §7.

def _query_dispatch(notes=None, cuts=None):
    """db.query(Model).filter(...).all() dispatch by Model, for a MagicMock db."""
    notes = notes if notes is not None else []
    cuts = cuts if cuts is not None else [_cut()]

    def _side_effect(model):
        q = MagicMock()
        if model is models.PerformanceNote:
            q.filter.return_value.all.return_value = notes
        elif model is models.Cut:
            q.filter.return_value.all.return_value = cuts
        else:
            q.filter.return_value.all.return_value = []
        return q

    return _side_effect


def _valid_guide_raw() -> str:
    """A minimal but schema-valid MasterGuide JSON string, matching the single
    youtube_shorts cut _cut() produces, for llm.complete() to return."""
    return json.dumps({
        "title": "Test guide",
        "niche": "football",
        "cuts": [{
            "platform": "youtube_shorts",
            "target_length_s": 45.0,
            "beats": [
                {"index": 0, "type": "hook", "duration_s": 5, "visual_direction": "stadium wide shot",
                 "on_screen_text": ["Hook"], "vo_script": "Could this be the biggest upset yet?"},
                {"index": 1, "type": "body", "duration_s": 10, "visual_direction": "training ground clip",
                 "on_screen_text": ["Body"], "vo_script": "The squad has been preparing all week for this."},
                {"index": 2, "type": "cta", "duration_s": 5, "visual_direction": "crowd celebration",
                 "on_screen_text": ["CTA"], "vo_script": "Drop your prediction below."},
            ],
            "caption": "Big match preview.",
            "hashtags": ["football", "soccer", "sports", "matchday", "preview"],
        }],
    })


def test_seeded_performance_notes_survive_past_attempt_1_on_retry():
    """The retry-replace bug: `feedback = [...]` (plain replace) on a failed attempt
    would silently drop performance notes seeded before attempt 1. Force attempt 1
    to score below threshold and attempt 2 to succeed; the seeded notes' text must
    still be present in the build_messages() call for attempt 2, not just attempt 1.
    """
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    notes = [
        SimpleNamespace(id=1, text="Direct-question hooks outperform statement hooks."),
        SimpleNamespace(id=2, text="Keep the CTA under 8 seconds."),
    ]
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=notes)

    def _combined_score_side_effect(rule_s, rule_i, guide, context, db=None, reel_id=None, attempt=None):
        if attempt == 1:
            return 30, ["Weak hook — add a question or direct address"]
        return 90, []

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.script_parser.parse", return_value=None),  # force standard path
        patch("worker.tasks.generate.get_llm_provider") as mock_get_llm,
        patch("worker.tasks.generate.build_messages", return_value=[{"role": "system", "content": "x"}]) as mock_build_messages,
        patch("worker.tasks.generate.score_guide", return_value=(0, [])),
        patch("worker.tasks.generate._combined_score", side_effect=_combined_score_side_effect),
        patch("worker.tasks.generate._enrich_standard_path_guide"),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
    ):
        mock_get_llm.return_value.complete.return_value = _valid_guide_raw()
        generate_guide(1)

    assert job.status == models.JobStatus.done
    assert mock_build_messages.call_count >= 2

    second_call_kwargs = mock_build_messages.call_args_list[1].kwargs
    prior_feedback = second_call_kwargs.get("prior_feedback") or []
    for note in notes:
        assert note.text in prior_feedback, (
            f"seeded note {note.text!r} missing from attempt-2 prior_feedback — "
            "the retry-replace bug dropped it"
        )


def test_structured_path_success_does_not_nameerror_on_performance_notes():
    """The NameError bug: active_notes_rows must be queried unconditionally at the
    top of generate_guide, not inside the `if guide is None:` (standard-path-only)
    branch — the shared job.meta write at the end of the function is reached by
    BOTH paths. Run the structured path to a threshold-clearing success with
    active PerformanceNotes present and assert the job completes `done` (not an
    unhandled NameError) with job.meta["performance_note_ids"] set correctly.
    """
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    notes = [SimpleNamespace(id=7, text="Some performance note.")]
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.side_effect = _query_dispatch(notes=notes, cuts=[_cut()])

    structured_guide = MasterGuide(
        title="Structured guide",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45.0,
            caption="Caption",
            hashtags=["football"] * 6,
            beats=[
                Beat(index=0, type="hook", duration_s=5, visual_direction="v",
                     on_screen_text=["h"], vo_script="Could this be the biggest upset yet?"),
                Beat(index=1, type="body", duration_s=10, visual_direction="v",
                     on_screen_text=["b"], vo_script="Body content here."),
                Beat(index=2, type="cta", duration_s=5, visual_direction="v",
                     on_screen_text=["c"], vo_script="Drop your prediction below."),
            ],
        )],
    )

    with (
        patch("worker.tasks.common.SessionLocal", return_value=db),
        patch("worker.tasks.generate.paid_call_count", return_value=0),
        patch("worker.tasks.generate.get_llm_provider"),
        patch("worker.tasks.generate.script_parser.parse", return_value=[MagicMock()]),
        patch("worker.tasks.generate.resolve_generation_path", return_value="structured"),
        patch("worker.tasks.generate._generate_from_structured_script", return_value=structured_guide),
        patch("worker.tasks.generate.score_guide", return_value=(90, [])),
        patch("worker.tasks.generate._combined_score", return_value=(90, [])),
        patch("worker.tasks.generate.generate_hook_variants", return_value=[]),
        patch("worker.tasks.generate.build_messages") as mock_build_messages,
    ):
        generate_guide(1)

    mock_build_messages.assert_not_called()  # never falls through to the standard path
    assert job.status == models.JobStatus.done
    assert job.meta.get("performance_note_ids") == [7]
    assert job.meta.get("quality_score") == 90
