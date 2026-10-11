"""An image must be readable and not enormous before it is cached (engine/render/asset_sourcer.py).

The byte cap (50 MB) says nothing about how many PIXELS a file declares: a 140 KB PNG declaring
12000x12000 px is "small" and passes the magic-byte sniff, yet the compositor's
`Image.open(...).convert("RGB")` allocates ~430 MB for it. `_image_ok` opens the image HEADER with
Pillow (it does not decode pixels) and refuses a file Pillow cannot identify or one declaring more than
`_MAX_IMAGE_PIXELS`. Applied to Wikipedia downloads, HuggingFace images and cached images; a refused
Wikipedia original falls back to the (much smaller) thumbnail.

These tests carry `real_image_check` and use real images; every other test gets a permissive check.
"""
import io
import logging
import struct
import warnings
import zlib
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import HuggingFaceImageSource
from tests.test_sourcer_download_guards import WIKI_OK, WIKI_THUMB, _Resp, _Stream, _wiki
from tests.test_sourcer_selection import _MOD, _summary

pytestmark = pytest.mark.real_image_check


def _img(fmt, size=(8, 8)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


def _png_declaring(width, height):
    """A tiny PNG whose IHDR claims width x height (no pixel data): enough for Pillow to report a size."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")


# ── the check ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF", "WEBP", "TIFF"])
def test_a_real_image_is_ok(fmt):
    assert AS._image_ok(_img(fmt)) is True


def test_a_path_is_checked_like_bytes(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(_img("PNG"))
    assert AS._image_ok(f) is True
    (tmp_path / "b.png").write_bytes(_png_declaring(20000, 20000))
    assert AS._image_ok(tmp_path / "b.png") is False


@pytest.mark.parametrize("data", [
    b"", b"\xff\xd8\xff\xe0" + b"junk" * 10,          # a JPEG signature on garbage
    b"\x89PNG\r\n\x1a\n" + b"nope" * 10, b"GIF89a" + b"\x00" * 4,
    b"<html>Access denied</html>", b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"x" * 8,
    _img("PNG")[:20],                                   # truncated inside the header
], ids=["empty", "jpeg-magic-garbage", "png-magic-garbage", "gif-stub", "html", "webp-stub", "truncated"])
def test_an_unreadable_image_is_refused(data):
    assert AS._image_ok(data) is False


def test_a_missing_file_is_refused(tmp_path):
    assert AS._image_ok(tmp_path / "nope.png") is False


def test_the_pixel_cap_is_fifty_megapixels():
    assert AS._MAX_IMAGE_PIXELS == 50_000_000


@pytest.mark.parametrize("w,h,ok", [(7000, 7000, True), (7071, 7071, True), (7072, 7072, False),
                                    (12000, 12000, False), (50_000, 1, True), (50_001, 1000, False)])
def test_the_cap_is_on_the_pixel_count_not_the_file_size(w, h, ok):
    assert AS._image_ok(_png_declaring(w, h)) is ok


def test_a_declared_size_far_past_pillows_own_bomb_limit_is_refused_not_crashed():
    """> 2x Pillow's MAX_IMAGE_PIXELS raises DecompressionBombError at open: a refusal, never an exception."""
    assert AS._image_ok(_png_declaring(30000, 30000)) is False


def test_the_cap_boundary_is_inclusive(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", 64 * 64)
    assert AS._image_ok(_img("PNG", (64, 64))) is True
    assert AS._image_ok(_img("PNG", (65, 64))) is False


def test_pillows_bomb_warning_does_not_escape_as_an_error(monkeypatch):
    """Pillow warns between 1x and 2x its own limit; with warnings as errors that must not become a refusal
    of an image that is within OUR cap."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert AS._image_ok(_img("PNG", (45, 30))) is True            # 1350 px: Pillow's warning band, under our cap


def test_only_the_header_is_read_pixels_are_never_decoded():
    data = _img("JPEG", (64, 64))
    with patch.object(Image.Image, "load", side_effect=AssertionError("decoded pixels")):
        assert AS._image_ok(data) is True


def test_a_soft_time_limit_is_not_swallowed():
    from celery.exceptions import SoftTimeLimitExceeded
    with patch("PIL.Image.open", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            AS._image_ok(b"anything")


# ── wired into Wikipedia, HuggingFace and the caches ─────────────────────────────────────────

def test_wikipedia_a_huge_original_falls_back_to_a_small_thumbnail(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    thumb = _img("JPEG")
    stream = _Stream({WIKI_OK: _Resp(chunks=(_png_declaring(12000, 12000),)), WIKI_THUMB: _Resp(chunks=(thumb,))})
    result = _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK, WIKI_THUMB] and result.local_path.read_bytes() == thumb
    assert [p.name for p in tmp_path.iterdir()] == ["wiki_123.jpg"]
    assert "not a readable image" in caplog.text


def test_wikipedia_an_unreadable_original_is_refused_even_with_a_valid_signature(tmp_path):
    stream = _Stream({WIKI_OK: _Resp(chunks=(b"\xff\xd8\xff\xe0" + b"junk" * 20,))})
    assert _wiki(tmp_path, _summary(thumbnail=None), stream) is None
    assert list(tmp_path.iterdir()) == []


def test_wikipedia_a_reasonable_image_is_cached(tmp_path):
    img = _img("PNG", (640, 480))
    assert _wiki(tmp_path, _summary(thumbnail=None), _Stream({WIKI_OK: _Resp(chunks=(img,))})).local_path.read_bytes() == img


def test_wikipedia_a_huge_cached_image_is_deleted_and_downloaded_again(tmp_path):
    (tmp_path / "wiki_123.jpg").write_bytes(_png_declaring(12000, 12000))
    small = _img("PNG")
    stream = _Stream({WIKI_OK: _Resp(chunks=(small,))})
    result = _wiki(tmp_path, _summary(thumbnail=None), stream)
    assert result.local_path.read_bytes() == small and stream.urls == [WIKI_OK]


def test_wikipedia_a_good_cached_image_is_served_without_a_request(tmp_path):
    good = _img("PNG")
    (tmp_path / "wiki_123.jpg").write_bytes(good)
    stream = _Stream({})
    assert _wiki(tmp_path, _summary(thumbnail=None), stream).local_path.read_bytes() == good and stream.requests == []


def _post(content):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.headers = {"content-type": "image/png"}
    resp.content = content
    return resp


def test_huggingface_a_huge_image_is_not_cached_or_billed(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post(_png_declaring(12000, 12000))):
        assert src.generate("a prompt") is None
    assert list(tmp_path.iterdir()) == [] and src.last_call_was_generated is False
    assert "not a readable image" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_huggingface_a_reasonable_image_is_cached(tmp_path):
    img = _img("PNG", (512, 512))
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post(img)):
        assert src.generate("a prompt").local_path.read_bytes() == img


def test_huggingface_a_huge_cached_image_is_regenerated(tmp_path):
    import hashlib
    fp = hashlib.sha256(b"a prompt, portrait orientation, vertical format, cinematic, high quality").hexdigest()[:16]
    cached = tmp_path / f"hf_{fp}.png"
    cached.write_bytes(_png_declaring(12000, 12000))
    fresh = _img("PNG")
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    with patch(f"{_MOD}.httpx.post", return_value=_post(fresh)) as post:
        src.generate("a prompt")
    assert post.call_count == 1 and cached.read_bytes() == fresh


def test_videos_are_not_subject_to_the_pixel_check(tmp_path):
    """Only images are opened with Pillow: an MP4 head is not an image and must not be refused for that."""
    from tests.test_sourcer_download_guards import PEXELS_OK, _pexels
    from tests.test_sourcer_selection import _video, _vf
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: _Resp(chunks=(mp4,))}))
    assert result is not None


def test_a_lowered_pillow_limit_does_not_override_our_cap(monkeypatch):
    """Pillow warns (1x-2x its limit) / raises (>2x) on its own; the cap here is the one that decides."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert AS._image_ok(_img("PNG", (45, 30))) is True           # 1350 px: Pillow's warning band, under our cap


# ── a transient failure is not a verdict (security review: it used to delete a paid cache file) ──

import errno


@pytest.mark.parametrize("exc", [OSError(errno.EMFILE, "Too many open files"), OSError(errno.EIO, "I/O error"),
                                 MemoryError()], ids=["emfile", "eio", "memory"])
def test_a_transient_failure_is_raised_not_reported_as_a_bad_image(exc):
    with patch("PIL.Image.open", side_effect=exc):
        with pytest.raises(type(exc)):
            AS._image_ok(b"anything")


def test_a_transient_failure_never_deletes_a_cached_image(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=AS.__name__)
    f = tmp_path / "hf_cache.png"
    f.write_bytes(_img("PNG"))
    with patch("PIL.Image.open", side_effect=OSError(errno.EMFILE, "Too many open files")):
        assert AS._cached_media_ok(f, "image") is True
    assert f.exists()
    assert "Could not check" in caplog.text


def test_a_definitely_bad_cached_image_is_still_deleted(tmp_path):
    f = tmp_path / "hf_cache.png"
    f.write_bytes(_png_declaring(12000, 12000))
    assert AS._cached_media_ok(f, "image") is False and not f.exists()


def test_a_transient_failure_while_checking_a_download_fails_that_candidate_only(tmp_path):
    stream = _Stream({WIKI_OK: _Resp(chunks=(_img("JPEG"),)), WIKI_THUMB: _Resp(chunks=(_img("JPEG"),))})
    calls = []

    def flaky(source):
        calls.append(1)
        if len(calls) == 1:
            raise OSError(errno.EMFILE, "Too many open files")
        return True

    with patch.object(AS, "_image_ok", flaky):
        result = _wiki(tmp_path, _summary(), stream)
    assert result is not None and stream.urls == [WIKI_OK, WIKI_THUMB]


# ── the check is for images only, and thumbnails are checked like originals ─────────────────────

@pytest.mark.real_media_sniffing
def test_a_cached_video_is_never_opened_with_pillow(tmp_path):
    p = tmp_path / "pexels_1.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8)
    assert AS._cached_media_ok(p, "video") is True and p.exists()


def test_wikipedia_a_huge_thumbnail_is_refused_too(tmp_path):
    huge = _png_declaring(12000, 12000)
    stream = _Stream({WIKI_OK: _Resp(chunks=(huge,)), WIKI_THUMB: _Resp(chunks=(huge,))})
    assert _wiki(tmp_path, _summary(), stream) is None
    assert stream.urls == [WIKI_OK, WIKI_THUMB] and list(tmp_path.iterdir()) == []
