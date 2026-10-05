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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from api import models
from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    HuggingFaceImageSource,
    HuggingFaceVideoSource,
    PexelsVideoSource,
    SourcedAsset,
    WikipediaImageSource,
    _cache_asset,
    _choose_video_file,
    _generate_gated_hf_asset,
    _image_extension,
    resolve_beat_assets,
)
from tests.test_sourcer_selection import (  # noqa: F401  (the autouse fixture is re-exported on purpose)
    _wikipedia_downloads_via_get_fakes,
    _MOD, _bytes_resp, _hf_resp, _json_resp, _pexels, _stream_factory, _summary, _vf, _video, _wiki,
)
from tests.test_asset_sourcer_cost import _FakeHFSource, _asset_result

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
    vid = _video(7, [{"id": 999, "width": 1080, "height": 1920, "link": "https://videos.pexels.com/video-files/a.mp4"}])
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
    orig, thumb = "https://upload.wikimedia.org/x/Messi.png", "https://upload.wikimedia.org/x/320px-Messi.jpg"
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


# ── fourth pass: list-position blind spots and chain wiring ──────────────────
def test_tallest_fhd_portrait_wins_regardless_of_list_position():
    files = [_vf(1080, 1280), _vf(1080, 1920), _vf(720, 960)]
    assert _choose_video_file(files)["height"] == 1920


def test_no_portrait_and_nothing_within_cap_returns_the_first_file_not_the_tallest():
    files = [_vf(3840, 2160, "https://videos.pexels.com/video-files/first"), _vf(5120, 2880, "https://videos.pexels.com/video-files/second")]
    assert _choose_video_file(files)["link"] == "https://videos.pexels.com/video-files/first"


def test_image_extension_with_a_real_dotted_hostname():
    """Every earlier png/webp case used a host-less URL, so a split on the first '.' passed."""
    assert _image_extension("https://upload.wikimedia.org/a/b/Messi.png") == "png"
    assert _image_extension("https://upload.wikimedia.org/a/b/Messi.WEBP?x=1") == "webp"


def test_wikipedia_summary_url_percent_encodes_parentheses(tmp_path):
    orig = _summary()["originalimage"]["source"]
    _, calls = _wiki(tmp_path, summary=_summary(), downloads={orig: _bytes_resp(b"i")},
                     opensearch=["x", ["Ronaldo (footballer)"], [], []])
    summary_calls = [c for c in calls if c.startswith(AS.WikipediaImageSource._SUMMARY)]
    assert summary_calls == [AS.WikipediaImageSource._SUMMARY + "/Ronaldo_%28footballer%29"]


def test_wikipedia_cache_hit_on_original_skips_every_download(tmp_path):
    orig = "https://upload.wikimedia.org/x/Messi.jpg"
    thumb = "https://upload.wikimedia.org/x/320px-Messi.png"
    (tmp_path / "wiki_5.jpg").write_bytes(b"cached")
    result, calls = _wiki(
        tmp_path,
        summary={"pageid": 5, "originalimage": {"source": orig}, "thumbnail": {"source": thumb}},
        downloads={orig: _bytes_resp(b"new"), thumb: _bytes_resp(b"new")},
    )
    assert result.local_path == tmp_path / "wiki_5.jpg"
    assert orig not in calls and thumb not in calls


def test_atomic_write_propagates_the_original_error_when_the_tmp_was_never_created(tmp_path):
    class Boom(Exception):
        pass
    with patch.object(Path, "write_bytes", side_effect=Boom("disk")):
        with pytest.raises(Boom):
            AS._atomic_write(tmp_path / "x.bin", b"d")


def test_cache_asset_new_row_returns_the_results_local_path(db_session):
    r = _sa("pexels", "77")
    _, p = _cache_asset(db_session, r, "footage")
    assert p == r.local_path


