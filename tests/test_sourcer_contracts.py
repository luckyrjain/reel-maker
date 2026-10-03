"""HTTP / persistence contracts of the asset sourcers (engine/render/asset_sourcer.py).

Second pass over the same module as test_sourcer_selection.py, added after an independent
mutation review found behaviors nothing pinned: the exact fail-closed license set (an
ADDITION to it is as dangerous as a removal), `SourcedAsset`'s fail-closed default,
`_atomic_write`, the factories, status-code / malformed-body handling on every HTTP call,
the Pexels / HuggingFace request shapes, `_cache_asset`, and `resolve_beat_assets` tiers.
Tests marked "characterization" pin current behavior that is not necessarily intended.
"""
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    _PERMISSIVE_LICENSES,
    HuggingFaceImageSource,
    HuggingFaceVideoSource,
    PexelsVideoSource,
    SourcedAsset,
    WikipediaImageSource,
    _atomic_write,
    _strip_html,
    _cache_asset,
    _choose_video_file,
    get_asset_sourcer,
    get_hf_sourcer,
    get_hf_video_sourcer,
    get_wiki_sourcer,
)
from tests.test_sourcer_selection import (
    _MOD, _ORIG, _THUMB, _bytes_resp, _hf_resp, _json_resp, _pexels, _stream_factory, _summary, _vf,
    _video, _wiki,
)


# -- the fail-closed license list is pinned exactly (additions are as dangerous as removals) --
def test_permissive_license_set_is_exactly_these_members():
    assert _PERMISSIVE_LICENSES == {
        "cc0", "cc-0", "public domain", "cc by", "cc-by", "cc by 2.0", "cc by 4.0",
        "pexels", "pexels_free",
    }


@pytest.mark.parametrize("short", ["CC BY-SA", "CC BY-NC 4.0", "GFDL", "Attribution", "CC BY 1.0", "CC BY 2.5", "PDM"])
def test_unlisted_licenses_are_not_safe(short):
    assert AS._license_from_extmetadata({"LicenseShortName": {"value": short}})["safe_to_publish"] is False


def test_sourced_asset_defaults_are_fail_closed():
    a = SourcedAsset(source="s", source_ref="r", local_path=None, license_str="l", duration_s=0.0)
    assert a.safe_to_publish is False and a.license_url is None and a.attribution is None


# -- _atomic_write has no direct test at all --
def test_atomic_write_writes_through_a_tmp_and_leaves_none(tmp_path):
    target = tmp_path / "x.bin"
    _atomic_write(target, b"abc")
    assert target.read_bytes() == b"abc"
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin"]


def test_atomic_write_never_exposes_a_partial_final_file_and_cleans_up_on_failure(tmp_path):
    target = tmp_path / "x.bin"
    target.write_bytes(b"old")
    with patch(f"{AS.__name__}.os.replace", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):                    # the failure is re-raised, not swallowed
            _atomic_write(target, b"new")
    assert target.read_bytes() == b"old"                     # final path untouched
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin"] # no .tmp left behind


def test_atomic_write_distinguishes_tmp_from_target(tmp_path):
    seen = {}
    real = AS.os.replace
    def spy(a, b):
        seen["a"], seen["b"] = str(a), str(b); real(a, b)
    with patch(f"{AS.__name__}.os.replace", side_effect=spy):
        _atomic_write(tmp_path / "y.png", b"z")
    assert seen["a"] != seen["b"] and seen["a"].endswith(".tmp")


# -- factories (only ever patched out in the render tests) --
def test_factories_wire_subdirs_keys_and_models(tmp_path, monkeypatch):
    s = AS.settings
    monkeypatch.setattr(s, "pexels_api_key", "PK")
    monkeypatch.setattr(s, "huggingface_api_key", "HK")
    monkeypatch.setattr(s, "huggingface_image_model", "img-model")
    monkeypatch.setattr(s, "huggingface_video_model", "vid-model")
    p, w, hi, hv = (get_asset_sourcer(tmp_path), get_wiki_sourcer(tmp_path),
                    get_hf_sourcer(tmp_path), get_hf_video_sourcer(tmp_path))
    assert (p.api_key, p.store_dir) == ("PK", tmp_path / "footage")
    assert w.store_dir == tmp_path / "wiki"
    assert (hi.api_key, hi.model, hi.store_dir) == ("HK", "img-model", tmp_path / "hf")
    assert (hv.api_key, hv.model, hv.store_dir) == ("HK", "vid-model", tmp_path / "hfvid")


