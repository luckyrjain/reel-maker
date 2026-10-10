"""The sourcers' API calls are streamed and capped while they are read (engine/render/asset_sourcer.py).

Pexels search, the Wikipedia opensearch / summary / imageinfo lookups and both HuggingFace generations
used `httpx.get` / `httpx.post`, which read the WHOLE body before returning: a hostile or broken
upstream could make the worker buffer gigabytes before any size check ran. `_api_get` / `_api_post`
mirror those functions' signatures (so call sites and their tests are unchanged) but go through the
`_http_stream` seam: `Accept-Encoding: identity`, no redirects, at most `limit` bytes read, and a real
`httpx.Response` built from what was read.

These tests carry the `real_api` marker: every other test gets `_api_get` / `_api_post` wired back to
`httpx.get` / `httpx.post` (tests/conftest.py), which is what the older fakes patch.
"""
import json as jsonlib
import logging

import httpx
import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    HuggingFaceImageSource, HuggingFaceVideoSource, PexelsVideoSource, WikipediaImageSource,
)
from tests.test_sourcer_download_guards import PEXELS_OK, WIKI_OK, _Resp, _Stream, _fake_clock, _wiki
from tests.test_sourcer_selection import _summary, _video, _vf

pytestmark = pytest.mark.real_api

URL = "https://api.example/x"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8


def _json_bytes(obj):
    return jsonlib.dumps(obj).encode()


def _with(stream, monkeypatch):
    monkeypatch.setattr(AS, "_http_stream", stream)
    return stream


# ── _api_get / _api_post themselves ──────────────────────────────────────────────────────────

def test_a_response_is_a_real_httpx_response_built_from_what_was_read(monkeypatch):
    stream = _with(_Stream({URL: _Resp(chunks=(b'{"a":', b' 1}'), headers={"content-type": "application/json"})}), monkeypatch)
    resp = AS._api_get(URL, timeout=5.0)
    assert isinstance(resp, httpx.Response) and resp.status_code == 200
    assert resp.json() == {"a": 1} and resp.content == b'{"a": 1}'
    assert resp.headers["Content-Type"] == "application/json"       # case-insensitive, a real Headers


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_an_error_status_is_a_response_that_raise_for_status_rejects(monkeypatch, status):
    _with(_Stream({URL: _Resp(status=status, chunks=(b"{}",))}), monkeypatch)
    resp = AS._api_get(URL, timeout=5.0)
    assert resp.status_code == status
    with pytest.raises(httpx.HTTPStatusError):
        resp.raise_for_status()


def test_the_request_is_get_with_identity_encoding_no_redirects_and_the_callers_arguments(monkeypatch):
    stream = _with(_Stream({URL: _Resp(chunks=(b"{}",))}), monkeypatch)
    AS._api_get(URL, params={"q": "messi"}, headers={"Authorization": "k"}, timeout=7.0)
    (url, kw), = stream.requests
    assert url == URL and kw["params"] == {"q": "messi"} and kw["timeout"] == 7.0
    assert kw["headers"] == {"Authorization": "k", "Accept-Encoding": "identity"}
    assert kw["follow_redirects"] is False


def test_a_post_carries_its_json_body(monkeypatch):
    stream = _with(_Stream({URL: _Resp(chunks=(b"{}",))}), monkeypatch)
    seen = {}
    real = stream.__call__
    AS._api_post(URL, json={"inputs": "a prompt"}, headers={"Authorization": "Bearer k"}, timeout=60.0)
    (url, kw), = stream.requests
    assert kw["json"] == {"inputs": "a prompt"} and kw["headers"]["Authorization"] == "Bearer k"


def test_the_method_is_get_or_post(monkeypatch):
    methods = []
    from contextlib import contextmanager

    @contextmanager
    def fake(method, url, **kw):
        methods.append(method)
        yield _Resp(chunks=(b"{}",))

    monkeypatch.setattr(AS, "_http_stream", fake)
    AS._api_get(URL, timeout=1.0)
    AS._api_post(URL, timeout=1.0)
    assert methods == ["GET", "POST"]


def test_a_body_over_the_limit_is_not_read_to_the_end(monkeypatch):
    resp = _Resp(chunks=tuple(b"x" * 10 for _ in range(1000)))
    _with(_Stream({URL: resp}), monkeypatch)
    with pytest.raises(AS._TooLarge):
        AS._api_get(URL, timeout=5.0, limit=25)
    assert resp.chunks_read == 3                                    # 10, 20, 30 > 25: stopped, 997 chunks unread


