"""Tests for engine/observability.py — paid_call_count() budget-cap helper."""
import pytest

from api import models
from engine.observability import paid_call_count


def test_paid_call_count_counts_only_nvidia_provider(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db.add(reel)
    db.flush()
    db.add(models.StageEvent(reel_id=reel.id, stage="generate", provider="nvidia"))
    db.add(models.StageEvent(reel_id=reel.id, stage="generate", provider="nvidia"))
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", provider="ollama"))
    db.add(models.StageEvent(reel_id=reel.id, stage="enrich", provider=None))
    db.commit()

    assert paid_call_count(db, reel.id) == 2


def test_paid_call_count_scoped_to_reel(db_session):
    db = db_session
    reel_a = models.Reel(context="a", status=models.ReelStatus.generating)
    reel_b = models.Reel(context="b", status=models.ReelStatus.generating)
    db.add_all([reel_a, reel_b])
    db.flush()
    db.add(models.StageEvent(reel_id=reel_a.id, stage="generate", provider="nvidia"))
    db.add(models.StageEvent(reel_id=reel_b.id, stage="generate", provider="nvidia"))
    db.commit()

    assert paid_call_count(db, reel_a.id) == 1
    assert paid_call_count(db, reel_b.id) == 1


def test_paid_call_count_zero_for_reel_with_no_events(db_session):
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.draft)
    db.add(reel)
    db.commit()
    assert paid_call_count(db, reel.id) == 0


# ── record_stage must not swallow the task's soft time limit ─────────────────────────────────

def test_record_stage_a_soft_limit_during_the_final_commit_propagates_and_rolls_back():
    """The `finally` commit used to be `except Exception: db.rollback()`: a Celery soft limit landing
    in that window (an Exception subclass) was eaten and the task carried on past its time limit."""
    from unittest.mock import MagicMock

    from celery.exceptions import SoftTimeLimitExceeded

    from engine.observability import record_stage

    db = MagicMock()
    db.commit.side_effect = SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        with record_stage(db, 1, "composite"):
            pass
    db.rollback.assert_called_once()                 # the session is left clean for the failure stamp


def test_record_stage_an_ordinary_commit_failure_is_still_swallowed():
    from unittest.mock import MagicMock

    from engine.observability import record_stage

    db = MagicMock()
    db.commit.side_effect = RuntimeError("db hiccup")
    with record_stage(db, 1, "composite"):           # observability must never fail the work it observes
        pass
    db.rollback.assert_called_once()


def test_record_stage_a_soft_limit_inside_the_block_is_recorded_and_re_raised():
    from unittest.mock import MagicMock

    from celery.exceptions import SoftTimeLimitExceeded

    from engine.observability import record_stage

    db = MagicMock()
    with pytest.raises(SoftTimeLimitExceeded):
        with record_stage(db, 1, "composite") as ev:
            raise SoftTimeLimitExceeded()
    assert ev.ok is False and "SoftTimeLimitExceeded" in ev.detail["error"]
    db.commit.assert_called_once()


def test_record_stage_a_soft_limit_in_the_commit_replaces_an_error_already_propagating():
    """The time limit is the more important signal: it wins over the body's own failure."""
    from unittest.mock import MagicMock

    from celery.exceptions import SoftTimeLimitExceeded

    from engine.observability import record_stage

    db = MagicMock()
    db.commit.side_effect = SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        with record_stage(db, 1, "composite"):
            raise ValueError("the work itself failed")