def test_sources_create_nested_store_dirs(tmp_path):
    for cls, args in ((WikipediaImageSource, ()), (PexelsVideoSource, ("k",)),
                      (HuggingFaceImageSource, ("k", "m")), (HuggingFaceVideoSource, ("k", "m"))):
        d = tmp_path / cls.__name__ / "a" / "b"          # a fresh, missing parent chain per class
        cls(*args, d)
        assert d.is_dir()


# -- Wikipedia: HTTP-status / malformed-body handling on every call --
def _wiki_routes(tmp_path, *, opensearch=None, summary=None, license_=None):
    def fake_get(url, **kw):
        a = (kw.get("params") or {}).get("action")
        if a == "opensearch":
            return opensearch if opensearch is not None else _json_resp(["x", ["Lionel Messi"], [], []])
        if a == "query":
            return license_ if license_ is not None else _json_resp({})
        if url.startswith(WikipediaImageSource._SUMMARY):
            return summary if summary is not None else _json_resp(_summary())
        return _bytes_resp(b"i")
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
        return WikipediaImageSource(tmp_path).search("Lionel Messi")


def test_wikipedia_opensearch_http_error_with_a_valid_body_is_none(tmp_path):
    assert _wiki_routes(tmp_path, opensearch=_json_resp(["x", ["Lionel Messi"], [], []], status=500)) is None


def test_wikipedia_summary_http_error_with_a_valid_body_is_none(tmp_path):
    assert _wiki_routes(tmp_path, summary=_json_resp(_summary(), status=404)) is None


def test_wikipedia_license_http_error_with_a_valid_body_is_unknown_and_unsafe(tmp_path):
    good = {"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {"LicenseShortName": {"value": "CC0"}}}]}}}}
    r = _wiki_routes(tmp_path, license_=_json_resp(good, status=503))
    assert r.license_str == "unknown" and r.safe_to_publish is False


@pytest.mark.parametrize("which", ["opensearch", "summary"])
def test_wikipedia_malformed_json_body_is_none(tmp_path, which):
    bad = _json_resp(None)
    bad.json.side_effect = ValueError("not json")
    assert _wiki_routes(tmp_path, **{which: bad}) is None


def test_wikipedia_malformed_json_license_is_unknown_and_unsafe(tmp_path):
    bad = _json_resp(None)
    bad.json.side_effect = ValueError("not json")
    r = _wiki_routes(tmp_path, license_=bad)
    assert r.license_str == "unknown" and r.safe_to_publish is False


def test_wikipedia_license_uses_the_first_imageinfo_entry(tmp_path):
    meta = lambda s: {"extmetadata": {"LicenseShortName": {"value": s}}}
    payload = {"query": {"pages": {"1": {"imageinfo": [meta("CC0"), meta("Fair use")]}}}}
    source = WikipediaImageSource(tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp(payload)):
        assert source._fetch_license("a.jpg")["license"] == "CC0"


def test_wikipedia_slash_in_a_title_is_percent_encoded_in_the_summary_url(tmp_path):
    seen = []
    def fake_get(url, **kw):
        seen.append((url, kw))
        a = (kw.get("params") or {}).get("action")
        if a == "opensearch": return _json_resp(["x", ["AC/DC"], [], []])
        if "/page/summary/" in url: return _json_resp({"pageid": 1})
        return _json_resp({})
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
        WikipediaImageSource(tmp_path).search("AC/DC")
    assert seen[1][1]["timeout"] == 10.0
    assert seen[1][0].endswith("/page/summary/AC%2FDC")


def test_wikipedia_urls_are_the_real_endpoints():
    assert WikipediaImageSource._SEARCH == "https://en.wikipedia.org/w/api.php"
    assert WikipediaImageSource._SUMMARY == "https://en.wikipedia.org/api/rest_v1/page/summary"


def test_wikipedia_a_5xx_original_is_not_retried_or_slept_on(tmp_path):
    sleeps = []
    result, calls = _wiki(tmp_path, summary=_summary(),
                          downloads={_ORIG: _bytes_resp(status=500), _THUMB: _bytes_resp(b"t")}, sleeps=sleeps)
    assert sleeps == [] and calls.count(_ORIG) == 1


def test_wikipedia_original_is_not_used_for_the_license_when_original_missing(tmp_path):
    result, calls = _wiki(tmp_path, summary=_summary(original=None), downloads={_THUMB: _bytes_resp()})
    assert result.safe_to_publish is False and result.license_url is None and result.attribution is None


def test_wikipedia_thumbnail_only_inline_license_is_exactly_unknown(tmp_path):
    result, _ = _wiki(tmp_path, summary=_summary(original=None), downloads={_THUMB: _bytes_resp()})
    assert (result.license_str, result.license_url, result.attribution, result.safe_to_publish) == \
        ("unknown", None, None, False)


def test_wikipedia_pageid_is_stringified_even_when_filename_ignores_it(tmp_path):
    result, _ = _wiki(tmp_path, summary=_summary(pageid=77), downloads={_ORIG: _bytes_resp()})
    assert result.source_ref == "77" and isinstance(result.source_ref, str)


def test_wikipedia_download_bytes_are_content_not_text(tmp_path):
    r = _bytes_resp(b"\xff\xd8bin"); r.text = "wrong"
    result, _ = _wiki(tmp_path, summary=_summary(thumbnail=None), downloads={_ORIG: r})
    assert result.local_path.read_bytes() == b"\xff\xd8bin"


def test_wikipedia_non_http_download_errors_still_fall_through(tmp_path):
    result, _ = _wiki(tmp_path, summary=_summary(),
                      downloads={_ORIG: ValueError("x"), _THUMB: _bytes_resp(b"t")})
    assert result.local_path.read_bytes() == b"t"


def test_wikipedia_cached_original_prefers_cache_over_thumbnail_download(tmp_path):
    (tmp_path / "wiki_123.jpg").write_bytes(b"c")
    result, calls = _wiki(tmp_path, summary=_summary())
    assert _THUMB not in calls and result.local_path.read_bytes() == b"c"


# -- Pexels --
def test_pexels_http_error_with_a_valid_body_is_none(tmp_path):
    from tests.test_sourcer_selection import _stream_factory
    src = PexelsVideoSource("k", tmp_path)
    streamed = []
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]}, status=500)), \
            patch(f"{_MOD}.httpx.stream", _stream_factory(streamed)):
        assert src.search("q", 1.0) is None
    assert streamed == []        # a 5xx search response must never lead to a download


