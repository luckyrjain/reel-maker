"""engine/render/asset_sourcer.py — the pin ledger, the persisted fingerprint formats, and
LocalMusicSource.

Closes test debt found by an independent mutation review: every test below is aimed at a
mutant of asset_sourcer.py that survived the existing suite (the mutation each one kills is
named in its comment). Production code is untouched.

Three groups:
  1. resolve_or_reuse() — the per-cut / per-beat CutAsset pin ledger and argument forwarding.
  2. _fp / compute_pins_fingerprint / compute_pins_fingerprint_for_render — the formats are
     persisted (cut_assets.resolved_from, cuts.rendered_pins_fingerprint), so they are pinned
     literally instead of through the function under test.
  3. LocalMusicSource — directory/file handling, tie-break, extension set, keyword rules.
"""
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Query

from api import models
from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    EMPTY_PINS_FINGERPRINT,
    LocalMusicSource,
    SourcedAsset,
    _fp,
    compute_pins_fingerprint,
    compute_pins_fingerprint_for_render,
    resolve_or_reuse,
)

_LOG = "engine.render.asset_sourcer"


# ---- fixtures / fakes ----------------------------------------------------------------------
@pytest.fixture
def cuts(db_session):
    """Two cuts of one reel whose cut ids differ from their reel id, so a mix-up between
    cut.id and cut.reel_id cannot hide behind equal values."""
    db = db_session
    pad = models.Reel(context="pad", status=models.ReelStatus.draft)
    reel = models.Reel(context="real", status=models.ReelStatus.draft)
    db.add_all([pad, reel])
    db.flush()

    def mk(r):
        return models.Cut(
            reel_id=r.id, platform=models.CutPlatform.youtube_shorts,
            target_length_s=45.0, status=models.CutStatus.draft,
        )

    # Two padding cuts first, so the cuts under test get ids 3 and 4 while their reel is id 2.
    db.add_all([mk(pad), mk(pad)])
    db.flush()
    c1, c2 = mk(reel), mk(reel)
    db.add_all([c1, c2])
    db.flush()
    assert c1.id != c1.reel_id and c2.id != c2.reel_id
    return c1, c2


def _sa(source="pexels", ref="r", **kw):
    return SourcedAsset(
        source=source, source_ref=ref, local_path=Path("/x/" + ref), license_str="l",
        duration_s=kw.pop("duration_s", 1.0), **kw,
    )


class _Pex:
    """Pexels stand-in: records (query, min_duration) and returns a distinct asset per call."""

    def __init__(self):
        self.calls = []

    def search(self, query, min_duration_s):
        self.calls.append((query, min_duration_s))
        return _sa("pexels", f"v{len(self.calls)}", safe_to_publish=True)


class _NoneSrc:
    def search(self, *args, **kwargs):
        return None


class _HF:
    """HF stand-in: has an api_key and reports a real generation, so the gated path runs."""

    def __init__(self, source):
        self.src = source
        self.api_key = "k"
        self.last_call_was_generated = True
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return _sa(self.src, "h-" + self.src, safe_to_publish=True, duration_s=2.0)


def _asset(db, ref):
    a = models.Asset(
        type="footage", source="pexels", source_ref=ref, local_path=f"/x/{ref}.mp4",
        license="pexels_free", safe_to_publish=True,
    )
    db.add(a)
    db.flush()
    return a


def _pin(db, cut, beat, order, asset, fingerprint):
    pin = models.CutAsset(
        cut_id=cut.id, asset_id=asset.id, role="footage", beat_index=beat,
        order_in_beat=order, resolved_from=fingerprint,
    )
    db.add(pin)
    db.flush()
    return pin


# ==== 1. resolve_or_reuse: scoping =========================================================
def test_another_cuts_pin_for_the_same_beat_is_neither_reused_nor_deleted(db_session, cuts):
    c1, c2 = cuts
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "x", 5.0, s)
    resolve_or_reuse(db_session, c2, 0, "x", 5.0, s)
    assert len(s.calls) == 2                      # kills: cut_id filter dropped from the read
    resolve_or_reuse(db_session, c2, 0, "y", 5.0, s)   # re-pins c2's beat 0
    assert db_session.query(models.CutAsset).filter_by(cut_id=c1.id).count() == 1
    #                                               kills: cut_id filter dropped from the delete


