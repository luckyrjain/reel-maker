"""Characterization tests for the asset sourcers' selection logic
(engine/render/asset_sourcer.py): which Pexels file is chosen, which Wikipedia image
URL wins and how it is cached, how a Wikimedia license string maps to
`safe_to_publish`, and HuggingFace's content-type / extension / cache decisions.

Before this file none of this was tested: only the HF `last_call_was_generated` flag and
Wikipedia's license-title decoding were. The `safe_to_publish` mapping in particular is
the input to the publish gate, so a wrong branch here is a licensing bug, not a cosmetic
one. HTTP is faked at `engine.render.asset_sourcer.httpx`; nothing touches the network.
"""
import hashlib
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from engine.render.asset_sourcer import (
    _FHD,
    _choose_video_file,
    _image_extension,
    _license_from_extmetadata,
    HuggingFaceImageSource,
    HuggingFaceVideoSource,
    PexelsVideoSource,
    WikipediaImageSource,
)

_MOD = "engine.render.asset_sourcer"


# ── fakes ────────────────────────────────────────────────────────────────────

def _json_resp(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    if status >= 400 and status != 429:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _bytes_resp(content=b"img", status=200):
    resp = _json_resp(None, status)
    resp.content = content
    return resp


def _vf(width, height, link=None):
    """A Pexels video_files entry; `link` defaults to a URL that encodes the size."""
    return {"width": width, "height": height, "link": link or f"https://videos.pexels.com/video-files/{width}x{height}.mp4"}


def _video(vid_id, files, duration=10):
    return {"id": vid_id, "duration": duration, "video_files": files}


def _stream_factory(streamed_urls, fail_urls=()):
    """Stand-in for httpx.stream: records the URL, optionally fails on __enter__."""
    @contextmanager
    def fake_stream(method, url, **kwargs):
        streamed_urls.append(url)
        if url in fail_urls:
            raise httpx.ConnectError("down")
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.iter_bytes.return_value = iter([b"vid", b"eo"])
        yield r
    return fake_stream


def _pexels(tmp_path, videos, *, fail_urls=()):
    """Run PexelsVideoSource.search() against a fake API; returns (result, streamed, get)."""
    streamed: list[str] = []
    source = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": videos})) as get, \
            patch(f"{_MOD}.httpx.stream", _stream_factory(streamed, fail_urls)):
        result = source.search("messi", min_duration_s=5.0)
    return result, streamed, get


@pytest.fixture(autouse=True)
def _wikipedia_downloads_via_get_fakes():
    """Serve `httpx.stream` from whatever `httpx.get` is patched to in these legacy fakes.

    Wikipedia image downloads used to be `httpx.get(...)`; they are `httpx.stream(...)` now (so
    the body can be size-capped), but these tests' URL-dispatching fakes still describe a download
    as a `_bytes_resp` returned from `httpx.get`. This adapts one to the other so the fakes keep
    pinning the same selection/caching/429 behavior; the streaming-specific guards (host check,
    redirects, size cap) are tested in test_sourcer_download_guards.py with real stream fakes.
    Pexels tests patch `httpx.stream` themselves, which takes precedence over this.
    """
    @contextmanager
    def stream_from_get(method, url, **kwargs):
        resp = httpx.get(url, **kwargs)
        content = resp.content
        resp.headers = {}
        resp.iter_bytes = lambda chunk_size=None: iter([content])
        yield resp

    with patch(f"{_MOD}.httpx.stream", stream_from_get):
        yield


# ── PexelsVideoSource.search ─────────────────────────────────────────────────

def test_pexels_without_api_key_makes_no_request(tmp_path):
    source = PexelsVideoSource(api_key="", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get") as get:
        assert source.search("messi", 5.0) is None
    get.assert_not_called()


def test_pexels_request_failure_returns_none(tmp_path):
    source = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", side_effect=httpx.ConnectError("down")):
        assert source.search("messi", 5.0) is None
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({}, status=500)):
        assert source.search("messi", 5.0) is None


def test_pexels_asks_for_portrait_medium_with_the_key_in_the_header(tmp_path):
    _, _, get = _pexels(tmp_path, [])
    kwargs = get.call_args.kwargs
    assert kwargs["headers"] == {"Authorization": "k"}
    assert kwargs["params"] == {
        "query": "messi", "per_page": 15, "orientation": "portrait", "size": "medium",
    }


def test_pexels_no_videos_returns_none(tmp_path):
    result, streamed, _ = _pexels(tmp_path, [])
    assert result is None and streamed == []


def test_pexels_skips_videos_shorter_than_the_minimum(tmp_path):
    videos = [_video(1, [_vf(1080, 1920)], duration=3), _video(2, [_vf(1080, 1920)], duration=8)]
    result, _, _ = _pexels(tmp_path, videos)
    assert result.source_ref == "2"


def test_pexels_exactly_the_minimum_duration_qualifies(tmp_path):
    result, _, _ = _pexels(tmp_path, [_video(1, [_vf(1080, 1920)], duration=5)])
    assert result.source_ref == "1"


def test_pexels_skips_a_video_with_no_files(tmp_path):
    result, _, _ = _pexels(tmp_path, [_video(1, []), _video(2, [_vf(1080, 1920)])])
    assert result.source_ref == "2"


def test_pexels_prefers_the_tallest_portrait_file_within_fhd(tmp_path):
    files = [_vf(720, 1280), _vf(1080, 1920), _vf(2160, 3840)]
    _, streamed, _ = _pexels(tmp_path, [_video(1, files)])
    assert streamed == ["https://videos.pexels.com/video-files/1080x1920.mp4"]


def test_pexels_with_only_over_fhd_portrait_takes_the_smallest_one(tmp_path):
    files = [_vf(2880, 5120), _vf(2160, 3840)]
    _, streamed, _ = _pexels(tmp_path, [_video(1, files)])
    assert streamed == ["https://videos.pexels.com/video-files/2160x3840.mp4"]


def test_pexels_square_counts_as_portrait(tmp_path):
    # The landscape file is taller than the square one, so if the square were NOT portrait
    # the no-portrait branch (tallest within FHD) would pick the landscape file instead.
    files = [_vf(1080, 1080), _vf(1280, 1100)]
    _, streamed, _ = _pexels(tmp_path, [_video(1, files)])
    assert streamed == ["https://videos.pexels.com/video-files/1080x1080.mp4"]


def test_pexels_with_no_portrait_takes_the_tallest_landscape_within_fhd(tmp_path):
    files = [_vf(1280, 720), _vf(1920, 1080), _vf(3840, 2160)]
    _, streamed, _ = _pexels(tmp_path, [_video(1, files)])
    assert streamed == ["https://videos.pexels.com/video-files/1920x1080.mp4"]


def test_pexels_with_nothing_within_fhd_and_no_portrait_takes_the_first_file(tmp_path):
    files = [_vf(5120, 2880), _vf(3840, 2160)]
    _, streamed, _ = _pexels(tmp_path, [_video(1, files)])
    assert streamed == ["https://videos.pexels.com/video-files/5120x2880.mp4"]


def test_pexels_a_portrait_file_beats_a_taller_landscape_one(tmp_path):
    files = [_vf(1080, 1920), _vf(1920, 1080), _vf(3840, 2160)]
    _, streamed, _ = _pexels(tmp_path, [_video(1, files)])
    assert streamed == ["https://videos.pexels.com/video-files/1080x1920.mp4"]


def test_pexels_chosen_file_without_a_link_skips_to_the_next_video(tmp_path):
    no_link = {"width": 1080, "height": 1920, "link": None}
    result, streamed, _ = _pexels(tmp_path, [_video(1, [no_link]), _video(2, [_vf(720, 1280)])])
    assert result.source_ref == "2"
    assert streamed == ["https://videos.pexels.com/video-files/720x1280.mp4"]


def test_pexels_download_failure_skips_to_the_next_video_and_leaves_no_partial(tmp_path):
    videos = [
        _video(1, [_vf(1080, 1920, link="https://videos.pexels.com/video-files/bad.mp4")]),
        _video(2, [_vf(1080, 1920, link="https://videos.pexels.com/video-files/good.mp4")]),
    ]
    result, streamed, _ = _pexels(tmp_path, videos, fail_urls={"https://videos.pexels.com/video-files/bad.mp4"})
    assert result.source_ref == "2"
    assert streamed == ["https://videos.pexels.com/video-files/bad.mp4", "https://videos.pexels.com/video-files/good.mp4"]
    assert not (tmp_path / "pexels_1.mp4").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_pexels_every_download_failing_returns_none(tmp_path):
    result, _, _ = _pexels(
        tmp_path, [_video(1, [_vf(1080, 1920, link="https://videos.pexels.com/video-files/bad.mp4")])],
        fail_urls={"https://videos.pexels.com/video-files/bad.mp4"},
    )
    assert result is None


def test_pexels_result_fields_and_cache_hit_skips_the_download(tmp_path):
    result, streamed, _ = _pexels(tmp_path, [_video(7, [_vf(1080, 1920)], duration=12)])
    assert (result.source, result.source_ref) == ("pexels", "7")
    assert result.local_path == tmp_path / "pexels_7.mp4"
    assert result.local_path.read_bytes() == b"video"
    assert result.duration_s == 12.0
    assert result.license_str == "pexels_free"
    assert result.license_url == "https://www.pexels.com/license/"
    assert result.attribution is None
    assert result.safe_to_publish is True
    assert len(streamed) == 1

    again, streamed_again, _ = _pexels(tmp_path, [_video(7, [_vf(1080, 1920)], duration=12)])
    assert again.local_path == result.local_path
    assert streamed_again == []


# ── WikipediaImageSource.search ──────────────────────────────────────────────

_ORIG = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Messi.jpg"
_THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Messi.jpg/320px-Messi.jpg"


def _wiki(tmp_path, *, summary, downloads=None, opensearch=None, license_meta=None,
          sleeps=None):
    """Run WikipediaImageSource.search("Lionel Messi") against a URL-dispatching fake.

    `downloads` maps image URL -> response (or exception) and records requests in `calls`.
    """
    calls: list[str] = []
    downloads = downloads if downloads is not None else {}
    if opensearch is None:
        opensearch = ["Lionel Messi", ["Lionel Messi"], [], []]
    if license_meta is None:
        license_meta = {
            "LicenseShortName": {"value": "CC BY 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
            "Artist": {"value": "<a href='x'>Some Photographer</a>"},
        }

    def fake_get(url, **kwargs):
        calls.append(url)
        params = kwargs.get("params") or {}
        if url == WikipediaImageSource._SEARCH and params.get("action") == "opensearch":
            if isinstance(opensearch, Exception):
                raise opensearch
            return _json_resp(opensearch)
        if url == WikipediaImageSource._SEARCH and params.get("action") == "query":
            return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": license_meta}]}}}})
        if url.startswith(WikipediaImageSource._SUMMARY):
            if isinstance(summary, Exception):
                raise summary
            return _json_resp(summary)
        outcome = downloads[url]
        if isinstance(outcome, list):          # successive responses for the same URL
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    source = WikipediaImageSource(tmp_path)
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get), \
            patch(f"{_MOD}.time.sleep") as sleep:
        result = source.search("Lionel Messi")
    if sleeps is not None:
        sleeps.extend(c.args[0] for c in sleep.call_args_list)
    return result, calls


def _summary(original=_ORIG, thumbnail=_THUMB, pageid=123):
    data = {"pageid": pageid}
    if original:
        data["originalimage"] = {"source": original}
    if thumbnail:
        data["thumbnail"] = {"source": thumbnail}
    return data


def test_wikipedia_no_opensearch_hit_returns_none(tmp_path):
    result, calls = _wiki(tmp_path, summary=_summary(), opensearch=["x", [], [], []])
    assert result is None
    assert not any(c.startswith(WikipediaImageSource._SUMMARY) for c in calls)


def test_wikipedia_opensearch_failure_returns_none(tmp_path):
    result, _ = _wiki(tmp_path, summary=_summary(), opensearch=httpx.ConnectError("down"))
    assert result is None


def test_wikipedia_summary_failure_returns_none(tmp_path):
    result, _ = _wiki(tmp_path, summary=httpx.ConnectError("down"))
    assert result is None


def test_wikipedia_page_without_images_returns_none(tmp_path):
    result, _ = _wiki(tmp_path, summary=_summary(original=None, thumbnail=None))
    assert result is None


def test_wikipedia_prefers_the_original_image_and_names_the_file_by_page_id(tmp_path):
    result, calls = _wiki(tmp_path, summary=_summary(), downloads={_ORIG: _bytes_resp(b"orig")})
    assert result.local_path == tmp_path / "wiki_123.jpg"
    assert result.local_path.read_bytes() == b"orig"
    assert (result.source, result.source_ref) == ("wikipedia", "123")
    assert result.duration_s == 0.0
    assert _THUMB not in calls


def test_wikipedia_falls_back_to_the_thumbnail_when_the_original_fails(tmp_path):
    result, calls = _wiki(
        tmp_path, summary=_summary(),
        downloads={_ORIG: _bytes_resp(status=500), _THUMB: _bytes_resp(b"thumb")},
    )
    assert result.local_path.read_bytes() == b"thumb"
    assert calls.index(_ORIG) < calls.index(_THUMB)


def test_wikipedia_429_is_retried_once_after_a_two_second_pause(tmp_path):
    sleeps: list[float] = []
    result, calls = _wiki(
        tmp_path, summary=_summary(thumbnail=None),
        downloads={_ORIG: [_bytes_resp(status=429), _bytes_resp(b"ok")]}, sleeps=sleeps,
    )
    assert result.local_path.read_bytes() == b"ok"
    assert sleeps == [2.0]
    assert calls.count(_ORIG) == 2


def test_wikipedia_429_twice_moves_on_to_the_thumbnail(tmp_path):
    sleeps: list[float] = []
    result, calls = _wiki(
        tmp_path, summary=_summary(),
        downloads={
            _ORIG: [_bytes_resp(status=429), _bytes_resp(status=429)],
            _THUMB: _bytes_resp(b"thumb"),
        },
        sleeps=sleeps,
    )
    assert result.local_path.read_bytes() == b"thumb"
    assert sleeps == [2.0]


def test_wikipedia_every_download_failing_returns_none(tmp_path):
    result, _ = _wiki(
        tmp_path, summary=_summary(),
        downloads={_ORIG: httpx.ConnectError("x"), _THUMB: _bytes_resp(status=404)},
    )
    assert result is None


@pytest.mark.parametrize("url,ext", [
    ("https://upload.wikimedia.org/y/P.PNG", "png"),
    ("https://upload.wikimedia.org/y/P.webp?width=300", "webp"),
    ("https://upload.wikimedia.org/y/P.jpeg", "jpeg"),
    ("https://upload.wikimedia.org/y/P.svg", "jpg"),
    ("https://upload.wikimedia.org/y/P", "jpg"),
])
def test_wikipedia_file_extension_is_normalized_to_a_known_image_type(tmp_path, url, ext):
    result, _ = _wiki(
        tmp_path, summary=_summary(original=url, thumbnail=None), downloads={url: _bytes_resp()},
    )
    assert result.local_path.name == f"wiki_123.{ext}"


def test_wikipedia_a_cached_file_is_reused_without_downloading(tmp_path):
    (tmp_path / "wiki_123.jpg").write_bytes(b"cached")
    result, calls = _wiki(tmp_path, summary=_summary())
    assert result.local_path.read_bytes() == b"cached"
    assert _ORIG not in calls


def test_wikipedia_missing_pageid_falls_back_to_the_underscored_title(tmp_path):
    summary = _summary()
    del summary["pageid"]
    result, _ = _wiki(tmp_path, summary=summary, downloads={_ORIG: _bytes_resp()})
    assert result.source_ref == "Lionel_Messi"


def test_wikipedia_license_metadata_travels_with_the_asset(tmp_path):
    result, _ = _wiki(tmp_path, summary=_summary(), downloads={_ORIG: _bytes_resp()})
    assert result.license_str == "CC BY 4.0"
    assert result.license_url == "https://creativecommons.org/licenses/by/4.0/"
    assert result.attribution == "Some Photographer"      # HTML stripped
    assert result.safe_to_publish is True


def test_wikipedia_thumbnail_only_page_gets_no_license_lookup_and_is_unsafe(tmp_path):
    """No original means no filename to look up: license unknown, NOT safe to publish."""
    result, calls = _wiki(
        tmp_path, summary=_summary(original=None), downloads={_THUMB: _bytes_resp()},
    )
    assert result.license_str == "unknown"
    assert result.safe_to_publish is False
    assert result.license_url is None and result.attribution is None
    assert not any(c == WikipediaImageSource._SEARCH for c in calls[1:])


# ── WikipediaImageSource._fetch_license: license string -> safe_to_publish ───

def _license_for(short, tmp_path):
    meta = {"LicenseShortName": {"value": short}} if short is not None else {}
    source = WikipediaImageSource(tmp_path)
    payload = {"query": {"pages": {"1": {"imageinfo": [{"extmetadata": meta}]}}}}
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp(payload)):
        return source._fetch_license("Messi.jpg")