def test_pexels_non_http_search_errors_return_none(tmp_path):
    with patch(f"{_MOD}.httpx.get", side_effect=RuntimeError("weird")):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None


def test_pexels_200_without_a_videos_key_is_none(tmp_path):
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({})):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None


def test_pexels_malformed_search_json_is_none(tmp_path):
    bad = _json_resp(None); bad.json.side_effect = ValueError("x")
    with patch(f"{_MOD}.httpx.get", return_value=bad):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None


@pytest.mark.parametrize("body", [[], "oops", 7, None, {"videos": None}, {"videos": "x"}, {"videos": {"a": 1}}])
def test_pexels_wrong_shape_search_body_is_none(tmp_path, body):
    streamed = []
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp(body)), \
            patch(f"{_MOD}.httpx.stream", _stream_factory(streamed)):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None
    assert streamed == []


_GOOD = _video(9, [_vf(1080, 1920)])


@pytest.mark.parametrize("bad", [
    {"duration": 10, "video_files": [_vf(1080, 1920)]},                      # no id
    {"id": 1, "video_files": [_vf(1080, 1920)]},                             # no duration
    {"id": 1, "duration": None, "video_files": [_vf(1080, 1920)]},
    {"id": 1, "duration": "10", "video_files": [_vf(1080, 1920)]},
    {"id": 1, "duration": 10, "video_files": [{"width": None, "height": None, "link": "https://videos.pexels.com/a.mp4"}]},
    {"id": 1, "duration": 10, "video_files": [{"width": 1080, "height": None, "link": "https://videos.pexels.com/a.mp4"}]},
    {"id": 1, "duration": 10, "video_files": [{"width": "1080", "height": 1920, "link": "https://videos.pexels.com/a.mp4"}]},
    {"id": 1, "duration": 10, "video_files": "nope"},
    {"id": 1, "duration": 10, "video_files": [None]},
    {"id": 1, "duration": 10, "video_files": None},
    {"id": 1, "duration": 10, "video_files": [{"width": 1080, "height": 1920, "link": 5}]},
    None, "x", 3, [],
], ids=lambda b: repr(b)[:50])
def test_pexels_a_malformed_video_entry_is_skipped_not_raised(tmp_path, bad):
    """One bad entry must not abort the search: the good entry after it is still used."""
    result, streamed, _ = _pexels(tmp_path, [bad, _GOOD])
    assert result is not None and result.source_ref == "9"
    assert streamed == [_GOOD["video_files"][0]["link"]]