def test_a_stale_pin_on_another_beat_does_not_block_reuse(db_session, cuts):
    c1, _ = cuts
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "a", 5.0, s)
    resolve_or_reuse(db_session, c1, 1, "b", 5.0, s)
    resolve_or_reuse(db_session, c1, 0, "a", 5.0, s)   # beat 1's pin has a different fingerprint
    assert [q for q, _ in s.calls] == ["a", "b"]       # kills: beat_index filter dropped from the read


def test_re_resolving_a_beat_leaves_other_beats_pins_alone(db_session, cuts):
    c1, _ = cuts
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "a", 5.0, s)
    resolve_or_reuse(db_session, c1, 1, "b", 5.0, s)
    resolve_or_reuse(db_session, c1, 0, "a2", 5.0, s)  # beat 0 changes; beat 1 untouched
    beats = sorted(p.beat_index for p in db_session.query(models.CutAsset).filter_by(cut_id=c1.id))
    assert beats == [0, 1]                             # kills: beat_index filter dropped from the delete


# ==== 1. resolve_or_reuse: reuse path ======================================================
@pytest.mark.parametrize("fingerprints", [("fresh", "stale"), ("stale", "fresh")])
def test_any_stale_pin_in_a_beat_forces_a_re_resolve(db_session, cuts, fingerprints):
    c1, _ = cuts
    current = _fp("dir")
    for order, tag in enumerate(fingerprints):
        _pin(db_session, c1, 0, order, _asset(db_session, f"a{order}"),
             current if tag == "fresh" else "old")
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "dir", 5.0, s)
    assert len(s.calls) == 1        # kills: all() -> any(); checking only pinned[0] or pinned[-1]


@pytest.mark.parametrize("insert_order", [(0, 1), (1, 0)])
def test_reuse_returns_pins_in_order_in_beat_order_as_paths(db_session, cuts, insert_order):
    c1, _ = cuts
    current = _fp("dir")
    assets = {0: _asset(db_session, "first"), 1: _asset(db_session, "second")}
    for order in insert_order:      # both insertion orders: id asc and id desc differ from order_in_beat
        _pin(db_session, c1, 0, order, assets[order], current)
    s = _Pex()
    out = resolve_or_reuse(db_session, c1, 0, "dir", 5.0, s)
    assert s.calls == []
    assert [a.source_ref for a, _ in out] == ["first", "second"]   # kills: order_by desc / order_by id
    assert all(isinstance(p, Path) for _, p in out)                # kills: Path() dropped
    assert [p for _, p in out] == [Path("/x/first.mp4"), Path("/x/second.mp4")]


def test_a_pin_whose_asset_row_is_gone_is_re_resolved_not_returned_empty(db_session, cuts):
    c1, _ = cuts
    ghost = models.Asset(id=9999, type="footage", source="pexels", source_ref="g", local_path="/g")
    db_session.add(ghost)
    db_session.flush()
    _pin(db_session, c1, 0, 0, ghost, _fp("dir"))
    # A later pin on another beat keeps sqlite from reusing the ghost pin's rowid for the re-pin
    # (which would trip an identity-map SAWarning unrelated to what is under test).
    _pin(db_session, c1, 7, 0, _asset(db_session, "other"), _fp("other"))
    db_session.query(models.Asset).filter_by(id=9999).delete()   # sqlite does not enforce the FK
    db_session.commit()
    s = _Pex()
    out = resolve_or_reuse(db_session, c1, 0, "dir", 5.0, s)
    assert len(s.calls) == 1 and out[0][0] is not None
    #                       kills: `if asset` guard removed / unconditional `return results`


def test_fingerprint_is_not_whitespace_normalised(db_session, cuts):
    c1, _ = cuts
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "dir ", 5.0, s)
    resolve_or_reuse(db_session, c1, 0, "dir", 5.0, s)
    assert len(s.calls) == 2                         # kills: _fp(visual_direction.strip())


# ==== 1. resolve_or_reuse: pin rows / sentinel / pass-through ==============================
def test_nothing_found_returns_sentinel_and_writes_no_pin(db_session, cuts):
    c1, _ = cuts
    assert resolve_or_reuse(db_session, c1, 0, "d", 5.0, _NoneSrc()) == [(None, None)]
    assert db_session.query(models.CutAsset).count() == 0   # kills: pin written for a None asset


