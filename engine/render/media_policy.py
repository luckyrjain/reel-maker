"""The media policy: whether downloaded bytes are admitted, downscaled or refused.

Magic-byte sniffing of images and videos, the Pillow decode and pixel caps, the downscale of an oversized
image, the TIFF tile guard and the `.refused` markers that remember an image too big to ever use. It decides
admit / downscale / refuse for bytes it is handed and never fetches anything (see `engine.render.safe_fetch`);
the adapters in `engine.render.asset_sourcer` call it after a download and before anything is cached.
Pillow is imported lazily inside the functions that use it.
"""

import hashlib
import io
import logging
import math
import warnings
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

_log = logging.getLogger(__name__)


# The byte cap says nothing about declared PIXELS: a 140 KB PNG can declare 12000x12000, and the compositor's
# `Image.open(...).convert("RGB")` then allocates ~430 MB. 50 Mpx is ~7000x7000, far past any photo used here.
_MAX_IMAGE_PIXELS = 50_000_000

# A non-JPEG image has no cheap partial decode: checking one for truncation, or shrinking it, means decoding
# it at full size. That is bounded in BYTES, not just pixels, because formats differ a lot (peak RSS measured
# on 79 Mpx inputs: PNG ~7 B/px, TIFF ~6, lossy / lossless / animated WebP ~12-17 at 25 Mpx, hence the 20): `_MAX_DECODE_BYTES` divided by a
# per-format bytes-per-pixel estimate is that format's pixel cap (`_pixel_cap`), and an image over it is
# refused or, for the formats that allow it, downscaled. `_MAX_FULL_DECODE_PIXELS` is the further ceiling on
# what is decoded to be downscaled. A JPEG is decoded at 1/2-1/8 scale (DCT scaling) instead and is bound
# only by `_MAX_IMAGE_PIXELS` and Pillow's own bomb limit.
_MAX_FULL_DECODE_PIXELS = 80_000_000
_MAX_DECODE_BYTES = 400_000_000

# Downscaling an over-cap image is a one-off decode per original (not on every cache hit), so it may use a
# larger budget than the check: with the defaults PNG / TIFF / GIF between 50 and 75 Mpx and WebP between 20
# and 30 Mpx are downscaled (peak RSS ~7 / ~6 / ~12-17 B/px: <= ~520 MB for a 75 Mpx PNG). Above that
# they are refused and remembered. Without a window larger than the cap the downscale could never run.
_MAX_DOWNSCALE_DECODE_BYTES = 600_000_000
_DECODE_BYTES_PER_PIXEL = {"WEBP": 20}
_DEFAULT_DECODE_BYTES_PER_PIXEL = 8
_JPEG_FORMATS = ("JPEG", "MPO")            # an MPO is a JPEG with extra frames: same DCT-scaled decode

# libtiff allocates a TILE-sized buffer (TileWidth x TileLength) whatever the declared image size: a 100x100
# TIFF declaring 25000x25000 tiles made `load()` allocate ~1 GB, so the pixel caps above do not bound it.
_MAX_TIFF_TILE_SIDE = 4096

# ── downloaded bytes must look like what they claim to be ─────────────────────────────────────
# The download guards bound where bytes come from, how many and how long; a 200 text/html or JSON body
# (a CDN error page, a captive portal, a compromised allowlisted host) or an empty one would otherwise
# be written as pexels_N.mp4 / wiki_N.jpg / hf_*.png and then reused forever by the `exists()` caches.
# The first bytes are checked against the formats the pipeline actually decodes (MoviePy/PIL/ffmpeg).

_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*")
_VIDEO_MAGIC = (b"\x1a\x45\xdf\xa3", b"GIF87a", b"GIF89a")             # WebM/EBML, GIF

# First ISO-BMFF box type, at offset 4. `ftyp` is what progressive MP4 (Pexels) starts with; the rest are
# first boxes of decodable QuickTime / fragmented / CMAF files. A false reject would silently drop a hit to
# the paid HuggingFace tier, while this is only a sanity check (it cannot prove the rest of the file).
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip",
              b"styp", b"moof", b"sidx", b"junk", b"pnot", b"uuid")


def sniff_ok(kind: str, head) -> bool:
    """True if `head` (the first <=16 bytes) is a JPEG/PNG/GIF/WebP/TIFF image or an MP4/WebM/GIF video.

    `kind` is "image" or "video"; anything else, or a non-bytes `head`, is not ok. SVG, BMP, HTML,
    JSON, PDF and an empty body are all refused.
    """
    if not isinstance(head, (bytes, bytearray)):
        return False
    head = bytes(head)
    if kind == "image":
        return head.startswith(_IMAGE_MAGIC) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    if kind == "video":
        return head[4:8] in _MP4_BOXES or head.startswith(_VIDEO_MAGIC)
    return False