def test_pexels_missing_duration_is_skipped_even_with_no_minimum(tmp_path):
    """No `duration` is not "0 seconds": with min_duration_s=0 it used to KeyError at the end."""
    src = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    vids = [{"id": 1, "video_files": [_vf(1080, 1920)]}]
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": vids})), \
            patch(f"{_MOD}.httpx.stream", _stream_factory([])):
        assert src.search("q", 0.0) is None


def test_pexels_only_malformed_entries_is_none(tmp_path):
    result, streamed, _ = _pexels(tmp_path, [{"duration": 10}, None])
    assert result is None and streamed == []


def test_pexels_download_uses_a_get_stream(tmp_path):
    methods = []
    @contextmanager
    def fake_stream(method, url, **kw):
        methods.append(method); r = MagicMock(); r.iter_bytes.return_value = iter([b"v"]); yield r
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]})), \
            patch(f"{_MOD}.httpx.stream", fake_stream):
        PexelsVideoSource("k", tmp_path).search("q", 1.0)
    assert methods == ["GET"]


def test_pexels_a_stale_tmp_file_is_overwritten_not_appended(tmp_path):
    (tmp_path / "pexels_1.tmp").write_bytes(b"JUNK")
    result, _, _ = _pexels(tmp_path, [_video(1, [_vf(1080, 1920)])])
    assert result.local_path.read_bytes() == b"video"


def test_pexels_non_http_download_errors_still_skip_to_next(tmp_path):
    @contextmanager
    def fake_stream(method, url, **kw):
        if "bad" in url: raise OSError("disk full")
        r = MagicMock(); r.iter_bytes.return_value = iter([b"v"]); yield r
    vids = [_video(1, [_vf(1080, 1920, link="https://cdn/bad.mp4")]), _video(2, [_vf(1080, 1920)])]
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": vids})), patch(f"{_MOD}.httpx.stream", fake_stream):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0).source_ref == "2"


def test_pexels_empty_string_key_and_none_key_both_skip(tmp_path):
    for key in ("", None):
        with patch(f"{_MOD}.httpx.get") as get:
            assert PexelsVideoSource(key, tmp_path).search("q", 1.0) is None
        get.assert_not_called()


def test_pexels_api_base_url_literal():
    assert PexelsVideoSource._API == "https://api.pexels.com/videos"


# -- HF --
def test_hf_urls_and_auth_scheme(tmp_path):
    for cls in (HuggingFaceImageSource, HuggingFaceVideoSource):
        assert cls._API == "https://api-inference.huggingface.co/models"
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")) as post:
        HuggingFaceImageSource("k", "org/m", tmp_path).generate("p")
    assert post.call_args.args[0] == "https://api-inference.huggingface.co/models/org/m"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer k"}
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")) as post:
        HuggingFaceVideoSource("k", "org/m", tmp_path).generate("p")
    assert post.call_args.args[0] == "https://api-inference.huggingface.co/models/org/m"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer k"}


def test_hf_image_request_json_key_and_bytes_written(tmp_path):
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png", b"PNGDATA")) as post:
        r = HuggingFaceImageSource("k", "m", tmp_path).generate("p")
    assert list(post.call_args.kwargs["json"]) == ["inputs"]
    assert r.local_path.read_bytes() == b"PNGDATA"


def test_hf_http_error_status_is_a_failure_for_image_and_video(tmp_path):
    def err(ct):
        resp = _hf_resp(ct, b"<html>oops</html>")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=resp)
        return resp
    with patch(f"{_MOD}.httpx.post", return_value=err("image/png")):
        assert HuggingFaceImageSource("k", "m", tmp_path).generate("p") is None
    with patch(f"{_MOD}.httpx.post", return_value=err("video/mp4")):
        v = HuggingFaceVideoSource("k", "m", tmp_path)
        assert v.generate("p") is None
        assert v.last_call_was_generated is False
    assert list(tmp_path.iterdir()) == []


def test_hf_non_http_errors_return_none_not_raise(tmp_path):
    with patch(f"{_MOD}.httpx.post", side_effect=ValueError("boom")):
        assert HuggingFaceImageSource("k", "m", tmp_path).generate("p") is None
        assert HuggingFaceVideoSource("k", "m", tmp_path).generate("p") is None