def test_a_declared_length_over_the_limit_is_refused_before_reading(monkeypatch):
    resp = _Resp(chunks=(b"x",), headers={"content-length": "26"})
    _with(_Stream({URL: resp}), monkeypatch)
    with pytest.raises(AS._TooLarge):
        AS._api_get(URL, timeout=5.0, limit=25)
    assert resp.chunks_read == 0


def test_a_body_exactly_at_the_limit_is_returned(monkeypatch):
    _with(_Stream({URL: _Resp(chunks=(b"x" * 25,), headers={"content-length": "25"})}), monkeypatch)
    assert len(AS._api_get(URL, timeout=5.0, limit=25).content) == 25


def test_the_default_limit_is_the_json_one(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_API_JSON_BYTES", 10)
    _with(_Stream({URL: _Resp(chunks=(b"x" * 11,))}), monkeypatch)
    with pytest.raises(AS._TooLarge):
        AS._api_get(URL, timeout=5.0)


@pytest.mark.parametrize("enc", ["gzip", "br", "deflate"])
def test_an_encoded_response_is_refused(monkeypatch, enc):
    _with(_Stream({URL: _Resp(chunks=(b"x",), headers={"content-encoding": enc})}), monkeypatch)
    with pytest.raises(ValueError, match="Content-Encoding"):
        AS._api_get(URL, timeout=5.0)


def test_too_large_is_a_value_error_so_existing_handlers_degrade_it():
    assert issubclass(AS._TooLarge, ValueError)


def test_a_failing_request_propagates_its_error(monkeypatch):
    _with(_Stream({URL: httpx.ConnectError("down")}), monkeypatch)
    with pytest.raises(httpx.ConnectError):
        AS._api_get(URL, timeout=5.0)


# ── through real httpx objects (not just the dict-based fakes) ───────────────────────────────

def _real_stream(handler):
    from contextlib import contextmanager

    @contextmanager
    def stream(method, url, **kw):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with client.stream(method, url, **kw) as r:
                yield r
    return stream


def test_real_httpx_a_huge_body_is_cut_off_without_being_buffered(monkeypatch):
    produced = []

    def body():
        for i in range(10_000):
            produced.append(i)
            yield b"x" * 1024

    monkeypatch.setattr(AS, "_http_stream", _real_stream(lambda req: httpx.Response(200, content=body())))
    with pytest.raises(AS._TooLarge):
        AS._api_get(URL, timeout=5.0, limit=4096)
    assert len(produced) < 10                                       # ~10 MB offered, ~5 KB pulled


def test_real_httpx_headers_params_and_json_reach_the_wire(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(method=req.method, url=str(req.url), accept=req.headers.get("accept-encoding"),
                    auth=req.headers.get("authorization"), body=req.content)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(AS, "_http_stream", _real_stream(handler))
    resp = AS._api_post(URL, params={"a": "1"}, headers={"Authorization": "Bearer k"}, json={"inputs": "p"}, timeout=5.0)
    assert resp.json() == {"ok": True}
    assert seen["method"] == "POST" and seen["url"] == URL + "?a=1"
    assert seen["accept"] == "identity" and seen["auth"] == "Bearer k" and jsonlib.loads(seen["body"]) == {"inputs": "p"}


def test_real_httpx_a_redirect_is_not_followed(monkeypatch):
    def handler(req):
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

    monkeypatch.setattr(AS, "_http_stream", _real_stream(handler))
    resp = AS._api_get(URL, timeout=5.0)
    assert resp.status_code == 302
    with pytest.raises(httpx.HTTPStatusError):
        resp.raise_for_status()


# ── end to end through the call sites, with no httpx.get/post patched at all ─────────────────

def test_pexels_a_whole_search_and_download_go_through_the_streaming_path(tmp_path, monkeypatch):
    api = "https://api.pexels.com/videos/search"
    videos = _json_bytes({"videos": [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])]})
    stream = _with(_Stream({api: _Resp(chunks=(videos,)), PEXELS_OK: _Resp(chunks=(MP4,))}), monkeypatch)
    result = PexelsVideoSource("k", tmp_path).search("messi", 1.0)
    assert result.local_path.read_bytes() == MP4 and stream.urls == [api, PEXELS_OK]
    assert stream.requests[0][1]["headers"]["Authorization"] == "k"
    assert stream.requests[0][1]["params"]["query"] == "messi"