@pytest.mark.parametrize("short,safe", [
    ("CC0", True),
    ("cc-0", True),
    ("Public domain", True),
    ("CC BY 2.0", True),
    ("CC BY 4.0", True),
    ("CC BY", True),
    ("cc by 4.0", True),                  # case-insensitive
    ("CC BY-SA 4.0", False),              # share-alike is NOT in the permissive set
    ("CC BY-SA 3.0", False),
    ("CC BY 3.0", False),                 # exact-match set: bare "cc by"/"cc-by", 2.0 and 4.0 only
    ("Fair use", False),
    ("unknown", False),
    ("", False),
])
def test_license_safe_to_publish_mapping(tmp_path, short, safe):
    info = _license_for(short, tmp_path)
    assert info["license"] == short
    assert info["safe_to_publish"] is safe


def test_license_missing_short_name_defaults_to_unknown_and_unsafe(tmp_path):
    info = _license_for(None, tmp_path)
    assert info == {"license": "unknown", "license_url": None, "attribution": "", "safe_to_publish": False}


def test_license_page_without_imageinfo_is_unknown_and_unsafe(tmp_path):
    source = WikipediaImageSource(tmp_path)
    payload = {"query": {"pages": {"-1": {"missing": ""}}}}
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp(payload)):
        info = source._fetch_license("Nope.jpg")
    assert info["license"] == "unknown" and info["safe_to_publish"] is False