def test_hf_generated_assets_carry_no_license_url_or_attribution_and_video_license_str(tmp_path):
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")):
        i = HuggingFaceImageSource("k", "m", tmp_path).generate("p")
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")):
        v = HuggingFaceVideoSource("k", "m", tmp_path).generate("p")
    for a in (i, v):
        assert (a.license_str, a.license_url, a.attribution, a.safe_to_publish) == ("generated", None, None, True)


def test_hf_video_prefers_a_cached_mp4_over_a_cached_gif(tmp_path):
    full = "p, portrait orientation, vertical format, cinematic, high quality"
    fp = hashlib.sha256(full.encode()).hexdigest()[:16]
    (tmp_path / f"hfvid_{fp}.mp4").write_bytes(b"m"); (tmp_path / f"hfvid_{fp}.gif").write_bytes(b"g")
    with patch(f"{_MOD}.httpx.post") as post:
        r = HuggingFaceVideoSource("k", "m", tmp_path).generate("p")
    post.assert_not_called()
    assert r.local_path.suffix == ".mp4"


def test_hf_video_gif_detection_is_case_sensitive_characterization(tmp_path):
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/GIF")):          # upper-case: pin current behavior
        assert HuggingFaceVideoSource("k", "m", tmp_path).generate("p").local_path.suffix == ".mp4"


def test_hf_fingerprint_is_over_the_full_prompt_for_both(tmp_path):
    full = lambda p: f"{p}, portrait orientation, vertical format, cinematic, high quality"
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")):
        i = HuggingFaceImageSource("k", "m", tmp_path).generate("p")
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")):
        v = HuggingFaceVideoSource("k", "m", tmp_path).generate("p")
    exp = hashlib.sha256(full("p").encode()).hexdigest()[:16]
    assert i.source_ref == v.source_ref == exp
    assert v.local_path.name == f"hfvid_{exp}.mp4"
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")) as post:
        HuggingFaceVideoSource("k", "m", tmp_path / "v2").generate("p")
    assert post.call_args.kwargs["json"]["inputs"] == full("p")


# -- _cache_asset heal --
def test_cache_asset_new_row_carries_every_field_and_heal_copies_license_fields():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from api import models
    eng = create_engine("sqlite://"); models.Base.metadata.create_all(eng); db = sessionmaker(bind=eng)()
    a, p = _cache_asset(db, SourcedAsset("wikipedia", "1", Path("/x/a.jpg"), "unknown", 0.0), "photo")
    assert (a.type, a.license, a.safe_to_publish, a.local_path) == ("photo", "unknown", False, "/x/a.jpg")
    _cache_asset(db, SourcedAsset("wikipedia", "1", Path("/x/a.jpg"), "CC0", 0.0,
                                  license_url="https://u", attribution="Jane", safe_to_publish=True), "photo")
    assert (a.license_url, a.attribution, a.safe_to_publish) == ("https://u", "Jane", True)
    # characterization, not endorsement: the heal does not touch the `license` string itself
    assert a.license == "unknown"
    b, _ = _cache_asset(db, SourcedAsset("pexels", "2", Path("/x/b.mp4"), "pexels_free", 5.0,
                                         safe_to_publish=True), "footage")
    assert (b.type, b.license, b.safe_to_publish) == ("footage", "pexels_free", True)


# -- image extension / license passthrough / content-type edge cases --
@pytest.mark.parametrize("url", ["https://x/a.bmp", "https://x/a.tif", "https://x/a.avif", "https://x/a.tiff"])
def test_image_extension_unlisted_types_become_jpg(url):
    assert AS._image_extension(url) == "jpg"


def test_license_string_is_returned_verbatim_not_normalized():
    assert AS._license_from_extmetadata({"LicenseShortName": {"value": " CC0 "}})["license"] == " CC0 "


def test_hf_video_gif_detection_is_a_substring_match(tmp_path):
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/gif; charset=binary")):
        assert HuggingFaceVideoSource("k", "m", tmp_path).generate("p").local_path.suffix == ".gif"


def test_hf_image_content_type_needs_the_slash(tmp_path):
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("imagey")):
        assert HuggingFaceImageSource("k", "m", tmp_path).generate("p") is None


# -- resolve_beat_assets: asset types per tier, provider, fall-through, sentinel --
class _NoneSourcer:
    def search(self, q, d): return None


