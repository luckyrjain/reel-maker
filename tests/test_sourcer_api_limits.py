"""Size limits on the sourcers' API responses (engine/render/asset_sourcer.py).

The Pexels search, the Wikipedia opensearch / summary / imageinfo lookups and the two HuggingFace
generation calls go through `httpx.get` / `httpx.post`, which read the whole body. Unlike the
downloads they are not streamed (that would route six more calls through the `_http_stream` seam), so
the transient read buffer is NOT bounded here. What IS bounded is what is done with the body: an
oversized one (by its declared Content-Length, or its decoded length) is never handed to `.json()`
(a 100 MB document can take gigabytes to parse) and never written to the asset cache (a 5 GB "image"
filling the disk). A rejected HuggingFace body is neither cached nor billed.
"""
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import (
    HuggingFaceImageSource, HuggingFaceVideoSource, PexelsVideoSource, WikipediaImageSource,
)
from tests.test_sourcer_selection import _MOD, _json_resp, _summary, _video, _vf

LIMIT = 100


def _big_json(payload, *, declared=None, actual=None):
    """A `_json_resp` whose size, not its payload, is the point. `.json` must never be called."""
    resp = _json_resp(payload)
    resp.headers = {"content-length": str(declared)} if declared is not None else {}
    resp.content = b"x" * actual if actual is not None else b"{}"
    return resp


@pytest.fixture(autouse=True)
def small_limits(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_API_JSON_BYTES", LIMIT)
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", LIMIT)
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", LIMIT)


def test_the_json_limit_is_a_sane_number(monkeypatch):
    monkeypatch.undo()
    assert 1024 * 1024 <= AS._MAX_API_JSON_BYTES <= 64 * 1024 * 1024


# ── the check ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers,content,expected", [
    ({"content-length": "101"}, b"", True),                       # declared too large
    ({"content-length": "100"}, b"x" * 100, False),               # exactly the limit
    ({}, b"x" * 101, True),                                       # no/lying header: the real length counts
    ({"content-length": "5"}, b"x" * 101, True),                  # a header that understates (e.g. gzip)
    ({"content-length": "abc"}, b"x" * 10, False),                # unparsable header: fall back to the body
    ({}, b"", False),
])
def test_body_too_large(headers, content, expected):
    resp = MagicMock(headers=headers, content=content)
    assert AS._body_too_large(resp, LIMIT) is expected


def test_body_too_large_tolerates_a_response_without_usable_fields():
    assert AS._body_too_large(MagicMock(headers=None, content=None), LIMIT) is False
    assert AS._body_too_large(object(), LIMIT) is False


# ── Pexels search ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", [{"declared": LIMIT + 1}, {"actual": LIMIT + 1}], ids=["declared", "actual"])
def test_pexels_an_oversized_search_response_is_not_parsed(tmp_path, kw):
    resp = _big_json({"videos": [_video(1, [_vf(1080, 1920)])]}, **kw)
    with patch(f"{_MOD}.httpx.get", return_value=resp):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None
    resp.json.assert_not_called()


def test_pexels_a_search_response_within_the_limit_is_parsed(tmp_path):
    resp = _big_json({"videos": []}, declared=LIMIT, actual=LIMIT)
    with patch(f"{_MOD}.httpx.get", return_value=resp):
        PexelsVideoSource("k", tmp_path).search("q", 1.0)
    resp.json.assert_called_once()


# ── Wikipedia lookups ────────────────────────────────────────────────────────────────────────

def _wiki_get(oversize_stage=None):
    big = {"declared": LIMIT + 1}

    def fake_get(url, **kw):
        action = (kw.get("params") or {}).get("action")
        stage = {"opensearch": "opensearch", "query": "license"}.get(action, "summary")
        payload = {
            "opensearch": ["Lionel Messi", ["Lionel Messi"], [], []],
            "license": {"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {
                "LicenseShortName": {"value": "CC BY 4.0"}}}]}}}},
            "summary": _summary(thumbnail=None),
        }[stage]
        resp = _big_json(payload, **big) if stage == oversize_stage else _big_json(payload, declared=10)
        calls.append((stage, resp))
        return resp

    calls = []
    fake_get.calls = calls
    return fake_get


@pytest.mark.parametrize("stage", ["opensearch", "summary"])
def test_wikipedia_an_oversized_lookup_response_gives_no_result_and_is_not_parsed(tmp_path, stage):
    fake = _wiki_get(stage)
    with patch(f"{_MOD}.httpx.get", side_effect=fake), patch(f"{_MOD}.time.sleep"):
        assert WikipediaImageSource(tmp_path).search("Lionel Messi") is None
    assert [r for s, r in fake.calls if s == stage][0].json.call_count == 0


def test_wikipedia_an_oversized_license_response_means_unknown_and_unsafe():
    fake = _wiki_get("license")
    with patch(f"{_MOD}.httpx.get", side_effect=fake):
        info = WikipediaImageSource.__new__(WikipediaImageSource)._fetch_license("Messi.jpg")
    assert info["license"] == "unknown" and info["safe_to_publish"] is False
    assert [r for s, r in fake.calls if s == "license"][0].json.call_count == 0


# ── HuggingFace ──────────────────────────────────────────────────────────────────────────────

def _hf(content_type, content, declared=None):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": content_type, **({"content-length": str(declared)} if declared else {})}
    resp.content = content
    return resp


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8


@pytest.mark.parametrize("make", [
    lambda: (HuggingFaceImageSource, "image/png", PNG + b"x" * LIMIT),
    lambda: (HuggingFaceVideoSource, "video/mp4", MP4 + b"x" * LIMIT),
], ids=["image", "video"])
@pytest.mark.parametrize("how", ["actual", "declared"])
def test_huggingface_an_oversized_body_is_not_cached_or_billed(tmp_path, make, how, caplog):
    cls, ctype, body = make()
    resp = _hf(ctype, body) if how == "actual" else _hf(ctype, b"small", declared=LIMIT + 1)
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    src = cls("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=resp):
        assert src.generate("a prompt") is None
    assert list(tmp_path.iterdir()) == [] and src.last_call_was_generated is False
    assert "too large" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]     # expected condition: no traceback


@pytest.mark.parametrize("cls,ctype,body", [
    (HuggingFaceImageSource, "image/png", PNG.ljust(LIMIT, b"x")),
    (HuggingFaceVideoSource, "video/mp4", MP4.ljust(LIMIT, b"x")),
], ids=["image", "video"])
def test_huggingface_a_body_exactly_at_the_limit_is_kept(tmp_path, cls, ctype, body):
    assert len(body) == LIMIT
    src = cls("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf(ctype, body, declared=LIMIT)):
        result = src.generate("a prompt")
    assert result.local_path.read_bytes() == body and src.last_call_was_generated is True


def test_huggingface_each_source_uses_its_own_limit(tmp_path, monkeypatch):
    """A video larger than the image limit is fine; the video limit is what applies to it."""
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", 20)
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 1000)
    body = MP4 + b"x" * 200
    src = HuggingFaceVideoSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf("video/mp4", body)):
        assert src.generate("a prompt") is not None


def test_huggingface_the_image_source_is_held_to_the_image_limit_not_the_video_one(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", 20)
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 1000)
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_hf("image/png", PNG + b"x" * 200)):
        assert src.generate("a prompt") is None