def test_license_lookup_failure_is_unknown_and_unsafe(tmp_path):
    source = WikipediaImageSource(tmp_path)
    with patch(f"{_MOD}.httpx.get", side_effect=httpx.ConnectError("down")):
        info = source._fetch_license("Messi.jpg")
    assert info == {"license": "unknown", "license_url": None, "attribution": None, "safe_to_publish": False}


# ── HuggingFace image / video selection decisions ────────────────────────────

def _hf_resp(content_type, content=b"bytes"):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": content_type}
    resp.content = content
    return resp


def test_hf_image_rejects_a_non_image_200_without_writing_or_charging(tmp_path):
    """HF answers 200 with a JSON body while a model is loading."""
    source = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("application/json", b'{"error":"loading"}')):
        assert source.generate("a prompt") is None
    assert source.last_call_was_generated is False
    assert list(tmp_path.glob("hf_*")) == []


def test_hf_image_api_failure_returns_none(tmp_path):
    source = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", side_effect=httpx.ConnectError("down")):
        assert source.generate("a prompt") is None
    assert source.last_call_was_generated is False


def test_hf_image_request_shape_and_result_fields(tmp_path):
    source = HuggingFaceImageSource(api_key="k", model="org/flux", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")) as post:
        result = source.generate("a prompt")
    assert post.call_args.args[0].endswith("/org/flux")
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer k"}
    sent = post.call_args.kwargs["json"]["inputs"]
    assert sent == "a prompt, portrait orientation, vertical format, cinematic, high quality"
    assert result.source == "huggingface"
    assert result.local_path.suffix == ".png"
    assert result.license_str == "generated" and result.safe_to_publish is True
    assert result.duration_s == 0.0


def test_hf_image_cache_key_follows_the_prompt(tmp_path):
    source = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")) as post:
        a = source.generate("one")
        b = source.generate("two")
    assert a.local_path != b.local_path
    assert post.call_count == 2


@pytest.mark.parametrize("content_type,ext", [
    ("video/mp4", "mp4"),
    ("image/gif", "gif"),
    ("image/gif; charset=binary", "gif"),     # "gif" anywhere in the type, not only at the end
    ("application/octet-stream", "mp4"),
])
def test_hf_video_extension_follows_the_content_type(tmp_path, content_type, ext):
    source = HuggingFaceVideoSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp(content_type)):
        result = source.generate("a prompt")
    assert result.local_path.suffix == f".{ext}"
    assert result.source == "huggingface_video"
    assert result.duration_s == 4.0
    assert result.safe_to_publish is True