class _Pex:
    def __init__(self, r): self.r = r
    def search(self, q, d): return self.r


class _Wiki:
    def __init__(self, r): self.r = r
    def search(self, name): return self.r


class _HF:
    def __init__(self, result, gen, api_key="key"):
        self.result, self.last_call_was_generated, self.api_key = result, gen, api_key
    def generate(self, p): return self.result


def _sa(source, ref="r", dur=0.0):
    return SourcedAsset(source=source, source_ref=ref, local_path=Path("/x/" + ref),
                        license_str="l", duration_s=dur, safe_to_publish=True)


def test_resolve_beat_assets_asset_type_per_tier(db_session):
    db = db_session
    (a, _), = AS.resolve_beat_assets(db, "Lionel Messi scores", 1.0, _NoneSourcer(), wiki=_Wiki(_sa("wikipedia", "w")))
    assert a.type == "photo"
    (a, _), = AS.resolve_beat_assets(db, "stadium", 1.0, _Pex(_sa("pexels", "p")))
    assert a.type == "footage"
    (a, _), = AS.resolve_beat_assets(db, "stadium", 1.0, _NoneSourcer(), hf_video=_HF(_sa("huggingface_video", "v"), False))
    assert a.type == "footage"
    (a, _), = AS.resolve_beat_assets(db, "stadium", 1.0, _NoneSourcer(), hf=_HF(_sa("huggingface", "i"), False))
    assert a.type == "photo"


def test_resolve_beat_assets_named_person_not_found_falls_through_to_pexels(db_session):
    (a, _), = AS.resolve_beat_assets(db_session, "Lionel Messi scores", 1.0, _Pex(_sa("pexels", "p")), wiki=_Wiki(None))
    assert a.source == "pexels"


def test_resolve_beat_assets_nothing_found_returns_the_single_none_sentinel(db_session):
    assert AS.resolve_beat_assets(db_session, "stadium", 1.0, _NoneSourcer()) == [(None, None)]


def test_hf_stage_events_record_provider_and_video_cost_uses_the_generated_duration(db_session):
    from api import models
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating); db.add(reel); db.commit()
    with patch(f"{_MOD}.settings.huggingface_price_per_video_second", 0.01):
        AS.resolve_beat_assets(db, "q", 1.0, _NoneSourcer(),
                               hf_video=_HF(_sa("huggingface_video", "v", dur=7.0), True), reel_id=reel.id)
    ev = db.query(models.StageEvent).filter_by(reel_id=reel.id).one()
    assert ev.provider == "huggingface" and round(ev.cost_usd, 4) == 0.07      # 7 s, not a constant 4 s


def test_hf_image_tier_stage_event_records_the_provider(db_session):
    from api import models
    db = db_session
    reel = models.Reel(context="x", status=models.ReelStatus.generating); db.add(reel); db.commit()
    AS.resolve_beat_assets(db, "q", 1.0, _NoneSourcer(),
                           hf=_HF(_sa("huggingface", "i"), True), reel_id=reel.id)
    ev = db.query(models.StageEvent).filter_by(reel_id=reel.id).one()
    assert (ev.stage, ev.provider) == ("asset_hf_image", "huggingface")


def test_strip_html_leaves_a_bare_empty_tag_marker():
    """`<>` is not a tag (`<[^>]+>` needs at least one character): current behavior."""
    assert AS._strip_html("a<>b <i>c</i>") == "a<>b c"


# ── fifth pass: case preservation, boundaries, retry scoping ─────────────────
def test_strip_html_strips_all_whitespace_kinds():
    assert _strip_html("<b>x</b>\n\t ") == "x"
    assert _strip_html("\n<i>y</i>") == "y"


def test_license_lookup_decodes_exactly_once(tmp_path):
    # "%2541" is a literal "%41" in the real title; a second decode would turn it into "A".
    orig = "https://upload.wikimedia.org/wikipedia/commons/a/ab/A%2541.jpg"
    with patch.object(WikipediaImageSource, "_fetch_license", return_value={
            "license": "l", "license_url": None, "attribution": None, "safe_to_publish": False}) as fl:
        _wiki(tmp_path, summary=_summary(original=orig, thumbnail=None),
              downloads={orig: _bytes_resp(b"i")})
    fl.assert_called_once_with("A%41.jpg")