def test_two_item_beat_pins_start_at_order_zero(db_session, cuts):
    c1, _ = cuts

    class Wiki:
        def search(self, name):
            return _sa("wikipedia", "w-" + name.split()[0], safe_to_publish=True)

    with patch(f"{_LOG}.time.sleep"):
        out = resolve_or_reuse(
            db_session, c1, 0, "Lionel Messi and Cristian Romero", 5.0, _NoneSrc(), wiki=Wiki(),
        )
    pins = db_session.query(models.CutAsset).order_by(models.CutAsset.order_in_beat).all()
    assert [p.order_in_beat for p in pins] == [0, 1]        # kills: enumerate(results, 1)
    assert [a.source_ref for a, _ in out] == ["w-Lionel", "w-Cristian"]
    assert {p.resolved_from for p in pins} == {_fp("Lionel Messi and Cristian Romero")}


def test_nothing_found_still_drops_the_stale_pins_of_that_beat(db_session, cuts):
    c1, _ = cuts
    _pin(db_session, c1, 0, 0, _asset(db_session, "old"), "old-fingerprint")
    assert resolve_or_reuse(db_session, c1, 0, "new", 5.0, _NoneSrc()) == [(None, None)]
    assert db_session.query(models.CutAsset).filter_by(cut_id=c1.id, beat_index=0).count() == 0
    #                                       kills: stale-pin delete skipped when nothing was found


def test_stale_pin_survives_a_resolve_that_raises(db_session, cuts):
    """Resolve first, delete second: a crash mid-resolve must leave the old pin in place."""
    c1, _ = cuts
    _pin(db_session, c1, 0, 0, _asset(db_session, "old"), "old-fingerprint")

    class Boom:
        def search(self, *args, **kwargs):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        resolve_or_reuse(db_session, c1, 0, "new", 5.0, Boom())
    db_session.rollback()
    pins = db_session.query(models.CutAsset).filter_by(cut_id=c1.id).all()
    assert [p.resolved_from for p in pins] == ["old-fingerprint"]   # kills: delete moved above resolve


def test_reuse_leaves_the_existing_pin_rows_untouched(db_session, cuts):
    c1, _ = cuts
    first = _pin(db_session, c1, 0, 0, _asset(db_session, "a"), _fp("dir"))
    second = _pin(db_session, c1, 5, 0, _asset(db_session, "b"), _fp("other"))   # keeps rowids from being reused
    ids = {first.id, second.id}
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "dir", 5.0, s)
    assert s.calls == []
    assert {p.id for p in db_session.query(models.CutAsset).filter_by(cut_id=c1.id)} == ids
    #                                       kills: reuse path deleting and re-inserting the pins


def test_written_pin_binds_cut_beat_asset_role_and_fingerprint(db_session, cuts):
    c1, _ = cuts
    _asset(db_session, "pad")                         # so the pinned asset's id is not 1
    out = resolve_or_reuse(db_session, c1, 3, "dir", 5.0, _Pex())
    (pin,) = db_session.query(models.CutAsset).all()
    assert (pin.cut_id, pin.beat_index, pin.order_in_beat) == (c1.id, 3, 0)
    assert pin.asset_id == out[0][0].id and pin.asset_id != 1               # kills: asset_id swapped / hard-coded
    assert pin.role == "footage"                      # kills: role changed
    assert pin.resolved_from == _fp("dir")


def test_no_transaction_is_left_open_across_a_source_call(db_session, cuts):
    c1, _ = cuts
    # Render workers open sessions with expire_on_commit=False; with the fixture's default, reading
    # cut.reel_id after the function's own commit would lazily reload and reopen a transaction.
    db_session.expire_on_commit = False
    db_session.commit()                               # end the fixture's setup transaction

    class Probe:
        in_tx = []

        def search(self, query, min_duration_s):
            self.in_tx.append(db_session.in_transaction())
            return _sa("pexels", "p", safe_to_publish=True)

    probe = Probe()
    resolve_or_reuse(db_session, c1, 0, "dir", 5.0, probe)
    assert probe.in_tx == [False]                     # kills: the read-transaction commit removed