def test_gated_hf_call_forwards_the_exact_query_to_generate():
    src = SimpleNamespace(api_key="k", last_call_was_generated=False,
                          generate=MagicMock(return_value=None))
    with patch(f"{_LOG}.record_stage") as rs:
        rs.return_value.__enter__.return_value = SimpleNamespace(detail={}, cost_usd=None)
        AS._generate_gated_hf_asset(MagicMock(), 1, "asset_hf_image", src, " exact q ", lambda r: 0.0)
    src.generate.assert_called_once_with(" exact q ")


def test_three_named_people_are_all_searched(db_session):
    seen = []

    class W:
        def search(self, name):
            seen.append(name)
            return _sa("wikipedia", name.split()[0], safe_to_publish=True)

    with patch(f"{_LOG}.time.sleep"):
        out = resolve_beat_assets(db_session, "Lionel Messi, Cristian Romero and Rodrigo Palacios",
                                  1.0, _NoneSrc(), wiki=W())
    assert seen == ["Lionel Messi", "Cristian Romero", "Rodrigo Palacios"]
    assert len(out) == 3


def test_hf_image_is_still_tried_when_the_hf_video_tier_yields_nothing(db_session):
    class VidNone:
        api_key, last_call_was_generated = "k", False

        def generate(self, p):
            return None

    out = resolve_beat_assets(db_session, "q", 1.0, _NoneSrc(), hf_video=VidNone(),
                              hf=_HF("huggingface"))
    assert out[0][0] is not None and out[0][0].source == "huggingface"


def test_flag_starts_false_on_both_hf_sources(tmp_path):
    assert HuggingFaceImageSource("k", "m", tmp_path / "a").last_call_was_generated is False
    assert HuggingFaceVideoSource("k", "m", tmp_path / "b").last_call_was_generated is False


# ── sixth pass: cache-hit return value, heal semantics, opensearch scope ─────
# _generate_gated_hf_asset must still RETURN the generator's result on a cache hit: a None here
# would turn every cached re-render into a black frame.
def test_gated_hf_returns_the_result_on_a_cache_hit(db_session):
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db_session.add(reel); db_session.commit()
    res = _asset_result()
    out = _generate_gated_hf_asset(db_session, reel.id, "asset_hf_image",
                                   _FakeHFSource(res, was_generated=False), "q", lambda r: 0.003)
    assert out is res


def test_resolve_returns_a_cached_hf_asset_when_reel_id_is_given(db_session):
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db_session.add(reel); db_session.commit()
    hi = _HF("huggingface"); hi.last_call_was_generated = False      # cache hit
    out = resolve_beat_assets(db_session, "q", 1.0, _NoneSrc(), hf=hi, reel_id=reel.id)
    assert out[0][0] is not None and out[0][0].source == "huggingface"
    hv = _HF("huggingface_video"); hv.last_call_was_generated = False
    out = resolve_beat_assets(db_session, "q2", 1.0, _NoneSrc(), hf_video=hv, reel_id=reel.id)
    assert out[0][0] is not None and out[0][0].source == "huggingface_video"


# gated path: the exact (long) query reaches hf_video / hf with a reel id (gated path)
def test_long_query_reaches_hf_tiers_unchanged_in_the_gated_path(db_session):
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db_session.add(reel); db_session.commit()
    q = "a very long visual direction describing a stadium at night with floodlights"
    hv, hi = _HF("huggingface_video"), _HF("huggingface")
    resolve_beat_assets(db_session, q, 1.0, _NoneSrc(), hf_video=hv, reel_id=reel.id)
    resolve_beat_assets(db_session, q, 1.0, _NoneSrc(), hf=hi, reel_id=reel.id)
    assert hv.prompts == hi.prompts == [q]


# heal: _cache_asset heal keys on license_url ONLY
def test_heal_keys_on_existing_license_url_not_attribution(db_session):
    a, _ = _cache_asset(db_session, _sa("wikipedia", "1", attribution="OLD", safe_to_publish=True), "photo")
    _cache_asset(db_session, _sa("wikipedia", "1", license_url="https://u", attribution="NEW"), "photo")
    assert (a.license_url, a.attribution, a.safe_to_publish) == ("https://u", "NEW", False)