def test_hf_video_a_cached_gif_counts_as_a_cache_hit(tmp_path):
    source = HuggingFaceVideoSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/gif")) as post:
        first = source.generate("a prompt")
        second = source.generate("a prompt")
    assert first.local_path == second.local_path
    assert first.local_path.suffix == ".gif"
    assert post.call_count == 1
    assert source.last_call_was_generated is False


def test_hf_video_api_failure_returns_none(tmp_path):
    source = HuggingFaceVideoSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", side_effect=httpx.ReadTimeout("slow")):
        assert source.generate("a prompt") is None
    assert source.last_call_was_generated is False


def test_hf_video_without_api_key_makes_no_request(tmp_path):
    source = HuggingFaceVideoSource(api_key="", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post") as post:
        assert source.generate("a prompt") is None
    post.assert_not_called()


# ── the extracted pure decisions, directly ───────────────────────────────────

def _f(w, h):
    return {"width": w, "height": h}


def test_choose_video_file_ladder():
    assert _choose_video_file([_f(720, 1280), _f(1080, 1920), _f(2160, 3840)]) == _f(1080, 1920)
    assert _choose_video_file([_f(2880, 5120), _f(2160, 3840)]) == _f(2160, 3840)
    assert _choose_video_file([_f(1280, 720), _f(1920, 1080), _f(3840, 2160)]) == _f(1920, 1080)
    assert _choose_video_file([_f(5120, 2880), _f(3840, 2160)]) == _f(5120, 2880)


def test_choose_video_file_cap_boundary_is_inclusive_and_configurable():
    assert _FHD == 1920
    assert _choose_video_file([_f(1080, 1920), _f(1088, 1930)]) == _f(1080, 1920)
    assert _choose_video_file([_f(540, 960), _f(720, 1280)], max_height=1000) == _f(540, 960)


def test_choose_video_file_missing_dimensions_count_as_a_portrait_candidate():
    """A file with no size defaults to width 1 / height 1 — a (tiny) square, so portrait —
    and therefore beats a real landscape file; with a different width default it would not."""
    unsized = {"link": "x"}
    assert _choose_video_file([unsized, _f(1280, 720)]) is unsized


def test_license_from_extmetadata_defaults_and_html_stripping():
    assert _license_from_extmetadata({}) == {
        "license": "unknown", "license_url": None, "attribution": "", "safe_to_publish": False,
    }
    info = _license_from_extmetadata({
        "LicenseShortName": {"value": "CC0"},
        "LicenseUrl": {"value": "https://u"},
        "Artist": {"value": "<b>Jane</b> <i>Doe</i>"},
    })
    assert info == {
        "license": "CC0", "license_url": "https://u",
        "attribution": "Jane Doe", "safe_to_publish": True,
    }


@pytest.mark.parametrize("url,ext", [
    ("https://x/a.jpg", "jpg"), ("https://x/a.JPEG", "jpeg"), ("https://x/a.png?x=1", "png"),
    ("https://x/a.webp", "webp"), ("https://x/a.gif", "jpg"), ("https://x/a", "jpg"),
])
def test_image_extension(url, ext):
    assert _image_extension(url) == ext


# ── second pass: request shape, atomic writes, HF fingerprints, set members ──
@pytest.mark.parametrize("short", ["pexels", "pexels_free", "CC-BY", "cc-by"])
def test_license_every_remaining_permissive_set_member_is_safe(tmp_path, short):
    assert _license_for(short, tmp_path)["safe_to_publish"] is True


def test_license_whitespace_padded_value_is_not_safe(tmp_path):
    assert _license_for(" CC0 ", tmp_path)["safe_to_publish"] is False


def test_license_attribution_is_trimmed():
    assert _license_from_extmetadata({"Artist": {"value": "  <b>Jane</b>  "}})["attribution"] == "Jane"


def test_choose_video_file_ranks_by_height_not_width():
    # fhd portrait: tallest wins even though narrower
    assert _choose_video_file([_f(1000, 1500), _f(900, 1900)]) == _f(900, 1900)
    # over-cap portrait: smallest height wins even though wider
    assert _choose_video_file([_f(2000, 3000), _f(1500, 3500)]) == _f(2000, 3000)
    # landscape-only: tallest height wins even though narrower
    assert _choose_video_file([_f(1920, 1080), _f(1400, 1200)]) == _f(1400, 1200)
    # landscape cap boundary is inclusive
    assert _choose_video_file([_f(2560, 1920), _f(1280, 720)]) == _f(2560, 1920)


def test_pexels_missing_duration_is_below_the_minimum_and_types_are_normalized(tmp_path):
    v = {"id": 1, "video_files": [_vf(1080, 1920)]}          # no duration key -> treated as 0
    result, _, _ = _pexels(tmp_path, [v])
    assert result is None
    result, _, _ = _pexels(tmp_path, [_video(2, [_vf(1080, 1920)], duration=5)])
    assert isinstance(result.duration_s, float) and isinstance(result.source_ref, str)


def test_pexels_endpoint_timeouts_and_redirect_options(tmp_path):
    seen = {}

    @contextmanager
    def fake_stream(method, url, **kw):
        seen.update(kw)
        r = MagicMock(); r.iter_bytes.return_value = iter([b"v"]); yield r

    source = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]})) as get, \
            patch(f"{_MOD}.httpx.stream", fake_stream):
        source.search("q", 1.0)
    assert get.call_args.args[0] == "https://api.pexels.com/videos/search"
    assert get.call_args.kwargs["timeout"] == 30.0
    assert seen == {"follow_redirects": False, "timeout": 120.0,   # redirects are followed by hand, hop-checked
                    "headers": {"Accept-Encoding": "identity"}}


