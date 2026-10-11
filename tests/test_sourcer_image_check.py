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


@pytest.mark.parametrize("w,h,ok", [(100, 100, True), (10_000, 1, True), (1, 10_000, True),
                                    (101, 100, False), (100, 101, False), (5_000, 3, False)])
def test_the_cap_is_on_the_pixel_count_not_the_file_size(monkeypatch, w, h, ok):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", 10_000)
    assert AS._image_ok(_img("PNG", (w, h))) is ok


@pytest.mark.parametrize("w,h", [(7072, 7072), (12000, 12000), (50_001, 1000)])
def test_a_huge_declared_size_is_refused_from_the_header_alone(w, h):
    assert AS._image_ok(_png_declaring(w, h)) is False


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


def test_pixels_of_an_over_cap_image_are_never_decoded():
    with patch.object(Image.Image, "load", side_effect=AssertionError("decoded pixels")):
        assert AS._image_ok(_png_declaring(12000, 12000)) is False


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "TIFF", "WEBP"])
def test_a_file_truncated_after_its_header_is_refused(fmt):
    img = Image.effect_noise((200, 200), 80).convert("RGB")          # incompressible: the body is large
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    data = buf.getvalue()
    assert AS._image_ok(data) is True
    assert AS._image_ok(data[: len(data) // 2]) is False


def test_a_truncated_cached_image_is_deleted(tmp_path):
    img = Image.effect_noise((200, 200), 80).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    f = tmp_path / "wiki_1.jpg"
    f.write_bytes(buf.getvalue()[: len(buf.getvalue()) // 2])
    assert AS._cached_media_ok(f, "image") is False and not f.exists()


def test_a_jpeg_is_decoded_at_reduced_scale_not_full_size():
    data = _img("JPEG", (800, 600))
    seen = []
    from PIL import JpegImagePlugin
    real_draft = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        seen.append(size)
        return real_draft(self, mode, size)

    with patch.object(JpegImagePlugin.JpegImageFile, "draft", spy):
        assert AS._image_ok(data) is True
    assert seen == [(100, 75)]


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
    assert [p.name for p in tmp_path.iterdir() if not p.name.endswith(".refused")] == ["wiki_123.jpg"]
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
    assert stream.urls == [WIKI_OK, WIKI_THUMB]
    assert [p for p in tmp_path.iterdir() if not p.name.endswith(".refused")] == []


# ── an oversized JPEG is downscaled instead of dropped for the tiny thumbnail ────────────────────

CAP = 64 * 64


def _jpeg(size, orientation=None):
    img = Image.effect_noise(size, 60).convert("RGB")
    buf = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def _size_and_format(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.size, im.format


def test_an_oversized_jpeg_is_downscaled_to_within_the_cap(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    big = _jpeg((256, 192))
    assert AS._image_ok(big) is False
    fitted = AS._fit_image(big)
    (w, h), fmt = _size_and_format(fitted)
    assert fmt == "JPEG" and w * h <= CAP and w * h > CAP // 8          # reduced, not shrunk to nothing
    assert abs(w / h - 256 / 192) < 0.05                                  # aspect ratio kept
    assert AS._image_ok(fitted) is True


def test_a_jpeg_within_the_cap_is_returned_untouched(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    ok = _jpeg((40, 40))
    assert AS._fit_image(ok) is ok and AS._downscale_jpeg(ok) is None


def test_exif_rotation_is_baked_into_the_downscaled_jpeg(monkeypatch):
    """The re-encode drops the EXIF tag, so an unrotated result would show the photo sideways."""
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    (w, h), _ = _size_and_format(AS._fit_image(_jpeg((256, 128), orientation=6)))
    assert h > w                                                         # landscape + "rotate 90" -> portrait


def _encoded(fmt, size, **kw):
    buf = io.BytesIO()
    img = Image.effect_noise(size, 60).convert("RGBA" if kw.pop("alpha", False) else "RGB")
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


@pytest.mark.parametrize("fmt,expect", [("PNG", "PNG"), ("GIF", "JPEG"), ("TIFF", "JPEG"), ("WEBP", "JPEG")])
def test_a_non_jpeg_over_the_cap_is_downscaled_too(monkeypatch, fmt, expect):
    """PNG stays PNG (lossless); anything else becomes a JPEG. The size window is _MAX_FULL_DECODE_PIXELS."""
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    big = _encoded(fmt, (256, 192))
    assert AS._image_ok(big) is False
    fitted = AS._fit_image(big)
    (w, h), out_fmt = _size_and_format(fitted)
    assert out_fmt == expect and w * h <= CAP and w * h > CAP // 8
    assert abs(w / h - 256 / 192) < 0.1 and AS._image_ok(fitted) is True


def test_a_png_keeps_its_alpha_channel(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    with Image.open(io.BytesIO(AS._fit_image(_encoded("PNG", (256, 192), alpha=True)))) as im:
        assert im.mode == "RGBA"


def test_a_palette_png_is_handled(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    buf = io.BytesIO()
    Image.effect_noise((256, 192), 60).convert("P").save(buf, format="PNG")
    (w, h), fmt = _size_and_format(AS._fit_image(buf.getvalue()))
    assert fmt == "PNG" and w * h <= CAP


def test_a_16_bit_tiff_is_handled(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    buf = io.BytesIO()
    Image.effect_noise((256, 192), 60).convert("I;16").save(buf, format="TIFF")
    (w, h), fmt = _size_and_format(AS._fit_image(buf.getvalue()))
    assert fmt == "JPEG" and w * h <= CAP


def test_a_non_jpeg_past_the_full_decode_window_is_refused_without_being_decoded(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    monkeypatch.setattr(AS, "_MAX_FULL_DECODE_PIXELS", 2 * CAP)
    big = _encoded("PNG", (256, 192))
    with patch.object(Image.Image, "load", side_effect=AssertionError("decoded an image past the window")):
        assert AS._downscale_decoded(big) is None
    with pytest.raises(AS._OversizedImage):
        AS._fit_image(big)


def test_the_full_decode_window_boundary(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    big = _encoded("PNG", (256, 192))
    monkeypatch.setattr(AS, "_MAX_FULL_DECODE_PIXELS", 256 * 192)
    assert AS._downscale_decoded(big) is not None                     # exactly at the window: decoded
    monkeypatch.setattr(AS, "_MAX_FULL_DECODE_PIXELS", 256 * 192 - 1)
    assert AS._downscale_decoded(big) is None


def test_the_full_decode_window_is_eighty_megapixels():
    assert AS._MAX_FULL_DECODE_PIXELS == 80_000_000


def test_downscale_decoded_leaves_a_jpeg_and_an_in_cap_image_alone(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    assert AS._downscale_decoded(_jpeg((256, 192))) is None           # JPEG has its own path
    assert AS._downscale_decoded(_encoded("PNG", (40, 40))) is None   # within the cap: nothing to do


@pytest.mark.parametrize("orientation,landscape_in,portrait_out", [(6, True, True), (8, True, True), (1, True, False),
                                                                    (3, True, False)])
def test_exif_rotation_is_baked_into_a_downscaled_png(monkeypatch, orientation, landscape_in, portrait_out):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    exif = Image.Exif()
    exif[0x0112] = orientation
    (w, h), _ = _size_and_format(AS._fit_image(_encoded("PNG", (256, 128), exif=exif)))
    assert (h > w) is portrait_out


def test_a_truncated_oversized_png_is_not_rescued(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    big = _encoded("PNG", (256, 192))
    assert AS._downscale_decoded(big[: len(big) // 2]) is None
    with pytest.raises(ValueError):
        AS._fit_image(big[: len(big) // 2])


def test_an_animated_gif_is_downscaled_from_its_first_frame(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    frames = [Image.effect_noise((256, 192), 60).convert("P") for _ in range(3)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    (w, h), fmt = _size_and_format(AS._fit_image(buf.getvalue()))
    assert fmt == "JPEG" and w * h <= CAP


def test_a_soft_time_limit_is_not_swallowed_by_the_full_decode(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded
    with patch("PIL.Image.open", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            AS._downscale_decoded(b"x")


def test_a_truncated_oversized_jpeg_is_not_rescued(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    big = _jpeg((256, 192))
    with pytest.raises(ValueError):
        AS._fit_image(big[: len(big) // 2])


def test_a_jpeg_past_pillows_own_bomb_limit_is_not_rescued(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)                 # 2x = 2000 px < 256*192
    with pytest.raises(ValueError):
        AS._fit_image(_jpeg((256, 192)))


def test_an_oversized_jpeg_is_decoded_at_reduced_scale_not_full_size(monkeypatch):
    from PIL import JpegImagePlugin
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    big = _jpeg((256, 192))
    seen = []
    real = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        seen.append(size)
        return real(self, mode, size)

    with patch.object(JpegImagePlugin.JpegImageFile, "draft", spy):
        AS._downscale_jpeg(big)
    assert seen and seen[0] == (64, 48)                                   # 1/4: the smallest reduction that fits the cap


def test_a_soft_time_limit_is_not_swallowed_by_the_downscale(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    with patch("PIL.Image.open", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            AS._downscale_jpeg(b"x")


def test_wikipedia_an_oversized_jpeg_original_is_used_downscaled_not_replaced_by_the_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    stream = _Stream({WIKI_OK: _Resp(chunks=(_jpeg((256, 192)),)), WIKI_THUMB: _Resp(chunks=(_jpeg((16, 12)),))})
    result = _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK]                                       # the thumbnail was never needed
    (w, h), fmt = _size_and_format(result.local_path.read_bytes())
    assert fmt == "JPEG" and w * h <= CAP and w * h > 16 * 12
    assert [p.name for p in tmp_path.iterdir()] == ["wiki_123.jpg"]


def test_wikipedia_an_oversized_png_original_past_the_decode_window_falls_back_to_the_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    monkeypatch.setattr(AS, "_MAX_FULL_DECODE_PIXELS", 2 * CAP)
    buf = io.BytesIO()
    Image.effect_noise((256, 192), 60).convert("RGB").save(buf, format="PNG")
    thumb = _jpeg((16, 12))
    stream = _Stream({WIKI_OK: _Resp(chunks=(buf.getvalue(),)), WIKI_THUMB: _Resp(chunks=(thumb,))})
    result = _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK, WIKI_THUMB] and result.local_path.read_bytes() == thumb


def test_wikipedia_a_downscale_is_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    caplog.set_level(logging.INFO, logger=AS.__name__)
    _wiki(tmp_path, _summary(thumbnail=None), _Stream({WIKI_OK: _Resp(chunks=(_jpeg((256, 192)),))}))
    assert "Downscaled an oversized image" in caplog.text


def test_a_downscaled_result_that_is_still_unusable_is_refused(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    monkeypatch.setattr(AS, "_downscale_jpeg", lambda content: _jpeg((256, 192)))      # still over the cap
    with pytest.raises(ValueError, match="not a readable image"):
        AS._fit_image(_jpeg((256, 192)))


# ── a non-JPEG original that is over the cap is not downloaded again on every search ────────────────

PNG_ORIG = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Messi.png"


def _png_noise(size):
    buf = io.BytesIO()
    Image.effect_noise(size, 60).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _two_searches(tmp_path, monkeypatch, original, thumb, original_url=PNG_ORIG):
    """Two searches of one page whose original is a .png (or `original_url`) and whose thumbnail is a .jpg."""
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    monkeypatch.setattr(AS, "_MAX_FULL_DECODE_PIXELS", 2 * CAP)       # the original is too big to decode here
    summary = _summary(original=original_url)
    first = _Stream({original_url: _Resp(chunks=(original,)), WIKI_THUMB: _Resp(chunks=(thumb,))})
    r1 = _wiki(tmp_path, summary, first)
    second = _Stream({original_url: _Resp(chunks=(original,)), WIKI_THUMB: _Resp(chunks=(thumb,))})
    r2 = _wiki(tmp_path, summary, second)
    return first, r1, second, r2


def _refused(tmp_path):
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(".refused")]


def test_an_oversized_original_is_not_fetched_again_on_the_next_search(tmp_path, monkeypatch):
    thumb = _jpeg((16, 12))
    first, r1, second, r2 = _two_searches(tmp_path, monkeypatch, _png_noise((256, 192)), thumb)
    assert first.urls == [PNG_ORIG, WIKI_THUMB]
    assert second.urls == []                                         # original skipped, thumbnail served from cache
    assert r1.local_path == r2.local_path and r2.local_path.read_bytes() == thumb
    assert r2.local_path.name == "wiki_123.jpg"                      # different extension from the original


def test_the_refusal_is_recorded_next_to_the_would_be_file(tmp_path, monkeypatch):
    _two_searches(tmp_path, monkeypatch, _png_noise((256, 192)), _jpeg((16, 12)))
    refused = _refused(tmp_path)
    assert len(refused) == 1 and refused[0].startswith("wiki_123.png.")
    assert (tmp_path / refused[0]).read_text() == str(CAP)
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_a_changed_cap_forgets_the_refusal(tmp_path, monkeypatch):
    thumb = _jpeg((16, 12))
    orig = _png_noise((256, 192))
    _two_searches(tmp_path, monkeypatch, orig, thumb)
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", 20 * CAP)           # now it fits (256*192 < 81920)
    third = _Stream({PNG_ORIG: _Resp(chunks=(orig,)), WIKI_THUMB: _Resp(chunks=(thumb,))})
    result = _wiki(tmp_path, _summary(original=PNG_ORIG), third)
    assert third.urls == [PNG_ORIG] and result.local_path.read_bytes() == orig


def test_a_corrupt_original_is_not_recorded_as_oversized(tmp_path, monkeypatch):
    """A bad download may be transient: only a definitely-too-big original is remembered."""
    thumb = _jpeg((16, 12))
    first, _, second, _ = _two_searches(tmp_path, monkeypatch, b"\x89PNG\r\n\x1a\n" + b"garbage" * 20, thumb)
    assert _refused(tmp_path) == []
    assert second.urls == [PNG_ORIG]                                 # tried again (the thumbnail is cached)


def test_an_oversized_jpeg_past_pillows_limit_is_recorded_too(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    first, _, second, _ = _two_searches(tmp_path, monkeypatch, _jpeg((256, 192)), _png_noise((16, 12)),
                                        original_url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Messi.jpeg")
    assert second.urls == []


def test_a_failed_marker_write_is_not_fatal(tmp_path, monkeypatch):
    real = AS._atomic_write

    def flaky(path, data):
        if str(path).endswith(".refused"):
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(AS, "_atomic_write", flaky)
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    stream = _Stream({PNG_ORIG: _Resp(chunks=(_png_noise((256, 192)),)), WIKI_THUMB: _Resp(chunks=(_jpeg((16, 12)),))})
    assert _wiki(tmp_path, _summary(original=PNG_ORIG), stream) is not None


def test_a_different_pages_refusal_does_not_block_this_page(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    (tmp_path / "wiki_999.jpg.0123456789ab.refused").write_text(str(CAP))
    stream = _Stream({WIKI_OK: _Resp(chunks=(_jpeg((16, 12)),))})
    assert _wiki(tmp_path, _summary(thumbnail=None), stream) is not None and stream.urls == [WIKI_OK]


def test_remembering_the_original_does_not_block_a_thumbnail_that_shares_its_path(tmp_path, monkeypatch):
    """Both URLs map to wiki_<id>.jpg here (and often in real life): the marker is keyed by URL."""
    thumb = _jpeg((16, 12))
    first, r1, _, _ = _two_searches(tmp_path, monkeypatch, _png_noise((256, 192)), thumb, original_url=WIKI_OK)
    assert r1.local_path.read_bytes() == thumb and first.urls == [WIKI_OK, WIKI_THUMB]


def test_wikipedia_an_oversized_png_original_within_the_window_is_used_downscaled(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    stream = _Stream({PNG_ORIG: _Resp(chunks=(_png_noise((256, 192)),)), WIKI_THUMB: _Resp(chunks=(_jpeg((16, 12)),))})
    result = _wiki(tmp_path, _summary(original=PNG_ORIG), stream)
    assert stream.urls == [PNG_ORIG] and result.local_path.name == "wiki_123.png"
    (w, h), fmt = _size_and_format(result.local_path.read_bytes())
    assert fmt == "PNG" and w * h <= CAP and w * h > 16 * 12
    assert _refused(tmp_path) == []                                   # nothing to remember: it was used


def test_the_big_image_is_shrunk_with_an_integer_reduce_not_a_full_size_resize(monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", CAP)
    seen = []
    real = Image.Image.reduce

    def spy(self, factor, *a, **k):
        seen.append(factor)
        return real(self, factor, *a, **k)

    with patch.object(Image.Image, "reduce", spy):
        AS._downscale_decoded(_encoded("PNG", (256, 192)))
    assert seen == [4]                                                # ceil(sqrt(49152 / 4096))


def test_rounding_up_in_reduce_never_leaves_the_result_over_the_cap(monkeypatch):
    """reduce() rounds dimensions UP: 11x67 at factor 2 is 6x34 = 204 px, over a cap of 200."""
    monkeypatch.setattr(AS, "_MAX_IMAGE_PIXELS", 200)
    monkeypatch.setattr(AS, "_MAX_FULL_DECODE_PIXELS", 10_000)
    (w, h), _ = _size_and_format(AS._downscale_decoded(_encoded("PNG", (11, 67))))
    assert w * h <= 200