def test_existing_url_without_attribution_is_not_healed(db_session):
    b, _ = _cache_asset(db_session, _sa("wikipedia", "2", license_url="https://A"), "photo")
    _cache_asset(db_session, _sa("wikipedia", "2", license_url="https://B", attribution="Y", safe_to_publish=True), "photo")
    assert (b.license_url, b.attribution, b.safe_to_publish) == ("https://A", None, False)


def test_heal_overwrites_safe_to_publish_true_with_false(db_session):
    a, _ = _cache_asset(db_session, _sa("wikipedia", "3", safe_to_publish=True), "photo")
    _cache_asset(db_session, _sa("wikipedia", "3", license_url="https://u", attribution="J", safe_to_publish=False), "photo")
    assert a.safe_to_publish is False


def test_heal_overwrites_attribution_with_none(db_session):
    a, _ = _cache_asset(db_session, _sa("wikipedia", "4", attribution="STALE"), "photo")
    _cache_asset(db_session, _sa("wikipedia", "4", license_url="https://u", attribution=None), "photo")
    assert a.attribution is None


# opensearch: opensearch answering 200 with a MediaWiki error object / short list is a miss, not a crash
@pytest.mark.parametrize("payload", [{"error": {"code": "x"}}, [], ["only-query"]])
def test_wikipedia_malformed_opensearch_is_a_miss(tmp_path, payload):
    """Must stop at the opensearch step: a usable summary is offered, and must never be requested."""
    result, calls = _wiki(tmp_path, summary=_summary(), opensearch=payload)
    assert result is None
    assert not any(c.startswith(AS.WikipediaImageSource._SUMMARY) for c in calls)


# ladder: a single over-cap portrait still wins over an in-cap landscape
def test_single_over_cap_portrait_beats_an_in_cap_landscape():
    files = [_vf(1080, 2560), _vf(1920, 1080)]
    assert _choose_video_file(files)["height"] == 2560


# ── seventh pass: every tier supplied at once, as render_cut does ────────────
def _hit(source, ref):
    return SourcedAsset(source=source, source_ref=ref, local_path=Path("/x/" + ref), license_str="l",
                        duration_s=1.0, safe_to_publish=True)


class _Rec:
    """Duck-types every tier: records the calls it receives. api_key / last_call_was_generated mirror
    the real HF sources so a keyed source is handled the way render_cut's would be."""

    def __init__(self, result=None, gen=True, key="k"):
        self.result, self.gen, self.api_key, self.calls = result, gen, key, []
        self.last_call_was_generated = False

    def search(self, *a):
        self.calls.append(a)
        return self.result

    def generate(self, p):
        self.calls.append(p)
        self.last_call_was_generated = self.gen
        return self.result


def _all(wiki=None, pex=None, vid=None, img=None):
    return dict(wiki=wiki or _Rec(), sourcer=pex or _Rec(), hf_video=vid or _Rec(), hf=img or _Rec())


def _run(db, q, t):
    return resolve_beat_assets(db, q, 5.0, t["sourcer"], wiki=t["wiki"], hf_video=t["hf_video"], hf=t["hf"])


def test_wiki_hit_wins_over_every_other_tier_and_queries_none_of_them(db_session):
    t = _all(wiki=_Rec(_hit("wikipedia", "w")), pex=_Rec(_hit("pexels", "p")),
             vid=_Rec(_hit("huggingface_video", "v")), img=_Rec(_hit("huggingface", "i")))
    out = _run(db_session, "Lionel Messi scores", t)
    assert [a.source for a, _ in out] == ["wikipedia"]
    assert t["sourcer"].calls == [] and t["hf_video"].calls == [] and t["hf"].calls == []


def test_pexels_hit_wins_with_hf_tiers_present_and_never_calls_generate(db_session):
    t = _all(pex=_Rec(_hit("pexels", "p")), vid=_Rec(_hit("huggingface_video", "v")), img=_Rec(_hit("huggingface", "i")))
    out = _run(db_session, "stadium", t)
    assert [a.source for a, _ in out] == ["pexels"]
    assert t["hf_video"].calls == [] and t["hf"].calls == []