def test_pexels_partial_download_leaves_no_tmp_file(tmp_path):
    @contextmanager
    def fake_stream(method, url, **kw):
        r = MagicMock(); r.raise_for_status.return_value = None
        def gen():
            yield b"part"
            raise httpx.ReadError("cut")
        r.iter_bytes.return_value = gen(); yield r
    source = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]})), \
            patch(f"{_MOD}.httpx.stream", fake_stream):
        assert source.search("q", 1.0) is None
    assert list(tmp_path.iterdir()) == []


def test_wikipedia_every_request_carries_the_user_agent_params_and_timeouts(tmp_path):
    seen = []

    def run():
        def fake_get(url, **kw):
            seen.append((url, kw))
            if kw.get("params", {}).get("action") == "opensearch":
                return _json_resp(["Leo Messi", ["Lionel Andrés Messi", "Other"], [], []])
            if kw.get("params", {}).get("action") == "query":
                return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {}}]}}}})
            if "/page/summary/" in url:
                return _json_resp({"pageid": 5, "originalimage": {"source": "https://upload.wikimedia.org/a/Mes%C3%A9_%28x%29.jpg?uselang=en"}})
            return _bytes_resp(b"x")
        with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
            WikipediaImageSource(tmp_path).search("Leo Messi")
    run()
    ua = {"User-Agent": "reel-maker/1.0"}
    assert len(seen) == 4
    assert all(kw["headers"] == ua for _, kw in seen[:3])      # API calls: User-Agent only
    assert seen[0][1]["params"] == {"action": "opensearch", "search": "Leo Messi", "limit": 1, "format": "json"}
    assert seen[0][1]["timeout"] == 10.0
    # summary URL is built from the *canonical* title returned by opensearch, underscored and percent-encoded
    assert seen[1][0].endswith("/page/summary/Lionel_Andr%C3%A9s_Messi")
    # license lookup: decoded filename, query string stripped, File: prefix, imageinfo/extmetadata/json
    assert seen[2][1]["params"] == {"action": "query", "titles": "File:Mesé_(x).jpg",
                                    "prop": "imageinfo", "iiprop": "extmetadata", "format": "json"}
    assert seen[2][1]["timeout"] == 10.0
    assert seen[3][1]["follow_redirects"] is False and seen[3][1]["timeout"] == 30.0
    assert seen[3][1]["headers"] == {**ua, "Accept-Encoding": "identity"}


