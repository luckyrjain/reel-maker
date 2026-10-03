"""Cache, fallback-chain and adapter-detail contracts for engine/render/asset_sourcer.py.

Third pass, after an independent mutation review found behaviors the first two test files left
unpinned: `_cache_asset`'s lookup/heal rules (the heal decides `safe_to_publish`, which the
publish gate enforces), `resolve_beat_assets`' ordering and argument forwarding, and a few
Pexels / Wikipedia / HuggingFace adapter details. Tests marked "characterization" pin current
behavior that is not necessarily intended. Out of scope here (separate test debt): the
`resolve_or_reuse` pin ledger, the pins fingerprint, and the local music source.
"""
import logging
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from api import models
from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    HuggingFaceImageSource,
    HuggingFaceVideoSource,
    SourcedAsset,
    _cache_asset,
    resolve_beat_assets,
)
from tests.test_sourcer_selection import _MOD, _bytes_resp, _hf_resp, _pexels, _video, _wiki

_LOG = "engine.render.asset_sourcer"


def _sa(source="pexels", ref="r", **kw):
    return SourcedAsset(source=source, source_ref=ref, local_path=Path("/x/" + ref),
                        license_str="l", duration_s=kw.pop("duration_s", 1.0), **kw)


class _Pex:
    def __init__(self):
        self.calls = []

    def search(self, q, d):
        self.calls.append((q, d))
        return _sa("pexels", f"v{len(self.calls)}", safe_to_publish=True)


class _NoneSrc:
    def search(self, q, d):
        return None


class _HF:
    def __init__(self, source, key="k"):
        self.src, self.api_key, self.last_call_was_generated, self.prompts = source, key, True, []

    def generate(self, p):
        self.prompts.append(p)
        return _sa(self.src, "h-" + self.src, safe_to_publish=True, duration_s=2.0)


# ---- _cache_asset ---------------------------------------------------------------------------
def test_cache_asset_lookup_needs_both_source_and_ref(db_session):
    a, _ = _cache_asset(db_session, _sa("pexels", "1"), "footage")
    b, _ = _cache_asset(db_session, _sa("wikipedia", "1"), "photo")           # kills dropping the source filter
    assert a.id != b.id and b.source == "wikipedia"


def test_cache_asset_new_row_keeps_url_and_attribution(db_session):
    a, _ = _cache_asset(db_session, _sa("wikipedia", "1", license_url="https://u", attribution="Jane"), "photo")
    assert (a.license_url, a.attribution) == ("https://u", "Jane")    # kills hard-coded None for either


def test_heal_only_when_existing_has_no_url_and_result_has_one(db_session):
    # existing without url must NOT be overwritten by a result that also has no url
    a, _ = _cache_asset(db_session, _sa("hf", "1", safe_to_publish=True, attribution="A"), "photo")
    _cache_asset(db_session, _sa("hf", "1", safe_to_publish=False, attribution="B"), "photo")
    assert (a.safe_to_publish, a.attribution) == (True, "A")          # kills `or` / `not existing.license_url` only
    # existing WITH a url must not be replaced by a different url
    b, _ = _cache_asset(db_session, _sa("wikipedia", "2", license_url="https://A", attribution="X"), "photo")
    _cache_asset(db_session, _sa("wikipedia", "2", license_url="https://B", attribution="Y", safe_to_publish=True), "photo")
    assert (b.license_url, b.attribution, b.safe_to_publish) == ("https://A", "X", False)   # kills `if result.license_url:`


def test_heal_copies_safe_to_publish_false_and_leaves_license_and_path_alone(db_session):
    a, _ = _cache_asset(db_session, SourcedAsset("wikipedia", "3", Path("/x/old.jpg"), "unknown", 0.0), "photo")
    _cache_asset(db_session, SourcedAsset("wikipedia", "3", Path("/x/new.jpg"), "CC BY-SA 4.0", 0.0,
                                  license_url="https://u", attribution="J", safe_to_publish=False), "photo")
    assert a.safe_to_publish is False                                 # kills `safe_to_publish = True` (publish-gate hole)
    assert a.license == "unknown" and a.local_path == "/x/old.jpg"    # characterization: heal does NOT refresh license text


def test_existing_row_returns_its_own_stored_path_as_a_path(db_session):
    _cache_asset(db_session, _sa("pexels", "4"), "footage")
    _, p = _cache_asset(db_session, SourcedAsset("pexels", "4", Path("/other/z.mp4"), "l", 1.0), "footage")
    assert p == Path("/x/4") and isinstance(p, Path)                  # kills result.local_path / dropping Path()


def test_cache_asset_flushes_but_does_not_commit(db_session):
    _cache_asset(db_session, _sa("pexels", "5"), "footage")
    db_session.rollback()                                                      # kills db.commit() (callers batch the commit)
    assert db_session.query(models.Asset).count() == 0


# ---- resolve_beat_assets --------------------------------------------------------------------
def test_wikipedia_results_keep_the_order_the_names_appear_in(db_session):
    class W:
        def search(self, name):
            return _sa("wikipedia", name.split()[0], safe_to_publish=True)
    with patch(f"{_LOG}.time.sleep"):
        out = resolve_beat_assets(db_session, "Lionel Messi and Cristian Romero", 1.0, _NoneSrc(), wiki=W())
    assert [a.source_ref for a, _ in out] == ["Lionel", "Cristian"]   # kills reversed(found)