def test_resolve_or_reuse_forwards_every_argument(db_session, cuts):
    c1, _ = cuts
    s = _Pex()
    resolve_or_reuse(db_session, c1, 0, "d", 7.5, s)
    assert s.calls == [("d", 7.5)]                          # kills: min_duration_s -> 0.0 / dropped

    hv, hi = _HF("huggingface_video"), _HF("huggingface")
    resolve_or_reuse(db_session, c1, 1, "e ", 5.0, _NoneSrc(), hf_video=hv)
    assert hv.prompts == ["e "]                             # kills: hf_video not forwarded
    resolve_or_reuse(db_session, c1, 2, "f", 5.0, _NoneSrc(), hf=hi)
    assert hi.prompts == ["f"]                              # kills: hf not forwarded

    events = db_session.query(models.StageEvent).all()
    assert events and {e.reel_id for e in events} == {c1.reel_id}
    #                                                       kills: reel_id=None / reel_id=cut.id


# ==== 2. fingerprints: persisted formats ===================================================
def test_empty_pins_sentinel_literal_is_stable_because_it_is_persisted():
    assert EMPTY_PINS_FINGERPRINT == "no-pins-bound"
    assert len(EMPTY_PINS_FINGERPRINT) != 64         # never the shape of a real sha256 hex


def test_fp_golden_value_is_sha256_first_16_hex():
    """Every other test uses _fp() as its own oracle, so the algorithm was unpinned although
    it is persisted in cut_assets.resolved_from."""
    assert _fp("Romero tackle") == hashlib.sha256(b"Romero tackle").hexdigest()[:16]
    assert _fp("Romero tackle") == "4a2ccb8192a7da8c"       # literal: also guards the expression above
    assert len(_fp("")) == 16


def test_pins_fingerprint_golden_value(db_session, cuts):
    c1, _ = cuts
    first, second = _asset(db_session, "a"), _asset(db_session, "b")
    _pin(db_session, c1, 1, 0, first, "x")
    _pin(db_session, c1, 0, 0, second, "x")
    canonical = f"0:0:{second.id}|1:0:{first.id}"           # sorted (beat, order, asset), "|"-joined
    full = hashlib.sha256(canonical.encode()).hexdigest()
    assert compute_pins_fingerprint(db_session, c1.id) == full
    assert len(full) == 64


def test_pins_fingerprint_sorts_numerically_by_beat_then_order_then_asset(db_session, cuts):
    c1, _ = cuts
    a, b, c = _asset(db_session, "a"), _asset(db_session, "b"), _asset(db_session, "c")
    _pin(db_session, c1, 10, 0, a, "x")
    _pin(db_session, c1, 2, 1, b, "x")
    _pin(db_session, c1, 2, 0, c, "x")
    # numeric tuple order: beat 2 before beat 10, order 0 before order 1 (a string sort puts "10:" first)
    canonical = f"2:0:{c.id}|2:1:{b.id}|10:0:{a.id}"
    assert compute_pins_fingerprint(db_session, c1.id) == hashlib.sha256(canonical.encode()).hexdigest()


def test_pins_fingerprint_sorts_in_python_even_if_the_db_returns_rows_unordered(db_session, cuts):
    """The old 'order independent' test was vacuous on sqlite: the unique index
    (cut_id, beat_index, order_in_beat) returns rows in index order whatever the insert order,
    so removing sorted() still passed. Force a reversed result set instead."""
    c1, _ = cuts
    _pin(db_session, c1, 0, 0, _asset(db_session, "a"), "x")
    _pin(db_session, c1, 1, 0, _asset(db_session, "b"), "x")
    want = compute_pins_fingerprint(db_session, c1.id)
    real_all = Query.all
    with patch.object(Query, "all", lambda self: list(reversed(real_all(self)))):
        assert compute_pins_fingerprint(db_session, c1.id) == want   # kills: sorted() dropped