def test_wikipedia_429_retry_keeps_headers_redirects_and_timeout(tmp_path):
    seen = []
    def fake_get(url, **kw):
        if "/page/summary/" in url: return _json_resp(_summary(thumbnail=None))
        if kw.get("params", {}).get("action") == "opensearch": return _json_resp(["x", ["Lionel Messi"], [], []])
        if kw.get("params", {}).get("action") == "query": return _json_resp({})
        seen.append(kw)
        return _bytes_resp(status=429)
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get), patch(f"{_MOD}.time.sleep"):
        assert WikipediaImageSource(tmp_path).search("Lionel Messi") is None
    assert len(seen) == 2 and all(k["headers"] == {"User-Agent": "reel-maker/1.0", "Accept-Encoding": "identity"} and k["follow_redirects"] is False and k["timeout"] == 30.0 for k in seen)


def test_wikipedia_download_is_written_atomically(tmp_path):
    def boom(path, data): raise OSError("disk")
    with patch(f"{_MOD}._atomic_write", side_effect=boom) as aw:
        result, _ = _wiki(tmp_path, summary=_summary(thumbnail=None), downloads={_ORIG: _bytes_resp(b"abc")})
    assert aw.called and result is None            # a plain write_bytes would have returned a result


def test_hf_image_cache_hit_filename_and_timeout(tmp_path):
    source = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")) as post:
        a = source.generate("p")
        b = source.generate("p")
    assert post.call_count == 1 and source.last_call_was_generated is False
    full = "p, portrait orientation, vertical format, cinematic, high quality"
    fp = hashlib.sha256(full.encode()).hexdigest()[:16]
    assert a.local_path.name == f"hf_{fp}.png" and a.source_ref == fp == b.source_ref
    assert post.call_args.kwargs["timeout"] == 60.0