def test_pexels_an_oversized_search_response_is_never_buffered_or_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_API_JSON_BYTES", 64)
    api = "https://api.pexels.com/videos/search"
    resp = _Resp(chunks=tuple(b'{"videos": []}' for _ in range(500)))
    _with(_Stream({api: resp}), monkeypatch)
    assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None
    assert resp.chunks_read <= 6


def test_wikipedia_the_lookups_and_the_download_all_stream(tmp_path, monkeypatch):
    search = WikipediaImageSource._SEARCH
    summary = f"{WikipediaImageSource._SUMMARY}/Lionel_Messi"
    stream = _with(_Stream({
        search: [_Resp(chunks=(_json_bytes(["Lionel Messi", ["Lionel Messi"], [], []]),)),
                 _Resp(chunks=(_json_bytes({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {
                     "LicenseShortName": {"value": "CC BY 4.0"}}}]}}}}),))],
        summary: _Resp(chunks=(_json_bytes(_summary(thumbnail=None)),)),
        WIKI_OK: _Resp(chunks=(b"\xff\xd8\xff\xe0" + b"x" * 12,)),
    }), monkeypatch)
    result = WikipediaImageSource(tmp_path).search("Lionel Messi")
    assert result.safe_to_publish is True and result.local_path.read_bytes().startswith(b"\xff\xd8\xff")
    assert [kw["headers"]["User-Agent"] for _, kw in stream.requests[:3]] == ["reel-maker/1.0"] * 3
    assert all(kw["headers"]["Accept-Encoding"] == "identity" for _, kw in stream.requests)


HF = "https://api-inference.huggingface.co/models/org/m"


def test_huggingface_image_streams_posts_and_caches(tmp_path, monkeypatch):
    stream = _with(_Stream({HF: _Resp(chunks=(PNG, b"more"), headers={"content-type": "image/png"})}), monkeypatch)
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    result = src.generate("a prompt")
    assert result.local_path.read_bytes() == PNG + b"more" and src.last_call_was_generated is True
    (url, kw), = stream.requests
    assert kw["json"]["inputs"].startswith("a prompt,") and kw["headers"]["Authorization"] == "Bearer k"
    assert kw["timeout"] == 60.0


def test_huggingface_video_streams_posts_and_caches(tmp_path, monkeypatch):
    stream = _with(_Stream({HF: _Resp(chunks=(MP4,), headers={"content-type": "video/mp4"})}), monkeypatch)
    src = HuggingFaceVideoSource("k", "org/m", tmp_path)
    assert src.generate("a prompt").local_path.read_bytes() == MP4
    assert stream.requests[0][1]["timeout"] == 180.0


@pytest.mark.parametrize("cls,ctype,limit_name", [
    (HuggingFaceImageSource, "image/png", "_MAX_IMAGE_BYTES"),
    (HuggingFaceVideoSource, "video/mp4", "_MAX_VIDEO_BYTES"),
], ids=["image", "video"])
def test_huggingface_an_oversized_body_is_cut_off_while_reading_not_cached_not_billed(tmp_path, monkeypatch, caplog, cls, ctype, limit_name):
    monkeypatch.setattr(AS, limit_name, 50)
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    resp = _Resp(chunks=tuple(b"x" * 10 for _ in range(1000)), headers={"content-type": ctype})
    _with(_Stream({HF: resp}), monkeypatch)
    src = cls("k", "org/m", tmp_path)
    assert src.generate("a prompt") is None
    assert resp.chunks_read <= 6 and list(tmp_path.iterdir()) == [] and src.last_call_was_generated is False
    assert "too large" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]    # expected condition: no traceback


def test_huggingface_each_source_reads_up_to_its_own_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", 20)
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 1000)
    _with(_Stream({HF: _Resp(chunks=(MP4 + b"x" * 200,), headers={"content-type": "video/mp4"})}), monkeypatch)
    assert HuggingFaceVideoSource("k", "org/m", tmp_path).generate("p") is not None
    _with(_Stream({HF: _Resp(chunks=(PNG + b"x" * 200,), headers={"content-type": "image/png"})}), monkeypatch)
    assert HuggingFaceImageSource("k", "org/m", tmp_path / "i").generate("p") is None


def test_a_soft_time_limit_in_an_api_call_still_propagates(tmp_path, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded
    _with(_Stream({"https://api.pexels.com/videos/search": SoftTimeLimitExceeded()}), monkeypatch)
    with pytest.raises(SoftTimeLimitExceeded):
        PexelsVideoSource("k", tmp_path).search("q", 1.0)