def test_hf_video_hit_wins_and_image_tier_is_never_called(db_session):
    t = _all(vid=_Rec(_hit("huggingface_video", "v")), img=_Rec(_hit("huggingface", "i")))
    out = _run(db_session, "stadium", t)
    assert [a.source for a, _ in out] == ["huggingface_video"]
    assert t["hf"].calls == []


def test_wiki_rate_limit_pause_sits_between_the_two_searches(db_session):
    events = []

    class W:
        def search(self, name):
            events.append(("search", name))
            return None
    with patch(f"{_MOD}.time.sleep", side_effect=lambda s: events.append(("sleep", s))):
        resolve_beat_assets(db_session, "Lionel Messi and Cristian Romero", 5.0, _Rec(), wiki=W())
    assert events == [("search", "Lionel Messi"), ("sleep", 0.5), ("search", "Cristian Romero")]


# ── eighth pass: contracts a future edit could plausibly break ──────────────
# _choose_video_file: "tallest", not "largest area" (a plausible 'highest resolution' refactor)
def test_choose_is_by_height_not_by_area():
    files = [_vf(1000, 1000), _vf(400, 1200)]
    assert _choose_video_file(files)["height"] == 1200
    files = [_vf(1000, 1000), _vf(400, 1200)]
    assert _choose_video_file(list(reversed(files)))["height"] == 1200


# Wikipedia: a %3F ("?") in the original image filename is decoded AFTER the query split
def test_wikipedia_filename_with_encoded_question_mark_keeps_its_name_for_the_license_lookup(tmp_path):
    titles = []

    def fake_get(url, **kw):
        p = kw.get("params") or {}
        if url == WikipediaImageSource._SEARCH and p.get("action") == "opensearch":
            return _json_resp(["x", ["Who"], [], []])
        if url == WikipediaImageSource._SEARCH and p.get("action") == "query":
            titles.append(p["titles"])
            return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {}}]}}}})
        if url.startswith(WikipediaImageSource._SUMMARY):
            return _json_resp(_summary(original="https://upload.wikimedia.org/a/ab/Who%3F_Photo.jpg", thumbnail=None))
        return _bytes_resp(b"img")

    with patch(f"{_MOD}.httpx.get", side_effect=fake_get), patch(f"{_MOD}.time.sleep"):
        assert WikipediaImageSource(tmp_path).search("Who") is not None
    assert titles == ["File:Who?_Photo.jpg"]


# resolve_beat_assets: wiki is optional (documented) -- a named person with wiki=None goes to Pexels
def test_wiki_none_with_a_named_person_falls_to_pexels(db_session):
    p = _Pex()
    out = resolve_beat_assets(db_session, "Lionel Messi scores", 2.0, p, wiki=None)
    assert [a.source for a, _ in out] == ["pexels"] and p.calls == [("Lionel Messi scores", 2.0)]


# reel_id omitted (default) must never enter record_stage, even with an api-keyed HF source
def test_reel_id_omitted_never_enters_record_stage(db_session):
    hv, hi = _HF("huggingface_video"), _HF("huggingface")
    with patch(f"{_LOG}.record_stage") as rs:
        resolve_beat_assets(db_session, "q", 1.0, _NoneSrc(), hf_video=hv)
        resolve_beat_assets(db_session, "q", 1.0, _NoneSrc(), hf=hi)
    rs.assert_not_called()


# positional order wiki, hf_video, hf (resolve_or_reuse calls it positionally)
def test_positional_order_is_wiki_then_hf_video_then_hf(db_session):
    wiki, vid, img = _Rec(), _Rec(_hit("huggingface_video", "v")), _Rec(_hit("huggingface", "i"))
    out = resolve_beat_assets(db_session, "stadium", 1.0, _Rec(), wiki, vid, img)
    assert [a.source for a, _ in out] == ["huggingface_video"] and img.calls == []