def test_pexels_gets_min_duration_and_hf_gets_the_exact_query(db_session):
    p = _Pex()
    resolve_beat_assets(db_session, " Stadium ", 6.5, p)
    assert p.calls == [(" Stadium ", 6.5)]                            # kills min_duration -> 0.0
    hv, hi = _HF("huggingface_video"), _HF("huggingface")
    resolve_beat_assets(db_session, " Stadium ", 1.0, _NoneSrc(), hf_video=hv)
    resolve_beat_assets(db_session, " Stadium ", 1.0, _NoneSrc(), hf=hi)
    assert hv.prompts == hi.prompts == [" Stadium "]                  # kills .strip()/.upper() on the query


def test_reel_id_none_never_enters_record_stage_even_with_an_api_key(db_session):
    """The existing 'no reel id' tests assert StageEvent count == 0, which is VACUOUS: record_stage
    swallows the NOT NULL commit failure, so dropping the `reel_id is not None` gate still passes."""
    hi = _HF("huggingface")
    with patch(f"{_LOG}.record_stage") as rs:
        resolve_beat_assets(db_session, "q", 1.0, _NoneSrc(), hf=hi, reel_id=None)
    rs.assert_not_called()
    assert hi.prompts == ["q"]


# ---- Pexels / Wikipedia -----------------------------------------------------------------------
def test_pexels_source_ref_is_the_video_id_not_the_chosen_files_id(tmp_path):
    vid = _video(7, [{"id": 999, "width": 1080, "height": 1920, "link": "https://cdn/a.mp4"}])
    result, _, _ = _pexels(tmp_path, [vid])
    assert result.source_ref == "7"


def test_wikipedia_original_equal_to_thumbnail_is_still_downloaded(tmp_path):
    url = "https://upload.wikimedia.org/x/Messi.jpg"
    result, calls = _wiki(tmp_path, summary={"pageid": 1, "originalimage": {"source": url},
                                             "thumbnail": {"source": url}},
                          downloads={url: _bytes_resp(b"i")})
    assert result is not None and result.local_path.read_bytes() == b"i"   # kills `original != thumbnail` filter


def test_wikipedia_missing_pageid_falls_back_to_the_underscored_page_title_not_the_query(tmp_path):
    url = "https://upload.wikimedia.org/x/Messi.jpg"
    result, _ = _wiki(tmp_path, summary={"originalimage": {"source": url}},
                      opensearch=["Lionel Messi", ["Lionel Andres Messi"], [], []],
                      downloads={url: _bytes_resp(b"i")})
    assert result.source_ref == "Lionel_Andres_Messi"


def test_wikipedia_each_candidate_gets_its_own_extension_and_success_stops_the_loop(tmp_path):
    orig, thumb = "https://u/x/Messi.png", "https://u/x/320px-Messi.jpg"
    result, calls = _wiki(tmp_path, summary={"pageid": 5, "originalimage": {"source": orig},
                                             "thumbnail": {"source": thumb}},
                          downloads={orig: httpx.ConnectError("x"), thumb: _bytes_resp(b"t")})
    assert result.local_path.suffix == ".jpg"                          # kills _image_extension(original or img_url)
    result2, calls2 = _wiki(tmp_path / "b", summary={"pageid": 6, "originalimage": {"source": orig},
                                                     "thumbnail": {"source": thumb}},
                            downloads={orig: _bytes_resp(b"o"), thumb: _bytes_resp(b"t")})
    assert thumb not in calls2 and result2.local_path.read_bytes() == b"o"   # kills dropping `break` after a download


# ---- HuggingFace logging / flag -----------------------------------------------------------------
@pytest.mark.parametrize("cls", [HuggingFaceImageSource, HuggingFaceVideoSource])
def test_hf_failure_logs_model_with_traceback_and_never_the_api_key(cls, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOG), \
            patch(f"{_MOD}.httpx.post", side_effect=httpx.ConnectError("down")):
        assert cls("SECRET-KEY", "the-model", tmp_path).generate("p") is None
    recs = [r for r in caplog.records if r.name == _LOG]               # kills getLogger('x')
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR
    assert recs[0].exc_info                                            # kills log.error (traceback lost)
    assert "the-model" in recs[0].getMessage() and "SECRET-KEY" not in recs[0].getMessage()


def test_hf_image_flag_stays_false_when_the_write_fails(tmp_path):
    s = HuggingFaceImageSource("k", "m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")), \
            patch(f"{_MOD}._atomic_write", side_effect=OSError("disk")):
        assert s.generate("p") is None
    assert s.last_call_was_generated is False                          # kills flag-before-write reorder


def test_hf_video_flag_stays_false_when_the_write_fails(tmp_path):
    s = HuggingFaceVideoSource("k", "m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")), \
            patch(f"{_MOD}._atomic_write", side_effect=OSError("disk")):
        assert s.generate("p") is None
    assert s.last_call_was_generated is False


