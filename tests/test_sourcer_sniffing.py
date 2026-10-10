"""Downloaded bytes must look like what they claim to be (engine/render/asset_sourcer.py).

The download guards bound WHERE bytes come from, how many and how long; they say nothing about
WHAT arrives. A 200 `text/html`/JSON body (a CDN error page, a captive portal, a compromised
allowlisted host) was written to `pexels_N.mp4` / `wiki_N.jpg` / `hf_*.png` and then cached forever
by the `if path.exists()` check, and an empty 200 body became a 0-byte "video". Every download is now
checked against the magic bytes of the formats the pipeline actually decodes before it is cached:

* images: JPEG, PNG, GIF, WebP, TIFF (SVG, HTML, JSON, BMP, PDF... are refused);
* videos: ISO-BMFF/MP4 (`ftyp`/`moov`/`mdat`/`free`/`wide`/`skip` box at offset 4), WebM, GIF.

These tests run with the REAL check (every other test gets a permissive one, see tests/conftest.py).
"""
from unittest.mock import MagicMock, patch

import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import HuggingFaceImageSource, HuggingFaceVideoSource
from tests.test_sourcer_download_guards import (
    PEXELS_OK, WIKI_OK, WIKI_THUMB, _Resp, _Stream, _pexels, _wiki,
)
from tests.test_sourcer_selection import _MOD, _summary, _video, _vf

pytestmark = pytest.mark.real_media_sniffing

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"x" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"x" * 10
GIF = b"GIF89a" + b"x" * 10
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"x" * 10
TIFF_LE = b"II*\x00" + b"x" * 12
TIFF_BE = b"MM\x00*" + b"x" * 12
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + b"x" * 10
WEBM = b"\x1a\x45\xdf\xa3" + b"x" * 12
HTML = b"<!DOCTYPE html><html><body>Access denied</body></html>"


# ── the check itself ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("head", [JPEG, PNG, GIF, b"GIF87a" + b"x" * 10, WEBP, TIFF_LE, TIFF_BE],
                         ids=["jpeg", "png", "gif89a", "gif87a", "webp", "tiff-le", "tiff-be"])
def test_image_accepts_the_formats_the_pipeline_decodes(head):
    assert AS._sniff_ok("image", head[:16]) is True


@pytest.mark.parametrize("head", [
    b"", b"\xff\xd8", b"\x89PNG", b"GIF8", HTML[:16], b"<?xml version=\"1", b"<svg xmlns=\"http:",
    b'{"error": "model ', b"BM" + b"x" * 14, b"%PDF-1.7\n%\xe2\xe3\xcf\xd3", b"PK\x03\x04" + b"x" * 12,
    b"RIFF\x24\x00\x00\x00WAVEfmt ", b"RIFF\x24\x00\x00\x00AVI LIST", MP4, b" " + JPEG[:15],
    b"XXXX\x24\x00\x00\x00WEBPVP8 ",                       # WEBP at offset 8 without the RIFF container
    b"\x00" * 16, None, 5, "str-not-bytes",
], ids=lambda h: repr(h)[:28])
def test_image_rejects_everything_else(head):
    assert AS._sniff_ok("image", head) is False


@pytest.mark.parametrize("head", [
    MP4, b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00", b"\x00\x00\x00\x08moov" + b"x" * 8,
    b"\x00\x00\x00\x08mdat" + b"x" * 8, b"\x00\x00\x00\x08free" + b"x" * 8,
    b"\x00\x00\x00\x08wide" + b"x" * 8, b"\x00\x00\x00\x08skip" + b"x" * 8, WEBM, GIF,
    b"GIF87a" + b"x" * 10,
], ids=["mp4-ftyp", "mp4-isom", "moov", "mdat", "free", "wide", "skip", "webm", "gif89a", "gif87a"])
def test_video_accepts_mp4_webm_and_gif(head):
    assert AS._sniff_ok("video", head[:16]) is True


@pytest.mark.parametrize("head", [
    b"", b"\x00\x00\x00\x18", b"\x00\x00\x00\x18ftyp"[:7], HTML[:16], b'{"error": "model ', PNG, JPEG,
    b"\x00\x00\x00\x18abcd" + b"x" * 8, b"ftypmp42" + b"x" * 8, None, 5,
], ids=lambda h: repr(h)[:28])
def test_video_rejects_everything_else(head):
    assert AS._sniff_ok("video", head) is False


def test_an_unknown_kind_is_never_ok():
    assert AS._sniff_ok("audio", MP4) is False and AS._sniff_ok("", PNG) is False


def test_the_check_is_the_real_one_in_this_module_and_permissive_elsewhere(request):
    assert request.node.get_closest_marker("real_media_sniffing")
    assert AS._sniff_ok("image", b"placeholder") is False


# ── Pexels ───────────────────────────────────────────────────────────────────────────────────

def test_pexels_a_real_mp4_is_cached(tmp_path):
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])],
                     _Stream({PEXELS_OK: _Resp(chunks=(MP4, b"more"))}))
    assert result.local_path.read_bytes() == MP4 + b"more"