def test_hf_video_fingerprint_filename_timeout_headers_and_flag(tmp_path):
    source = HuggingFaceVideoSource(api_key="k", model="org/ltx", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")) as post:
        r = source.generate("p")
    full = "p, portrait orientation, vertical format, cinematic, high quality"
    fp = hashlib.sha256(full.encode()).hexdigest()[:16]
    assert r.local_path.name == f"hfvid_{fp}.mp4" and r.source_ref == fp
    assert source.last_call_was_generated is True
    kw = post.call_args.kwargs
    assert post.call_args.args[0].endswith("/org/ltx")
    assert kw["timeout"] == 180.0 and kw["headers"] == {"Authorization": "Bearer k"} and kw["json"] == {"inputs": full}
    assert r.local_path.read_bytes() == b"bytes"


def test_hf_video_missing_content_type_defaults_to_mp4(tmp_path):
    resp = MagicMock(); resp.raise_for_status.return_value = None; resp.headers = {}; resp.content = b"x"
    source = HuggingFaceVideoSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=resp):
        assert source.generate("p").local_path.suffix == ".mp4"


def test_hf_image_missing_content_type_is_rejected(tmp_path):
    resp = MagicMock(); resp.raise_for_status.return_value = None; resp.headers = {}; resp.content = b"x"
    source = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=resp):
        assert source.generate("p") is None