def test_no_license_lookup_when_original_url_has_an_empty_filename(tmp_path):
    orig = "https://upload.wikimedia.org/wikipedia/commons/dir/"
    with patch.object(WikipediaImageSource, "_fetch_license") as fl:
        result, _ = _wiki(tmp_path, summary=_summary(original=orig, thumbnail=None),
                          downloads={orig: _bytes_resp(b"i")})
    fl.assert_not_called()
    assert result.safe_to_publish is False and result.license_str == "unknown"


def test_429_retry_refetches_the_same_candidate_not_the_first(tmp_path):
    sleeps = []
    downloads = {_ORIG: _bytes_resp(status=500), _THUMB: [_bytes_resp(status=429), _bytes_resp(b"t")]}
    result, calls = _wiki(tmp_path, summary=_summary(), downloads=downloads, sleeps=sleeps)
    assert result is not None and sleeps == [2.0]
    assert calls[-2:] == [_THUMB, _THUMB]


def test_only_429_triggers_the_sleep_and_retry(tmp_path):
    sleeps = []
    downloads = {_ORIG: _bytes_resp(status=503), _THUMB: _bytes_resp(b"t")}
    result, calls = _wiki(tmp_path, summary=_summary(), downloads=downloads, sleeps=sleeps)
    assert sleeps == [] and calls.count(_ORIG) == 1