def test_pexels_an_mp4_header_arriving_in_tiny_chunks_is_still_recognised(tmp_path):
    chunks = tuple(bytes([b]) for b in MP4)
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])],
                   _Stream({PEXELS_OK: _Resp(chunks=chunks)})) is not None


@pytest.mark.parametrize("body", [HTML, b'{"error":"rate limited"}', PNG, b"", b"\x00\x00\x00"],
                         ids=["html", "json", "png", "empty", "3-bytes"])
def test_pexels_a_body_that_is_not_a_video_is_discarded_and_never_cached(tmp_path, body, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    chunks = (body,) if body else ()
    bad = "https://videos.pexels.com/video-files/bad.mp4"
    stream = _Stream({bad: _Resp(chunks=chunks), PEXELS_OK: _Resp(chunks=(MP4,))})
    videos = [_video(1, [_vf(1080, 1920, link=bad)]), _video(2, [_vf(1080, 1920, link=PEXELS_OK)])]
    result = _pexels(tmp_path, videos, stream)
    assert result.source_ref == "2"                                  # moved on to the next hit
    assert [p.name for p in tmp_path.iterdir()] == ["pexels_2.mp4"]  # no pexels_1.mp4, no .tmp
    assert "not a valid video" in caplog.text


def test_pexels_every_hit_being_html_returns_none(tmp_path):
    stream = _Stream({PEXELS_OK: _Resp(chunks=(HTML,))})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert list(tmp_path.iterdir()) == []


# ── Wikipedia ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("img", [JPEG, PNG, GIF, WEBP, TIFF_LE], ids=["jpeg", "png", "gif", "webp", "tiff"])
def test_wikipedia_a_real_image_is_cached(tmp_path, img):
    result = _wiki(tmp_path, _summary(thumbnail=None), _Stream({WIKI_OK: _Resp(chunks=(img,))}))
    assert result.local_path.read_bytes() == img


@pytest.mark.parametrize("body", [HTML, b"<svg xmlns='http://www.w3.org/2000/svg'/>", b'{"error":"x"}', b""],
                         ids=["html", "svg", "json", "empty"])
def test_wikipedia_a_bad_original_falls_back_to_a_real_thumbnail(tmp_path, body, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    stream = _Stream({WIKI_OK: _Resp(chunks=(body,) if body else ()), WIKI_THUMB: _Resp(chunks=(PNG,))})
    result = _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK, WIKI_THUMB] and result.local_path.read_bytes() == PNG
    assert [p.name for p in tmp_path.iterdir()] == ["wiki_123.jpg"]
    assert "not a valid image" in caplog.text


def test_wikipedia_when_nothing_downloaded_is_an_image_nothing_is_cached(tmp_path):
    stream = _Stream({WIKI_OK: _Resp(chunks=(HTML,)), WIKI_THUMB: _Resp(chunks=(HTML,))})
    assert _wiki(tmp_path, _summary(), stream) is None
    assert list(tmp_path.iterdir()) == []


# ── HuggingFace ──────────────────────────────────────────────────────────────────────────────

def _post(content_type, content):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": content_type}
    resp.content = content
    return resp


def test_hf_image_a_real_png_is_cached(tmp_path):
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post("image/png", PNG)):
        result = src.generate("a prompt")
    assert result.local_path.read_bytes() == PNG and src.last_call_was_generated is True


@pytest.mark.parametrize("body", [HTML, b'{"error":"model is loading"}', b""], ids=["html", "json", "empty"])
def test_hf_image_a_body_that_is_not_an_image_is_not_cached_or_billed(tmp_path, body, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post("image/png", body)):    # lies about its type
        assert src.generate("a prompt") is None
    assert list(tmp_path.iterdir()) == [] and src.last_call_was_generated is False
    assert "not a valid image" in caplog.text


@pytest.mark.parametrize("content_type,body,ext", [
    ("video/mp4", MP4, "mp4"), ("video/webm", WEBM, "mp4"), ("image/gif", GIF, "gif"),
], ids=["mp4", "webm", "gif"])
def test_hf_video_real_videos_are_cached(tmp_path, content_type, body, ext):
    src = HuggingFaceVideoSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post(content_type, body)):
        result = src.generate("a prompt")
    assert result.local_path.suffix == f".{ext}" and result.local_path.read_bytes() == body
    assert src.last_call_was_generated is True


@pytest.mark.parametrize("body", [HTML, b'{"error":"model is loading"}', PNG, b""], ids=["html", "json", "png", "empty"])
def test_hf_video_a_body_that_is_not_a_video_is_not_cached_or_billed(tmp_path, body, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    src = HuggingFaceVideoSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post("video/mp4", body)):
        assert src.generate("a prompt") is None
    assert list(tmp_path.iterdir()) == [] and src.last_call_was_generated is False
    assert "not a valid video" in caplog.text