# resolve_beat_assets must not commit: the caller (resolve_or_reuse) owns the transaction
@pytest.mark.parametrize("tier", ["wiki", "pexels", "hf_video", "hf"])
def test_resolve_does_not_commit_new_assets(db_session, tier):
    t = dict(wiki=_Rec(), sourcer=_Rec(), hf_video=_Rec(), hf=_Rec())
    key = {"wiki": "wiki", "pexels": "sourcer"}.get(tier, tier)
    t[key] = _Rec(_hit(tier, "ref"))
    resolve_beat_assets(db_session, "Lionel Messi scores", 1.0, t["sourcer"], wiki=t["wiki"],
                        hf_video=t["hf_video"], hf=t["hf"])
    assert db_session.query(models.Asset).count() == 1
    db_session.rollback()
    assert db_session.query(models.Asset).count() == 0


# each source instance writes under ITS OWN store_dir (no class-level / module-level dir)
def test_pexels_instances_use_their_own_store_dir(tmp_path):
    outs = []
    srcs = [PexelsVideoSource(api_key="k", store_dir=tmp_path / sub) for sub in ("a", "b")]   # both built first
    for src in srcs:
        with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(7, [_vf(1080, 1920)])]})), \
                patch(f"{_MOD}._httpx_stream", _stream_factory([])):
            outs.append(src.search("q", 1.0).local_path)
    assert outs[0].parent == tmp_path / "a" and outs[1].parent == tmp_path / "b"


# gated helper: provider is the literal "huggingface" (paid_call_count counts provider == "nvidia")
def test_gated_stage_event_provider_is_huggingface_even_when_the_source_has_a_model(db_session):
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db_session.add(reel); db_session.commit()
    src = _Rec(_hit("huggingface", "i"))
    src.model = "org/flux"
    _generate_gated_hf_asset(db_session, reel.id, "asset_hf_image", src, "q", lambda r: 0.01)
    ev = db_session.query(models.StageEvent).one()
    assert ev.provider == "huggingface"


# factories build from CURRENT settings on every call (no lru_cache / import-time snapshot)
def test_factories_reflect_current_settings_each_call(tmp_path, monkeypatch):
    monkeypatch.setattr(AS.settings, "pexels_api_key", "k1")
    monkeypatch.setattr(AS.settings, "huggingface_api_key", "h1")
    a1, h1, v1 = AS.get_asset_sourcer(tmp_path), AS.get_hf_sourcer(tmp_path), AS.get_hf_video_sourcer(tmp_path)
    monkeypatch.setattr(AS.settings, "pexels_api_key", "k2")
    monkeypatch.setattr(AS.settings, "huggingface_api_key", "h2")
    a2, h2, v2 = AS.get_asset_sourcer(tmp_path), AS.get_hf_sourcer(tmp_path), AS.get_hf_video_sourcer(tmp_path)
    assert (a1.api_key, a2.api_key, h1.api_key, h2.api_key, v1.api_key, v2.api_key) == ("k1", "k2", "h1", "h2", "h1", "h2")
    assert AS.get_wiki_sourcer(tmp_path / "x").store_dir == tmp_path / "x" / "wiki"
    assert AS.get_wiki_sourcer(tmp_path / "y").store_dir == tmp_path / "y" / "wiki"


# Wikipedia summary URL: a literal % or ? in a page title is escaped (low priority)
def test_wikipedia_summary_url_escapes_percent_and_question_mark(tmp_path):
    urls = []

    def fake_get(url, **kw):
        p = kw.get("params") or {}
        urls.append(url)
        if url == WikipediaImageSource._SEARCH and p.get("action") == "opensearch":
            return _json_resp(["x", ["100% Wolf?"], [], []])
        return _json_resp(_summary(original=None, thumbnail=None))

    with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
        WikipediaImageSource(tmp_path).search("x")
    assert urls[-1] == f"{WikipediaImageSource._SUMMARY}/100%25_Wolf%3F"