def test_pexels_query_is_sent_verbatim_case_preserved(tmp_path):
    src = PexelsVideoSource("k", tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": []})) as get:
        src.search("Lionel MESSI Goal", 5.0)
    assert get.call_args.kwargs["params"]["query"] == "Lionel MESSI Goal"


def _pexels_min(tmp_path, videos, min_d):
    src = PexelsVideoSource("k", tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": videos})), \
            patch(f"{_MOD}.httpx.stream", _stream_factory([])):
        return src.search("q", min_d)


def test_pexels_duration_just_below_minimum_is_rejected(tmp_path):
    assert _pexels_min(tmp_path, [_video(1, [_vf(1080, 1920)], duration=4)], 5.0) is None


def test_pexels_fractional_duration_above_fractional_minimum_qualifies(tmp_path):
    r = _pexels_min(tmp_path, [_video(1, [_vf(1080, 1920)], duration=5.5)], 5.2)
    assert r is not None and r.duration_s == 5.5


def test_pexels_multi_digit_video_id_is_kept_whole(tmp_path):
    result, _, _ = _pexels(tmp_path, [_video(1234567, [_vf(1080, 1920)])])
    assert result.source_ref == "1234567"
    assert result.local_path.name == "pexels_1234567.mp4"


@pytest.mark.parametrize("cls,resp", [(HuggingFaceImageSource, _hf_resp("image/png")),
                                      (HuggingFaceVideoSource, _hf_resp("video/mp4"))])
def test_hf_model_id_is_used_case_preserved_in_the_url(cls, resp, tmp_path):
    src = cls(api_key="k", model="Org/Model-X.1", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=resp) as post:
        src.generate("p")
    assert post.call_args.args[0].endswith("/Org/Model-X.1")


def test_hf_image_accepts_any_image_content_type(tmp_path):
    src = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/jpeg")):
        assert src.generate("p") is not None
    assert src.last_call_was_generated is True


def test_gated_stage_event_has_no_cut_and_exact_detail(db_session):
    from api import models
    reel = models.Reel(context="x", status=models.ReelStatus.generating)
    db_session.add(reel); db_session.commit()
    src = SimpleNamespace(api_key="k", last_call_was_generated=True,
                          generate=MagicMock(return_value=SimpleNamespace(duration_s=0.0)))
    AS._generate_gated_hf_asset(db_session, reel.id, "asset_hf_image", src, "q", lambda r: 0.5)
    ev = db_session.query(models.StageEvent).one()
    assert ev.cut_id is None and ev.detail == {"cache_hit": False} and ev.cost_usd == 0.5


def test_gated_hf_call_forwards_query_case_preserved():
    src = SimpleNamespace(api_key="k", last_call_was_generated=False, generate=MagicMock(return_value=None))
    with patch(f"{_MOD}.record_stage") as rs:
        rs.return_value.__enter__.return_value = SimpleNamespace(detail={}, cost_usd=None)
        AS._generate_gated_hf_asset(MagicMock(), 1, "s", src, "Exact Q", lambda r: 0.0)
    src.generate.assert_called_once_with("Exact Q")


# ── sixth pass: license wiring, wrong-shape bodies, Pexels ordering/rename ───
def _meta(short, url):
    m = {"LicenseShortName": {"value": short}}
    if url:
        m["LicenseUrl"] = {"value": url}
    return m


# 1. search() must wire safe_to_publish from the license MAPPING, not from "has a license url"
@pytest.mark.parametrize("short,url,safe", [
    ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/", False),   # url present, NOT permissive
    ("CC BY 4.0", None, True),                                                     # permissive, no url
])
def test_wikipedia_search_safe_flag_comes_from_the_license_not_url_presence(tmp_path, short, url, safe):
    result, _ = _wiki(tmp_path, summary=_summary(), downloads={_ORIG: _bytes_resp()},
                      license_meta=_meta(short, url))
    assert result.license_str == short and result.safe_to_publish is safe


# 2. wrong-SHAPE (valid JSON) license / opensearch bodies must still fail closed, never raise
@pytest.mark.parametrize("body", [[], None, "x", {"query": None}, {"query": {"pages": []}}])
def test_wikipedia_license_wrong_shape_json_is_unknown_and_unsafe(tmp_path, body):
    def fake_get(url, **kw):
        a = (kw.get("params") or {}).get("action")
        if a == "opensearch":
            return _json_resp(["x", ["Lionel Messi"], [], []])
        if a == "query":
            return _json_resp(body)
        if url.startswith(WikipediaImageSource._SUMMARY):
            return _json_resp(_summary())
        return _bytes_resp(b"i")
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
        r = WikipediaImageSource(tmp_path).search("Lionel Messi")
    assert r is not None and r.license_str == "unknown" and r.safe_to_publish is False


def test_wikipedia_opensearch_null_body_is_a_miss(tmp_path):
    def fake_get(url, **kw):
        return _json_resp(None)
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
        assert WikipediaImageSource(tmp_path).search("Lionel Messi") is None


# 3. a cached THUMBNAIL (not just a cached original) must be reused when the original fails
def test_wikipedia_cached_thumbnail_is_reused_without_redownload(tmp_path):
    orig, thumb = "https://u/x/Messi.png", "https://u/x/320px-Messi.jpg"
    summ = {"pageid": 5, "originalimage": {"source": orig}, "thumbnail": {"source": thumb}}
    (tmp_path / "wiki_5.jpg").write_bytes(b"cached")
    result, calls = _wiki(tmp_path, summary=summ, downloads={orig: httpx.ConnectError("x")})
    assert result.local_path.read_bytes() == b"cached"
    assert thumb not in calls


# 4. Pexels: first qualifying video in API (relevance) order wins
@pytest.mark.parametrize("durs", [[8, 20, 12], [20, 8, 12]])
def test_pexels_first_qualifying_video_wins_not_sorted_by_duration(tmp_path, durs):
    vids = [_video(i + 1, [_vf(1080, 1920)], duration=d) for i, d in enumerate(durs)]
    result, _, _ = _pexels(tmp_path, vids)
    assert result.source_ref == "1"


# 5. Pexels: fractional min duration is not truncated
def test_pexels_fractional_minimum_is_not_truncated(tmp_path):
    src = PexelsVideoSource("k", tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)], duration=5.2)]})), \
            patch(f"{_MOD}.httpx.stream", _stream_factory([])):
        assert src.search("q", 5.5) is None


# 6. Pexels: long / unicode query verbatim; filename keyed by VIDEO id even if rendition has its own id
def test_pexels_long_query_verbatim_and_filename_uses_video_id(tmp_path):
    q = "Lionel Messi celebrating a last-minute winner in front of a roaring Buenos Aires crowd, café"
    vid = _video(7, [{"id": 999, "width": 1080, "height": 1920, "link": "https://cdn/a.mp4"}])
    src = PexelsVideoSource("k", tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [vid]})) as get, \
            patch(f"{_MOD}.httpx.stream", _stream_factory([])):
        r = src.search(q, 5.0)
    assert get.call_args.kwargs["params"]["query"] == q
    assert r.local_path.name == "pexels_7.mp4"


# 7. Pexels: a failing final rename must be skipped like any other download failure, tmp cleaned
def test_pexels_failed_rename_is_a_skip_not_a_crash_and_leaves_no_tmp(tmp_path):
    src = PexelsVideoSource("k", tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]})), \
            patch(f"{_MOD}.httpx.stream", _stream_factory([])), \
            patch(f"{_MOD}.os.replace", side_effect=OSError("exdev")):
        assert src.search("q", 5.0) is None
    assert list(tmp_path.iterdir()) == []