def _bytes_per_pixel(fmt) -> int:
    return _DECODE_BYTES_PER_PIXEL.get(fmt, _DEFAULT_DECODE_BYTES_PER_PIXEL)


def _pixel_cap(fmt) -> int:
    """The most pixels an image of Pillow format `fmt` may declare: `_MAX_IMAGE_PIXELS`, and for a format
    that is decoded in full also what fits in `_MAX_DECODE_BYTES` (WebP: 20 Mpx; PNG / TIFF / GIF: 50 Mpx)."""
    if fmt in _JPEG_FORMATS:
        return _MAX_IMAGE_PIXELS                # decoded at 1/8 scale: far less than a full-size buffer
    return min(_MAX_IMAGE_PIXELS, _MAX_DECODE_BYTES // _bytes_per_pixel(fmt))


def _decode_window(fmt) -> int:
    """The most pixels of a non-JPEG image that may be decoded in full in order to downscale it:
    `min(_MAX_FULL_DECODE_PIXELS, _MAX_DOWNSCALE_DECODE_BYTES // bytes-per-pixel)`."""
    return min(_MAX_FULL_DECODE_PIXELS, _MAX_DOWNSCALE_DECODE_BYTES // _bytes_per_pixel(fmt))


def _tiff_tiles_ok(im) -> bool:
    """False for a TIFF declaring a tile (TileWidth / TileLength) larger than `_MAX_TIFF_TILE_SIDE`; True
    for every other image. Checked from the header, before any pixel is decoded."""
    if im.format != "TIFF":
        return True
    tags = getattr(im, "tag_v2", None)
    if tags is None:
        return True
    for tag in (322, 323):                      # TileWidth, TileLength
        value = tags.get(tag)
        values = value if isinstance(value, (tuple, list)) else (value,)
        if any(isinstance(v, int) and v > _MAX_TIFF_TILE_SIDE for v in values):
            return False
    return True


def _is_transient(exc: BaseException) -> bool:
    """True for a failure that says nothing about the image: MemoryError, or an OSError carrying an errno
    (EMFILE, EIO, ...) that is not a definite "not a file" (missing, a directory, a path through a file) or
    Pillow's own "cannot identify image"."""
    from PIL import UnidentifiedImageError
    if isinstance(exc, MemoryError):
        return True
    return (isinstance(exc, OSError)
            and not isinstance(exc, (FileNotFoundError, IsADirectoryError, NotADirectoryError, UnidentifiedImageError))
            and exc.errno is not None)


def image_ok(source) -> bool:
    """True if Pillow can decode `source` (image bytes or a Path) and it declares <= `_pixel_cap(format)`
    pixels (`_MAX_IMAGE_PIXELS`, and for a non-JPEG also what fits in `_MAX_DECODE_BYTES`: 20 Mpx for WebP).

    The declared size is read from the header first, and the pixels are decoded (`load()`) only when it
    is within the cap, so the decode is bounded (by the per-format byte budget, `_MAX_DECODE_BYTES`; a JPEG is decoded at 1/8
    scale via DCT scaling, which still reads the whole entropy-coded stream, so truncation is caught
    without the full-size buffer). An unidentifiable or truncated/corrupt file, a file Pillow itself calls
    a decompression bomb (> 2x its own limit raises at open) and a missing path are all "not ok" -- a
    refusal, not an exception. Pillow's own `DecompressionBombWarning` (a soft warning between 1x and 2x
    its limit) is silenced: the cap here is the one that decides. Only the first frame of an animated
    file is decoded. A TRANSIENT failure (see `_is_transient`: `MemoryError`, or
    an `OSError` carrying an errno other than a definite "not a file" or Pillow's own "cannot identify": EMFILE, EIO, ...) says nothing about the
    image and is re-raised, so a caller never deletes a good (possibly paid-for) cached file over it.
    `SoftTimeLimitExceeded` is re-raised.
    """
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source) as im:
                width, height = im.size
                if width * height > _pixel_cap(im.format) or not _tiff_tiles_ok(im):
                    return False
                im.draft("RGB", (max(1, width // 8), max(1, height // 8)))   # JPEG only; a no-op otherwise
                im.load()                                                    # raises on a truncated file
        return True
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        if _is_transient(exc):
            raise
        return False


def _downscale_jpeg(content: bytes) -> "bytes | None":
    """A JPEG over `_MAX_IMAGE_PIXELS`, re-encoded smaller; None for anything else or on any failure.

    Wikimedia originals are often JPEG photos over the cap (a 75 Mpx featured picture) and the only
    alternative, the summary thumbnail, is ~320 px wide: far too small for a 1080x1920 frame. A JPEG
    can be decoded at 1/2, 1/4 or 1/8 scale (DCT scaling) without ever allocating the full-size
    buffer, so such a picture is decoded at the smallest reduction that fits, its EXIF rotation is baked
    in (the re-encode drops the tag) and it is saved as a JPEG. Only up to Pillow's own bomb limit
    (2x `Image.MAX_IMAGE_PIXELS`, ~179 Mpx) -- beyond it `Image.open` raises and this returns None, as it
    does for non-JPEG formats (those are handled by `_downscale_decoded`).
    """
    from PIL import Image, ImageOps
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(content)) as im:
                if im.format not in _JPEG_FORMATS:
                    return None
                width, height = im.size
                if width * height <= _MAX_IMAGE_PIXELS:
                    return None
                scale = 2
                while scale < 8 and (width // scale) * (height // scale) > _MAX_IMAGE_PIXELS:
                    scale *= 2
                im.draft("RGB", (max(1, width // scale), max(1, height // scale)))
                im.load()
                out = ImageOps.exif_transpose(im).convert("RGB")
        if out.width * out.height > _MAX_IMAGE_PIXELS:
            out.thumbnail((out.width, out.height))          # unreachable in practice; keep the cap a guarantee
            factor = (_MAX_IMAGE_PIXELS / (out.width * out.height)) ** 0.5
            out = out.resize((max(1, int(out.width * factor)), max(1, int(out.height * factor))))
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        if _is_transient(exc):
            raise                       # a MemoryError / EMFILE says nothing about the image: never remembered
        return None


class OversizedImage(ValueError):
    """A readable image over its pixel cap that is beyond its decode window (or Pillow's bomb limit)."""


def _beyond_downscale_window(content: bytes) -> bool:
    """True if `content` is a readable image that is over the cap AND cannot be downscaled by design.

    That is: Pillow's own bomb error (> 2x its limit), or a non-JPEG over `_decode_window(fmt)`.
    Those are the only images worth REMEMBERING as too big. An image that is over the cap but inside the
    window (a JPEG, or a non-JPEG up to its `_decode_window`) that still failed to downscale failed to DECODE
    (truncated, corrupt, a transient MemoryError): that may not repeat, so it is never recorded.
    False for anything else, including a header Pillow cannot read."""
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(content)) as im:
                pixels = im.size[0] * im.size[1]
                return (pixels > _pixel_cap(im.format) and im.format not in _JPEG_FORMATS
                        and pixels > _decode_window(im.format))
    except SoftTimeLimitExceeded:
        raise
    except Image.DecompressionBombError:
        return True
    except Exception:
        return False


_ORIENTATION_TRANSPOSE = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE",
                          6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def _downscale_decoded(content: bytes) -> "bytes | None":
    """A non-JPEG image over `_MAX_IMAGE_PIXELS`, decoded in full and re-encoded smaller; None otherwise.

    PNG / WebP / TIFF / GIF have no partial decode, so the whole image is decoded (only if it is within
    `_decode_window(fmt)` = min(`_MAX_FULL_DECODE_PIXELS`, `_MAX_DOWNSCALE_DECODE_BYTES` // bytes-per-pixel), which
    bounds the transient memory), shrunk with an integer box `reduce()`
    (cheap, no resampling allocation of the big image), has its EXIF orientation baked in (the reduce drops
    the metadata) and is re-encoded: PNG stays PNG (lossless, alpha kept), everything else becomes a
    JPEG. Only the first frame of an animated file is used -- the compositor also opens just that
    (`Image.open(...).convert("RGB")`). None for a JPEG (see `_downscale_jpeg`), for an image outside the
    size window, or on any failure.
    """
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(content)) as im:
                fmt = im.format
                width, height = im.size
                pixels = width * height
                if fmt in _JPEG_FORMATS or pixels <= _pixel_cap(fmt) or pixels > _decode_window(fmt):
                    return None
                if not _tiff_tiles_ok(im):
                    return None
                target = _MAX_IMAGE_PIXELS     # always the binding cap here: window > cap implies bytes // bpp > _MAX_IMAGE_PIXELS
                orientation = im.getexif().get(0x0112)
                im.load()
                factor = max(2, math.ceil(math.sqrt(pixels / target)))
                try:
                    small = im.reduce(factor)
                except Exception:                       # modes reduce() does not support (e.g. palette)
                    small = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB").reduce(factor)
        name = _ORIENTATION_TRANSPOSE.get(orientation)
        if name:
            small = small.transpose(getattr(Image.Transpose, name))
        if small.width * small.height > target:
            shrink = (target / (small.width * small.height)) ** 0.5
            small = small.resize((max(1, int(small.width * shrink)), max(1, int(small.height * shrink))))
        buf = io.BytesIO()
        if fmt == "PNG":
            small.convert("RGBA" if "A" in small.getbands() else "RGB").save(buf, format="PNG")
        else:
            small.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        if _is_transient(exc):
            raise                       # a MemoryError / EMFILE says nothing about the image: never remembered
        return None


def fit_image(content: bytes) -> bytes:
    """`content` if `image_ok`; else a downscaled copy of an oversized image; else ValueError (after a WARNING).

    The ValueError is `OversizedImage` when the image is readable but too big and cannot be downscaled
    (a non-JPEG past its decode window, or any image past Pillow's bomb limit), so the caller can
    remember it. A decode failure inside the window is a plain ValueError and never remembered."""
    if image_ok(content):
        return content
    smaller = _downscale_jpeg(content) or _downscale_decoded(content)
    if smaller is not None and image_ok(smaller):
        _log.info("Downscaled an oversized image (%d -> %d bytes)", len(content), len(smaller))
        return smaller
    _log.warning("Discarding a download that is not a readable image of a reasonable size")
    if _beyond_downscale_window(content):
        raise OversizedImage("downloaded bytes are not a readable image: over the pixel cap and cannot be downscaled")
    raise ValueError("downloaded bytes are not a readable image")


def refusal_marker(path: Path, url: str) -> Path:
    """Where a refused-as-oversized image is remembered: next to the file it would have been, keyed by URL.

    The URL is part of the name because an original and its thumbnail often share one target path
    (same extension): remembering the original must not block the thumbnail."""
    digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()[:12]   # a lone surrogate must not raise
    return path.with_name(f"{path.name}.{digest}.refused")


def refusal_stamp() -> str:
    """What a marker holds: the limits (and the per-format bytes-per-pixel estimates) it was refused under.
    Any change to them forgets the marker."""
    table = ",".join(f"{k}={v}" for k, v in sorted(_DECODE_BYTES_PER_PIXEL.items()))
    return (f"{_MAX_IMAGE_PIXELS}:{_MAX_FULL_DECODE_PIXELS}:{_MAX_DECODE_BYTES}:{_MAX_DOWNSCALE_DECODE_BYTES}"
            f":{_DEFAULT_DECODE_BYTES_PER_PIXEL}:{table}")


def refusal_recorded(path: Path, url: str) -> bool:
    """True if `url` (which would be cached at `path`) was already refused as oversized under the CURRENT limits.

    Without this a non-JPEG original over the cap is downloaded in full (up to 50 MB), refused and
    forgotten on every `search()` -- the thumbnail that replaced it is cached under a different
    extension, so the `exists()` check never fires for the original's own path. The marker holds the limits
    it was recorded under (`refusal_stamp`); changing any of them forgets it."""
    try:
        return refusal_marker(path, url).read_text() == refusal_stamp()
    except (OSError, ValueError):       # unreadable / not UTF-8: treat as no marker
        return False


def require_media(kind: str, head) -> None:
    """Raise ValueError (after a WARNING) if `head` is not a `kind` per `sniff_ok`."""
    if not sniff_ok(kind, head):
        _log.warning("Discarding a download that is not a valid %s", kind)
        raise ValueError(f"downloaded bytes are not a valid {kind}")


def cached_media_ok(path: Path, kind: str) -> bool:
    """True if the cached file at `path` starts like a real `kind`; otherwise delete it and say so.

    A file cached before the content check existed (or by a process that died mid-write on a
    pre-`os.replace` version) would otherwise be served by the `exists()` caches forever and fail
    every later render in the compositor. The caller falls through to a fresh download.

    For an image the file is also decoded (`image_ok`). A TRANSIENT error (reading the head or inside
    Pillow: EMFILE, EIO, MemoryError, ...) says nothing about the file, so it is KEPT and True is returned
    -- deleting a paid HuggingFace asset over it would mean a re-billed regeneration.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        head = b""                   # not a readable file at all: a definite verdict
    except OSError:
        # EMFILE / EIO / ...: says nothing about the file, so never delete it over this (for a paid
        # HuggingFace asset that would mean a re-billed regeneration)
        _log.warning("Could not read a cached %s (transient error); keeping it", kind, exc_info=True)
        return True
    ok = sniff_ok(kind, head)
    if ok and kind == "image":
        try:
            ok = image_ok(path)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            _log.warning("Could not check a cached image (transient error); keeping it", exc_info=True)
            return True
    if ok:
        return True
    _log.warning("Discarding a cached file that is not a valid %s", kind)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return False