def test_pins_fingerprint_ignores_other_cuts_and_sees_each_key_field(db_session, cuts):
    c1, c2 = cuts
    a, b = _asset(db_session, "a"), _asset(db_session, "b")
    _pin(db_session, c2, 0, 0, a, "x")
    assert compute_pins_fingerprint(db_session, c1.id) is None      # kills: cut_id filter dropped
    _pin(db_session, c1, 0, 0, a, "x")
    base = compute_pins_fingerprint(db_session, c1.id)
    assert base == compute_pins_fingerprint(db_session, c2.id)      # same pin set, same fingerprint
    # each of (beat_index, order_in_beat, asset_id) alone must change it
    for beat, order, asset in ((1, 0, a), (0, 1, a), (0, 0, b)):
        db_session.query(models.CutAsset).filter_by(cut_id=c1.id).delete()
        _pin(db_session, c1, beat, order, asset, "x")
        assert compute_pins_fingerprint(db_session, c1.id) != base


def test_for_render_wrapper_returns_the_real_hash_or_the_sentinel(db_session, cuts):
    c1, _ = cuts
    assert compute_pins_fingerprint(db_session, c1.id) is None
    assert compute_pins_fingerprint_for_render(db_session, c1.id) == "no-pins-bound"
    _pin(db_session, c1, 0, 0, _asset(db_session, "a"), "x")
    real = compute_pins_fingerprint(db_session, c1.id)
    assert compute_pins_fingerprint_for_render(db_session, c1.id) == real != EMPTY_PINS_FINGERPRINT


# ==== 3. LocalMusicSource ==================================================================
def _track(d, name):
    p = d / name
    p.write_bytes(b"")
    return p


def test_music_library_path_that_is_a_file_is_none_not_a_crash(tmp_path):
    f = _track(tmp_path, "lib.mp3")
    assert LocalMusicSource(f).find("tense") is None      # kills: is_dir() -> exists() (NotADirectoryError)


def test_music_missing_library_and_blank_cue_are_none(tmp_path):
    assert LocalMusicSource(tmp_path / "nope").find("tense") is None
    _track(tmp_path, "tense.mp3")
    assert LocalMusicSource(tmp_path).find("   ") is None
    assert LocalMusicSource(tmp_path).find(None) is None


def test_music_tie_goes_to_the_alphabetically_first_track_regardless_of_listing_order(tmp_path):
    a, b = _track(tmp_path, "a_tense.mp3"), _track(tmp_path, "b_tense.mp3")
    assert LocalMusicSource(tmp_path).find("tense") == a            # kills: sorted(reverse=True)
    with patch.object(Path, "iterdir", lambda self: iter([b, a])):
        assert LocalMusicSource(tmp_path).find("tense") == a        # kills: sorted() dropped


def test_music_higher_overlap_beats_alphabetical_order(tmp_path):
    _track(tmp_path, "a_tense.mp3")
    best = _track(tmp_path, "z_tense_minimal.mp3")
    assert LocalMusicSource(tmp_path).find("tense minimal") == best


def test_music_directory_named_like_a_track_is_skipped(tmp_path):
    (tmp_path / "tense.mp3").mkdir()
    assert LocalMusicSource(tmp_path).find("tense") is None         # kills: is_file() dropped


@pytest.mark.parametrize("name", ["tense.MP3", "tense.m4a", "tense.flac", "tense.WAV", "tense.ogg"])
def test_music_extension_set_is_case_insensitive_and_includes_every_audio_type(tmp_path, name):
    p = _track(tmp_path, name)
    assert LocalMusicSource(tmp_path).find("tense") == p            # kills: .lower() dropped; set members removed


def test_music_does_not_descend_into_subdirectories(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    _track(sub, "tense.mp3")
    assert LocalMusicSource(tmp_path).find("tense") is None         # kills: iterdir() -> rglob("*")


def test_music_non_audio_extension_is_ignored(tmp_path):
    _track(tmp_path, "tense.txt")
    assert LocalMusicSource(tmp_path).find("tense") is None


def test_music_match_ignores_the_extension_text(tmp_path):
    _track(tmp_path, "zzz.flac")
    assert LocalMusicSource(tmp_path).find("flac sound") is None    # kills: _keywords(path.name) for path.stem


def test_music_keywords_split_on_digits_and_ignore_case_and_need_three_letters(tmp_path):
    p = _track(tmp_path, "tense01.mp3")
    assert LocalMusicSource(tmp_path).find("TENSE") == p            # kills: [^a-zA-Z0-9] split; .lower() dropped
    assert LocalMusicSource._keywords("go sad") == {"sad"}          # kills: len > 1 and len > 3
