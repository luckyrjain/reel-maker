"""Tests for resolve_or_reuse — the per-beat asset pinning ledger."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import models
from engine.render.asset_sourcer import SourcedAsset, _fp, resolve_or_reuse


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def cut(db):
    reel = models.Reel(context="Argentina squad review.", status=models.ReelStatus.draft)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id,
        platform=models.CutPlatform.youtube_shorts,
        target_length_s=45.0,
        status=models.CutStatus.draft,
    )
    db.add(cut)
    db.flush()
    return cut


class _StubSourcer:
    """Stands in for PexelsVideoSource — returns a distinct asset per query."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.queries = []

    def search(self, query, min_duration_s):
        self.queries.append(query)
        ref = f"vid_{len(self.queries)}"
        return SourcedAsset(
            source="pexels",
            source_ref=ref,
            local_path=self.tmp_path / f"{ref}.mp4",
            license_str="pexels_free",
            safe_to_publish=True,
            duration_s=10.0,
        )


def _pins(db, cut):
    return (
        db.query(models.CutAsset)
        .filter(models.CutAsset.cut_id == cut.id, models.CutAsset.beat_index == 0)
        .all()
    )


def test_first_resolve_pins_the_asset(db, cut, tmp_path):
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)

    pins = _pins(db, cut)
    assert len(pins) == 1
    assert pins[0].resolved_from == _fp("Romero tackle")
    assert sourcer.queries == ["Romero tackle"]


def test_unchanged_direction_reuses_pin_without_calling_the_api(db, cut, tmp_path):
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)

    assert sourcer.queries == ["Romero tackle"], "second call must not hit the sourcer"
    assert len(_pins(db, cut)) == 1


def test_changed_direction_replaces_the_pin_rather_than_duplicating(db, cut, tmp_path):
    """The re-pin path must delete the stale row — a duplicate violates uq_cut_beat_order."""
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Messi through-ball",
                     min_duration_s=5.0, sourcer=sourcer)

    pins = _pins(db, cut)
    assert len(pins) == 1, "stale pin must be deleted, not left alongside the new one"
    assert pins[0].resolved_from == _fp("Messi through-ball")
    assert sourcer.queries == ["Romero tackle", "Messi through-ball"]


def test_other_beats_pins_are_untouched(db, cut, tmp_path):
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=1, visual_direction="Messi through-ball",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero header",
                     min_duration_s=5.0, sourcer=sourcer)

    beat_1 = (
        db.query(models.CutAsset)
        .filter(models.CutAsset.cut_id == cut.id, models.CutAsset.beat_index == 1)
        .all()
    )
    assert len(beat_1) == 1
    assert beat_1[0].resolved_from == _fp("Messi through-ball")