def test_hf_image_content_type_must_start_with_image(tmp_path):
    source = HuggingFaceImageSource(api_key="k", model="m", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf_resp("text/image/png")):
        assert source.generate("p") is None


def test_hf_failures_are_logged(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        with patch(f"{_MOD}.httpx.post", side_effect=httpx.ConnectError("d")):
            HuggingFaceImageSource("k", "m", tmp_path).generate("p")
            HuggingFaceVideoSource("k", "m", tmp_path).generate("p")
    assert sum("generation failed" in r.message for r in caplog.records) == 2


def test_hf_writes_are_atomic(tmp_path):
    with patch(f"{_MOD}._atomic_write", side_effect=OSError("disk")), \
            patch(f"{_MOD}.httpx.post", return_value=_hf_resp("image/png")):
        assert HuggingFaceImageSource("k", "m", tmp_path).generate("p") is None
    with patch(f"{_MOD}._atomic_write", side_effect=OSError("disk")), \
            patch(f"{_MOD}.httpx.post", return_value=_hf_resp("video/mp4")):
        assert HuggingFaceVideoSource("k", "m", tmp_path).generate("p") is None



def test_wikipedia_plus_in_a_filename_reaches_the_license_lookup_unchanged(tmp_path):
    seen = []
    def fake_get(url, **kw):
        seen.append((url, kw))
        a = kw.get("params", {}).get("action")
        if a == "opensearch": return _json_resp(["x", ["Bjarne"], [], []])
        if a == "query": return _json_resp({})
        if "/page/summary/" in url: return _json_resp({"pageid": 1, "originalimage": {"source": "https://u/C++_conf.jpg"}})
        return _bytes_resp()
    with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
        WikipediaImageSource(tmp_path).search("Bjarne")
    assert seen[1][1]["timeout"] == 10.0
    assert seen[2][1]["params"]["titles"] == "File:C++_conf.jpg"


def test_pexels_download_goes_through_a_tmp_file_in_chunks(tmp_path):
    state = {}
    @contextmanager
    def fake_stream(method, url, **kw):
        r = MagicMock(); r.raise_for_status.side_effect = None
        def it(chunk_size):
            state["chunk"] = chunk_size
            state["final_exists_mid_write"] = (tmp_path / "pexels_1.mp4").exists()
            yield b"x"
        r.iter_bytes.side_effect = it
        yield r
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]})), \
            patch(f"{_MOD}.httpx.stream", fake_stream):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is not None
    assert state == {"chunk": 65536, "final_exists_mid_write": False}


def test_pexels_http_error_status_on_the_download_is_a_failure(tmp_path):
    @contextmanager
    def fake_stream(method, url, **kw):
        r = MagicMock(); r.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())
        yield r
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920)])]})), \
            patch(f"{_MOD}.httpx.stream", fake_stream):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None
    assert list(tmp_path.iterdir()) == []


def test_hf_image_without_api_key_makes_no_request(tmp_path):
    with patch(f"{_MOD}.httpx.post") as post:
        assert HuggingFaceImageSource("", "m", tmp_path).generate("p") is None
    post.assert_not_called()
