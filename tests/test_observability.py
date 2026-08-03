"""Tests for engine/observability.py — paid_call_count() budget-cap helper."""
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
