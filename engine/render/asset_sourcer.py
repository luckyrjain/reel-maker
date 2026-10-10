import hashlib
import io
import ipaddress
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import httpx
from celery.exceptions import SoftTimeLimitExceeded

from api import models
from api.config import settings
from engine.names import person_names
from engine.observability import record_stage
from engine.render.pricing import hf_image_cost_usd, hf_video_cost_usd

_log = logging.getLogger(__name__)


_PERMISSIVE_LICENSES = {
    "cc0", "cc-0", "public domain", "cc by", "cc-by", "cc by 2.0", "cc by 4.0",
    "pexels", "pexels_free",
}


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


_STALE_TMP_AGE_S = 3600     # a *.tmp older than this is a crashed process's leftover, not a write in progress


def _tmp_path_for(path: Path) -> Path:
    """A unique temp file beside `path` (same directory, so `os.replace` is atomic): `<name>.<8 hex>.tmp`.

    It used to be one fixed name per target (`pexels_1.tmp`, `wiki_5.jpg.tmp`), so two workers
    fetching the same asset wrote into the same file and the loser's failure could leave holes.
    """
    return path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")


def _sweep_stale_tmp(store_dir: Path) -> None:
    """Delete `*.tmp` files in `store_dir` older than `_STALE_TMP_AGE_S` (best effort, never raises).

    Unique temp names mean a crashed download's leftover is never overwritten, so it is swept when a
    source is created. Younger files are left alone: they may be another worker's write in progress.
    """
    cutoff = time.time() - _STALE_TMP_AGE_S
    try:
        for f in store_dir.glob("*.tmp"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes via a temp file + os.replace.

    Every sourcer caches by `if path.exists()`, so a process killed mid-write
    would otherwise leave a truncated file that is reused on every later render. The temp name
    is unique per call (see `_tmp_path_for`), so two workers writing the same asset never share it.
    """
    tmp = _tmp_path_for(path)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── guarded downloads ────────────────────────────────────────────────────────
# Download URLs (Pexels `link`, Wikipedia `originalimage`/`thumbnail` `source`) come straight from
# remote JSON, so a tampered or compromised upstream response could otherwise point the worker at
# any URL (blind SSRF) or hand it an unbounded body. Every download is https-only to an expected
# host, re-checked on each redirect hop (httpx is never asked to follow redirects itself), and
# size-capped.

_MAX_VIDEO_BYTES = 250 * 1024 * 1024   # FHD portrait Pexels clips are tens of MB
_MAX_IMAGE_BYTES = 50 * 1024 * 1024    # Wikipedia originals are rarely over ~20 MB
# The byte cap says nothing about declared PIXELS: a 140 KB PNG can declare 12000x12000, and the compositor's
# `Image.open(...).convert("RGB")` then allocates ~430 MB. 50 Mpx is ~7000x7000, far past any photo used here.
_MAX_IMAGE_PIXELS = 50_000_000
_MAX_API_JSON_BYTES = 8 * 1024 * 1024   # Pexels search / Wikipedia lookups are a few KB; refuse to parse more
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# httpx timeouts are per read, so a server that trickles never trips them: each download also gets a
# wall-clock budget, shared by its redirect hops / 429 retry. It is enforced in three places: before
# every hop (`_open_download`), after every network read (`_iter_capped`), and by a `_Watchdog` that
# shuts the socket down when the budget runs out -- the only thing that can interrupt a read blocked
# on a server dripping response headers or a chunked-encoding size line, where httpx yields nothing for
# the other two to check. Each hop's httpx timeout is also clamped to the time left (see
# `_HOP_TIMEOUT_GRACE_S`), which bounds the TCP connect / TLS handshake before the watchdog exists, and
# each search() has one overall budget (below), and the host's DNS lookup is given at most that long
# (`_dns_in_time`; getaddrinfo takes no timeout). The API calls are streamed and capped while they are
# read (`_api_call`), and a render's total is bounded by `asset_budget()` (below). SoftTimeLimitExceeded
# raised in ANY network call of this module is re-raised (it ends the task), never swallowed as a failed
# download.
_VIDEO_DEADLINE_S = 600.0
_IMAGE_DEADLINE_S = 120.0
# One budget per search() call, so the per-download budgets of its hits/candidates cannot add up (a
# Pexels search tries up to 15 hits, a Wikipedia search 2 candidates). A download's own budget is
# clipped to what is left of it, and no further download is started once it is spent.
_PEXELS_SEARCH_BUDGET_S = 900.0
_WIKI_SEARCH_BUDGET_S = 240.0
_monotonic = time.monotonic     # indirection so tests can drive a fake clock

# One budget for a whole render's asset sourcing. The per-search budgets above cannot add up across a
# render's beats only if something bounds the sum: `asset_budget()` sets a deadline that every search()
# inside it is clipped to, after which no download starts and the paid HuggingFace tiers are skipped (a
# cached file is still served). `render_cut` wraps its beat loop in it. Unset (the default) changes nothing.
RENDER_ASSET_BUDGET_S = 1200.0
_asset_deadline_at: ContextVar["float | None"] = ContextVar("asset_deadline_at", default=None)


@contextmanager
def asset_budget(seconds: float):
    """Bound the asset sourcing done inside this block to `seconds` of wall clock (nesting only tightens)."""
    deadline = _monotonic() + seconds
    outer = _asset_deadline_at.get()
    token = _asset_deadline_at.set(deadline if outer is None else min(deadline, outer))
    try:
        yield
    finally:
        _asset_deadline_at.reset(token)


def _clip_to_asset_budget(deadline_at: float) -> float:
    """`deadline_at`, or the active asset budget's deadline if that is sooner (no clock reading either way)."""
    budget = _asset_deadline_at.get()
    return deadline_at if budget is None else min(deadline_at, budget)


def _asset_budget_spent() -> bool:
    budget = _asset_deadline_at.get()
    return budget is not None and _monotonic() >= budget
# Per hop, httpx's own timeout is clamped to the budget that is left plus this grace: it bounds the TCP
# connect and TLS handshake (before the watchdog exists) and one read that would outlive the budget,
# while the watchdog -- exact, but only armed once connected -- still wins when the connection is up.
# DNS is gated separately (`_dns_in_time`), with this same hop timeout.
_HOP_TIMEOUT_GRACE_S = 1.0


_HOST_RE = re.compile(r"[a-z0-9.-]+")


def _https_host(url) -> "str | None":
    """Lowercased host of an https URL on the default port with no userinfo, else None.

    Deliberately stricter than urlsplit: it silently strips tab/CR/LF and accepts hosts that httpx
    (the parser that actually connects) rejects or reads differently, so a URL with any whitespace,
    control character or backslash is refused outright and the host must be plain `[a-z0-9.-]`.
    """
    if not isinstance(url, str) or any(c.isspace() or c == "\\" or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return None
    if port not in (None, 443):
        return None
    if not parts.netloc.isascii():      # hostname.lower() would map e.g. U+212A (Kelvin) to "k"
        return None
    host = (parts.hostname or "").lower()
    return host if _HOST_RE.fullmatch(host) else None


def _pexels_url_ok(url) -> bool:
    """`*.pexels.com` (videos.pexels.com serves the files), not the apex or a look-alike."""
    host = _https_host(url)
    return host is not None and host.endswith(".pexels.com") and host != ".pexels.com" \
        and ".." not in host


def _wikimedia_url_ok(url) -> bool:
    """upload.wikimedia.org: the image CDN both `originalimage` and `thumbnail` point at."""
    return _https_host(url) == "upload.wikimedia.org"


def _host_for_log(url) -> str:
    """Host of `url` for a log line: a short plain `[a-z0-9.-]` host, else "?".

    Never the path, query or userinfo (tokens), and never raw control characters or an unbounded
    string from a hostile upstream.
    """
    if not isinstance(url, str):
        return "?"
    try:
        host = urllib.parse.urlsplit(url).hostname or ""       # already lower-cased by urlsplit
    except ValueError:
        return "?"
    return host if len(host) <= 253 and _HOST_RE.fullmatch(host) else "?"


@contextmanager
def _http_stream(method, url, *, extensions=None, **kwargs):
    """`httpx.stream()` plus the `extensions` parameter it lacks (needed for the connect-time trace).

    A module-level seam so tests can substitute the transport; same call shape as `httpx.stream`.
    """
    with httpx.Client() as client:
        with client.stream(method, url, extensions=extensions, **kwargs) as response:
            yield response


class _Watchdog:
    """Shuts a download's connection down once its wall-clock budget is spent.

    httpx timeouts are per read, and the deadline checks in `_open_download`/`_iter_capped` only run
    when httpx hands back a response or a body chunk; a server dripping response headers, a chunked
    size line / extension, or a TLS handshake produces none of those, so nothing else can interrupt
    that blocked read. `trace` (the request's `trace` extension) arms it on httpcore's
    `connection.connect_tcp.complete` event: the raw socket exists before any response does, and
    a `dup()` of it is kept, not the socket itself -- for HTTPS (every allowlisted host) httpcore
    next wraps the socket in TLS, which *detaches* the original object (fileno -1), so shutting that
    down would silently do nothing. `shutdown()` acts on the connection, not the descriptor, so the
    dup still breaks a blocked read in any phase. `cancel()` it when the hop ends. When it fires,
    `fired` is True: the caller must treat the download as failed even if the read ended *cleanly*
    (a close-delimited body reads a shutdown as a valid EOF).
    """

    def __init__(self, deadline_at: "float | None"):
        self._deadline_at = deadline_at
        self._lock = threading.Lock()
        self._timer: "threading.Timer | None" = None
        self._sock: "socket.socket | None" = None
        self.fired = False

    def trace(self, event_name: str, info: dict) -> None:
        if self._deadline_at is None or self._timer is not None:
            return
        if event_name != "connection.connect_tcp.complete":
            return
        try:
            raw = info["return_value"].get_extra_info("socket")
            if not isinstance(raw, socket.socket):
                return
            dup = raw.dup()
        except SoftTimeLimitExceeded:
            raise                                   # a few microseconds wide, but the task is out of time
        except Exception:                                   # noqa: BLE001 - never break the request
            return
        timer = threading.Timer(max(self._deadline_at - _monotonic(), 0.0), self._fire)
        timer.daemon = True
        with self._lock:
            self._sock, self._timer = dup, timer
        timer.start()

    def _fire(self) -> None:
        with self._lock:                                    # cancel() closes the dup under this lock
            if self._sock is None:
                return
            self.fired = True                               # before the shutdown: the read thread may
            try:                                            # see the error the instant it returns
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                self.fired = False                          # nothing was closed; do not claim it was

    def cancel(self) -> None:
        with self._lock:
            timer, sock, self._sock = self._timer, self._sock, None
        if timer is not None:
            timer.cancel()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _dns_in_time(host: str, port: int, timeout: float) -> bool:
    """False only if resolving `host` is still pending after `timeout` seconds.

    `socket.getaddrinfo` takes no timeout, so neither httpx's connect/read timeouts nor the watchdog
    (which needs a connection) bound a stalled resolver. The lookup runs on a daemon thread (a hung
    getaddrinfo cannot be cancelled, so it is simply abandoned and must not block interpreter exit)
    and is waited on for at most `timeout`. A resolution that FAILS counts as in time: reporting it is
    httpx's job. Skipped (True) for IP literals and when an environment proxy is configured, because
    the proxy then resolves the name. This is a gate, not a guarantee: httpx resolves again when it
    connects (normally an OS-cache hit); a resolver that answers once and then stalls is not covered.
    """
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    env = urllib.request.getproxies_environment()
    if any(env.get(k) for k in ("all", "https", "http")):
        return True
    done = threading.Event()

    def resolve():
        try:
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception:                                   # noqa: BLE001 - httpx reports a failed lookup
            pass
        finally:
            done.set()

    threading.Thread(target=resolve, daemon=True, name="sourcer-dns").start()
    return done.wait(timeout)


@contextmanager
def _open_download(url: str, url_ok, *, timeout: float, headers: dict | None = None,
                   deadline_at: "float | None" = None):
    """Stream a GET of `url`, following redirects by hand so every hop is checked by `url_ok`.

    Raises ValueError for a disallowed URL (first or redirected), a redirect with no Location, or
    more than `_MAX_REDIRECTS` hops, if the wall-clock `deadline_at` has passed before a hop, or if a
    `_Watchdog` had to close the connection at the deadline. Yields the final (non-redirect) response;
    the caller still owns `raise_for_status()` and the status handling. With a `deadline_at`, each hop's
    httpx timeout is clamped to the time left (+ `_HOP_TIMEOUT_GRACE_S`) and its host's DNS lookup is
    given at most that long (`_dns_in_time`). `Accept-Encoding: identity` is always sent
    (media is not worth compressing, and a gzip body would inflate far past its Content-Length
    before the size cap could see it); `_iter_capped` rejects a response that is encoded anyway.
    """
    kwargs = {
        "follow_redirects": False,
        "timeout": timeout,
        "headers": {**(headers or {}), "Accept-Encoding": "identity"},
    }
    for _ in range(_MAX_REDIRECTS + 1):
        hop_kwargs = kwargs
        if deadline_at is not None:
            now = _monotonic()                              # one reading: the check and the clamp
            if now > deadline_at:
                raise ValueError("download too slow: deadline exceeded before the request")
            hop_kwargs = {**kwargs, "timeout": min(timeout, deadline_at - now + _HOP_TIMEOUT_GRACE_S)}
        if not url_ok(url):
            _log.warning("Refusing download from a disallowed URL (host %s)", _host_for_log(url))
            raise ValueError(f"download URL not allowed: {url!r}")
        if deadline_at is not None and not _dns_in_time(
                urllib.parse.urlsplit(url).hostname or "", 443, hop_kwargs["timeout"]):
            _log.warning("DNS resolution for %s did not finish in time; giving up on the download",
                         _host_for_log(url))
            raise ValueError("download too slow: DNS resolution did not finish in time")
        guard = _Watchdog(deadline_at)
        try:
            with _http_stream("GET", url, extensions={"trace": guard.trace}, **hop_kwargs) as r:
                if r.status_code not in _REDIRECT_STATUSES:
                    yield r
                    if guard.fired:
                        # the body ended "cleanly" only because we shut the connection down (a
                        # close-delimited body reads that as EOF): it is truncated, never a success
                        raise ValueError("download too slow: deadline exceeded (connection closed)")
                    return
                location = r.headers.get("location")
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            if guard.fired:
                _log.warning("Download from %s exceeded its deadline; connection closed", _host_for_log(url))
                raise ValueError("download too slow: deadline exceeded (connection closed)") from exc
            raise
        finally:
            guard.cancel()
        if not location or not isinstance(location, str):
            raise ValueError(f"redirect without a Location from {url!r}")
        url = urllib.parse.urljoin(url, location)
    raise ValueError("too many redirects")


def _api_call(method: str, url: str, limit: int, *, headers=None, timeout: float, params=None, json=None):
    """One API request, streamed through `_http_stream` and read at most `limit` bytes.

    `httpx.get` / `httpx.post` read the WHOLE body before returning, so a hostile or broken upstream
    could make the worker buffer gigabytes before any size check ran. Here `Accept-Encoding: identity`
    is sent (an encoded body is refused: its decoded size is not what the network read says), no
    redirect is followed (same as the httpx functions), reading stops with `_TooLarge` as soon as
    `limit` is crossed, and what was read is returned as a real `httpx.Response` (`.json()`,
    `.content`, `.headers`, `.raise_for_status()` work as call sites expect).
    """
    with _http_stream(method, url, headers={**(headers or {}), "Accept-Encoding": "identity"},
                      timeout=timeout, follow_redirects=False, params=params, json=json) as r:
        body = b"".join(_iter_capped(r, limit))
        return httpx.Response(r.status_code, headers=r.headers, content=body,
                              request=httpx.Request(method, url))


def _api_get(url: str, *, limit: int | None = None, **kw):
    """`httpx.get(url, **kw)` (params/headers/timeout), streamed and capped at `limit` (default 8 MB)."""
    return _api_call("GET", url, _MAX_API_JSON_BYTES if limit is None else limit, **kw)


def _api_post(url: str, *, limit: int | None = None, **kw):
    """`httpx.post(url, **kw)` (headers/json/timeout), streamed and capped at `limit` (default 8 MB)."""
    return _api_call("POST", url, _MAX_API_JSON_BYTES if limit is None else limit, **kw)


def _body_too_large(resp, limit: int) -> bool:
    """True if `resp` declares (Content-Length) or actually holds (decoded) more than `limit` bytes.

    Defense in depth behind `_api_call`, which already stops reading at the limit: this keeps an
    oversized body from being handed to `.json()` (parse time/memory amplification) or written to the
    asset cache (disk) if a response reaches the caller some other way. A header that understates
    (gzip) is caught by the decoded length; a missing or unparsable header falls back to it.
    """
    try:
        declared = int(resp.headers.get("content-length"))
    except (TypeError, ValueError, AttributeError):
        declared = None
    if declared is not None and declared > limit:
        return True
    content = getattr(resp, "content", None)
    return isinstance(content, (bytes, bytearray)) and len(content) > limit


class _TooLarge(ValueError):
    """A response body exceeded its size limit (declared Content-Length, or bytes actually read)."""


def _iter_capped(r, limit: int, deadline_at: "float | None" = None):
    """Yield `r`'s body in chunks; ValueError once it exceeds `limit` bytes or passes `deadline_at`.

    Also refuses a response with a non-identity Content-Encoding, whose decoded size is not what
    Content-Length (or the network read) says. `iter_bytes()` is called without a chunk size on
    purpose: with one, httpx buffers that many bytes before yielding anything, so a slow body would
    never reach the deadline check; without it, every network read does.
    """
    encoding = r.headers.get("content-encoding")
    if isinstance(encoding, str) and encoding.strip().lower() not in ("", "identity"):
        raise ValueError(f"download is Content-Encoding {encoding!r}, expected identity")
    try:
        declared = int(r.headers.get("content-length"))
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > limit:
        raise _TooLarge(f"download too large: Content-Length {declared} > {limit}")
    total = 0
    for chunk in r.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise _TooLarge(f"download too large: more than {limit} bytes")
        if deadline_at is not None and _monotonic() > deadline_at:
            raise ValueError("download too slow: deadline exceeded")
        yield chunk


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


def _sniff_ok(kind: str, head) -> bool:
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


def _image_ok(source) -> bool:
    """True if Pillow can identify `source` (image bytes or a Path) and it declares <= _MAX_IMAGE_PIXELS.

    Reads the HEADER only (`Image.open` is lazy; pixels are never decoded). An unidentifiable or
    truncated file, a file Pillow itself calls a decompression bomb (> 2x its own limit raises at open)
    and an unreadable path are all "not ok" -- a refusal, never an exception. Pillow's own
    `DecompressionBombWarning` (a soft warning between 1x and 2x its limit) is silenced: the cap here is
    the one that decides. `SoftTimeLimitExceeded` is re-raised.
    """
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source) as im:
                width, height = im.size
        return width * height <= _MAX_IMAGE_PIXELS
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return False


def _require_image(content: bytes) -> None:
    """Raise ValueError (after a WARNING) if `content` is not `_image_ok`."""
    if not _image_ok(content):
        _log.warning("Discarding a download that is not a readable image of a reasonable size")
        raise ValueError("downloaded bytes are not a readable image")


def _require_media(kind: str, head) -> None:
    """Raise ValueError (after a WARNING) if `head` is not a `kind` per `_sniff_ok`."""
    if not _sniff_ok(kind, head):
        _log.warning("Discarding a download that is not a valid %s", kind)
        raise ValueError(f"downloaded bytes are not a valid {kind}")


def _cached_media_ok(path: Path, kind: str) -> bool:
    """True if the cached file at `path` starts like a real `kind`; otherwise delete it and say so.

    A file cached before the content check existed (or by a process that died mid-write on a
    pre-`os.replace` version) would otherwise be served by the `exists()` caches forever and fail
    every later render in the compositor. The caller falls through to a fresh download.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        head = b""
    if _sniff_ok(kind, head) and (kind != "image" or _image_ok(path)):
        return True
    _log.warning("Discarding a cached file that is not a valid %s", kind)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


# ── ids that become local file names ─────────────────────────────────────────
# `pexels_{id}.mp4` / `wiki_{id}.{ext}` are built from remote JSON, so an id is validated against a
# strict, bounded pattern before it is interpolated (a `/`, `..` or a very long title must never
# reach the filesystem, not even just to be rejected by a failing `Path.exists()`/write).

_NUMERIC_ID_RE = re.compile(r"[0-9]{1,20}")
_SAFE_ID_RE = re.compile(r"[a-z0-9_-]{1,64}")      # lowercase only: see _title_id


def _numeric_id(raw) -> "str | None":
    """`str(raw)` for a non-negative int or an ASCII digit string whose decimal form is 1-20 digits, else None."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        raw = str(raw) if raw >= 0 else ""
    if isinstance(raw, str) and _NUMERIC_ID_RE.fullmatch(raw):
        return raw
    return None


def _title_id(title: str) -> str:
    """File-name-safe id for a page title: itself if already `[a-z0-9_-]{1,64}`, else a digest.

    Lowercase only, on purpose: on a case-insensitive filesystem (macOS / Windows default) `wiki_Ab.jpg`
    and `wiki_aB.jpg` are ONE file, so one page's cached image would be served for another. Any title
    with a capital goes through the digest (computed on the exact title, so `Ab` and `aB` still differ,
    and the digest is itself lowercase hex). Only reachable when `pageid` is missing.
    """
    if _SAFE_ID_RE.fullmatch(title):
        return title
    return "t" + hashlib.sha256(title.encode()).hexdigest()[:16]


_FHD = 1920   # Pexels returns 4K by default; cap downloads at <= FHD height
_IMAGE_EXTS = ("jpg", "jpeg", "png", "webp")


def _choose_video_file(files: list[dict], max_height: int = _FHD) -> dict:
    """Pick the Pexels `video_files` entry to download. `files` must be non-empty.

    Order of preference: the tallest portrait file within `max_height`; else the smallest
    portrait file (every one is over the cap); else the tallest file within the cap (no
    portrait at all); else the first file. A square file counts as portrait.
    """
    portrait = [f for f in files if f.get("width", 1) <= f.get("height", 1)]
    fhd_portrait = [f for f in portrait if f.get("height", 0) <= max_height]
    if fhd_portrait:
        return max(fhd_portrait, key=lambda f: f.get("height", 0))
    if portrait:
        return min(portrait, key=lambda f: f.get("height", 0))
    fhd_any = [f for f in files if f.get("height", 0) <= max_height]
    return max(fhd_any, key=lambda f: f.get("height", 0)) if fhd_any else files[0]


def _license_from_extmetadata(meta: dict) -> dict:
    """Map a Wikimedia `extmetadata` block to the license fields stored on an Asset.

    `safe_to_publish` is True only for an exact (case-insensitive) match against
    `_PERMISSIVE_LICENSES` — deliberately conservative: share-alike and unlisted CC
    versions are NOT safe, and the publish gate enforces whatever this returns.
    """
    short = meta.get("LicenseShortName", {}).get("value", "unknown")
    return {
        "license": short,
        "license_url": meta.get("LicenseUrl", {}).get("value"),
        "attribution": _strip_html(meta.get("Artist", {}).get("value", "")),
        "safe_to_publish": short.lower() in _PERMISSIVE_LICENSES,
    }


def _image_extension(url: str) -> str:
    """File extension for a downloaded Wikipedia image; anything unrecognized is jpg."""
    ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
    return ext if ext in _IMAGE_EXTS else "jpg"


class WikipediaImageSource:
    _SEARCH = "https://en.wikipedia.org/w/api.php"
    _SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary"
    _HEADERS = {"User-Agent": "reel-maker/1.0"}

    def __init__(self, store_dir: Path):
        self.store_dir = store_dir
        store_dir.mkdir(parents=True, exist_ok=True)
        _sweep_stale_tmp(store_dir)

    def _fetch_license(self, page_title: str) -> dict:
        """Return license metadata for a Wikimedia file via the imageinfo API."""
        try:
            resp = _api_get(
                self._SEARCH,
                params={
                    "action": "query",
                    "titles": f"File:{page_title}",
                    "prop": "imageinfo",
                    "iiprop": "extmetadata",
                    "format": "json",
                },
                headers=self._HEADERS,
                timeout=10.0,
            )
            resp.raise_for_status()
            if _body_too_large(resp, _MAX_API_JSON_BYTES):
                raise ValueError("license lookup response too large")
            pages = resp.json().get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            meta = (page.get("imageinfo") or [{}])[0].get("extmetadata", {})
            return _license_from_extmetadata(meta)
        except SoftTimeLimitExceeded:
            raise                                   # out of time: not "unknown license"
        except Exception:
            return {"license": "unknown", "license_url": None, "attribution": None, "safe_to_publish": False}

    def _download(self, url: str, search_deadline: float) -> "bytes | None":
        """Image bytes from a checked, size-capped GET; None if rate-limited twice or out of budget.

        A 429 is retried once after a 2 s pause. Any other failure raises (the caller moves on
        to the next candidate URL). `search_deadline` is the whole search's budget: this download
        gets `_IMAGE_DEADLINE_S` or what is left of it, whichever is less.
        """
        now = _monotonic()
        if now >= search_deadline:
            return None
        deadline_at = min(now + _IMAGE_DEADLINE_S, search_deadline)
        for attempt in (0, 1):
            with _open_download(url, _wikimedia_url_ok, timeout=30.0, headers=self._HEADERS,
                                deadline_at=deadline_at) as r:
                if r.status_code != 429:
                    r.raise_for_status()
                    return b"".join(_iter_capped(r, _MAX_IMAGE_BYTES, deadline_at))
            if attempt == 0:
                time.sleep(2.0)
        return None

    def search(self, person_name: str) -> "SourcedAsset | None":
        try:
            resp = _api_get(
                self._SEARCH,
                params={"action": "opensearch", "search": person_name, "limit": 1, "format": "json"},
                headers=self._HEADERS,
                timeout=10.0,
            )
            resp.raise_for_status()
            if _body_too_large(resp, _MAX_API_JSON_BYTES):
                raise ValueError("opensearch response too large")
            results = resp.json()
            if not results[1]:
                return None
            page_title = results[1][0]
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return None

        try:
            # safe="": the title is ONE path segment, so a "/" in it ("AC/DC") must become %2F
            safe = urllib.parse.quote(page_title.replace(" ", "_"), safe="")
            resp = _api_get(f"{self._SUMMARY}/{safe}", headers=self._HEADERS, timeout=10.0)
            resp.raise_for_status()
            if _body_too_large(resp, _MAX_API_JSON_BYTES):
                raise ValueError("summary response too large")
            data = resp.json()
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        def image_source(key: str) -> "str | None":
            block = data.get(key)
            src = block.get("source") if isinstance(block, dict) else None
            return src if isinstance(src, str) and src else None

        original = image_source("originalimage")
        thumbnail = image_source("thumbnail")
        candidate_urls = [u for u in (original, thumbnail) if u]
        if not candidate_urls:
            return None

        page_id = _numeric_id(data.get("pageid")) or _title_id(page_title.replace(" ", "_"))

        # Fetch license metadata for rights tracking (needed at publish time)
        image_filename = original.rsplit("/", 1)[-1].rsplit("?", 1)[0] if original else ""
        # Decode: this is still URL-percent-encoded (from the raw image URL), but MediaWiki's
        # titles= param expects the real title (spaces/accents/parens literal, not %XX) -- an
        # accented or spaced filename otherwise matches no page and silently returns "unknown"/
        # safe_to_publish=False. unquote(), not unquote_plus(): this came from a URL path segment,
        # not a query string, so a literal "+" in a filename must not become a space.
        image_filename = urllib.parse.unquote(image_filename)
        license_info = self._fetch_license(image_filename) if image_filename else {
            "license": "unknown", "license_url": None, "attribution": None, "safe_to_publish": False
        }

        local_path = None
        search_deadline = _clip_to_asset_budget(_monotonic() + _WIKI_SEARCH_BUDGET_S)
        for img_url in candidate_urls:
            lp = self.store_dir / f"wiki_{page_id}.{_image_extension(img_url)}"
            if lp.exists() and _cached_media_ok(lp, "image"):
                local_path = lp
                break
            try:
                content = self._download(img_url, search_deadline)
                if content is None:
                    continue
                _require_media("image", content[:16])
                _require_image(content)
                _atomic_write(lp, content)
                local_path = lp
                break
            except SoftTimeLimitExceeded:
                raise                               # the task is out of time: do not try the next candidate
            except Exception:
                continue

        if local_path is None:
            return None

        return SourcedAsset(
            source="wikipedia",
            source_ref=page_id,
            local_path=local_path,
            license_str=license_info["license"],
            license_url=license_info["license_url"],
            attribution=license_info["attribution"],
            safe_to_publish=license_info["safe_to_publish"],
            duration_s=0.0,
        )


@dataclass
class SourcedAsset:
    source: str
    source_ref: str
    local_path: Path
    license_str: str
    duration_s: float
    license_url: str | None = None
    attribution: str | None = None
    safe_to_publish: bool = False


class PexelsVideoSource:
    _API = "https://api.pexels.com/videos"

    def __init__(self, api_key: str, store_dir: Path):
        self.api_key = api_key
        self.store_dir = store_dir
        store_dir.mkdir(parents=True, exist_ok=True)
        _sweep_stale_tmp(store_dir)

    @staticmethod
    def _pick(video, min_duration_s: float) -> "tuple[str, float, str] | None":
        """(id, duration_s, download link) for a usable search hit, else None.

        The response is remote JSON: a missing or null field, or a value of the wrong type,
        skips just this hit (like a too-short one) rather than raising into the render task.
        """
        try:
            duration = video.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                return None
            if not math.isfinite(duration):     # NaN/Infinity parse from JSON; a huge int overflows here
                return None
            if duration < min_duration_s:
                return None
            files = video.get("video_files", [])
            if not files:
                return None
            link = _choose_video_file(files).get("link")
            if not link or not isinstance(link, str):
                return None
            vid_id = _numeric_id(video["id"])
            if vid_id is None:
                return None
            return vid_id, float(duration), link
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            _log.warning("Skipping a malformed Pexels search result", exc_info=True)
            return None

    def search(self, query: str, min_duration_s: float) -> SourcedAsset | None:
        if not self.api_key:
            return None

        try:
            resp = _api_get(
                f"{self._API}/search",
                headers={"Authorization": self.api_key},
                params={
                    "query": query,
                    "per_page": 15,
                    "orientation": "portrait",
                    "size": "medium",
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            if _body_too_large(resp, _MAX_API_JSON_BYTES):
                raise ValueError("search response too large")
            videos = resp.json().get("videos", [])
            if not isinstance(videos, list):
                return None
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return None

        search_deadline = _clip_to_asset_budget(_monotonic() + _PEXELS_SEARCH_BUDGET_S)
        for video in videos:
            picked = self._pick(video, min_duration_s)
            if picked is None:
                continue
            vid_id, duration, link = picked
            local_path = self.store_dir / f"pexels_{vid_id}.mp4"

            if not (local_path.exists() and _cached_media_ok(local_path, "video")):
                tmp_path = _tmp_path_for(local_path)
                now = _monotonic()
                if now >= search_deadline:
                    continue                        # budget spent: no more downloads, but a later hit may be cached
                try:
                    deadline_at = min(now + _VIDEO_DEADLINE_S, search_deadline)
                    with _open_download(link, _pexels_url_ok, timeout=120.0,
                                        deadline_at=deadline_at) as r:
                        r.raise_for_status()
                        with open(tmp_path, "wb") as fh:
                            for chunk in _iter_capped(r, _MAX_VIDEO_BYTES, deadline_at):
                                fh.write(chunk)
                    with open(tmp_path, "rb") as fh:
                        _require_media("video", fh.read(16))
                    os.replace(tmp_path, local_path)
                except SoftTimeLimitExceeded:
                    tmp_path.unlink(missing_ok=True)
                    raise                           # the task is out of time: do not start the next hit
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    continue

            return SourcedAsset(
                source="pexels",
                source_ref=vid_id,
                local_path=local_path,
                license_str="pexels_free",
                license_url="https://www.pexels.com/license/",
                attribution=None,
                safe_to_publish=True,
                duration_s=duration,
            )

        return None


class HuggingFaceImageSource:
    """Generates a custom image via HuggingFace Inference API (FLUX.1-schnell by default).

    Used as a last-resort fallback when neither Wikipedia nor Pexels finds a match.
    Generated images are stored locally and cached by a fingerprint of the prompt.
    """

    _API = "https://api-inference.huggingface.co/models"

    def __init__(self, api_key: str, model: str, store_dir: Path):
        self.api_key = api_key
        self.model = model
        self.store_dir = store_dir
        store_dir.mkdir(parents=True, exist_ok=True)
        _sweep_stale_tmp(store_dir)
        # Set by generate() on every call — False on a cache hit (no real API
        # call, no cost) or a failed/skipped call. Callers check this before
        # charging StageEvent.cost_usd, so a cached re-render isn't billed twice.
        self.last_call_was_generated = False

    def generate(self, prompt: str) -> "SourcedAsset | None":
        self.last_call_was_generated = False
        if not self.api_key:
            return None

        # Portrait-oriented prompt for vertical video
        full_prompt = f"{prompt}, portrait orientation, vertical format, cinematic, high quality"
        fp = hashlib.sha256(full_prompt.encode()).hexdigest()[:16]
        local_path = self.store_dir / f"hf_{fp}.png"

        if not (local_path.exists() and _cached_media_ok(local_path, "image")):
            try:
                resp = _api_post(
                    f"{self._API}/{self.model}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"inputs": full_prompt},
                    timeout=60.0,
                    limit=_MAX_IMAGE_BYTES,
                )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    return None
                if _body_too_large(resp, _MAX_IMAGE_BYTES):
                    _log.warning("Discarding a HuggingFace download that is too large for an image")
                    return None
                if not _sniff_ok("image", resp.content[:16]):
                    _log.warning("Discarding a HuggingFace download that is not a valid image")
                    return None
                if not _image_ok(resp.content):
                    _log.warning("Discarding a HuggingFace download that is not a readable image of a reasonable size")
                    return None
                _atomic_write(local_path, resp.content)
                self.last_call_was_generated = True
            except SoftTimeLimitExceeded:
                raise                               # out of time: do not log it as a model failure
            except _TooLarge:
                _log.warning("Discarding a HuggingFace download that is too large for an image")
                return None
            except Exception:
                _log.exception("HuggingFace image generation failed for model %s", self.model)
                return None

        return SourcedAsset(
            source="huggingface",
            source_ref=fp,
            local_path=local_path,
            license_str="generated",
            license_url=None,
            attribution=None,
            safe_to_publish=True,
            duration_s=0.0,
        )


class HuggingFaceVideoSource:
    """Generates a short video clip via HuggingFace Inference API (text-to-video).

    Used as a fallback after Pexels fails and before falling back to static image generation.
    Generated clips are cached locally by a fingerprint of the prompt.
    """

    _API = "https://api-inference.huggingface.co/models"

    def __init__(self, api_key: str, model: str, store_dir: Path):
        self.api_key = api_key
        self.model = model
        self.store_dir = store_dir
        store_dir.mkdir(parents=True, exist_ok=True)
        _sweep_stale_tmp(store_dir)
        # Set by generate() on every call — False on a cache hit (no real API
        # call, no cost) or a failed/skipped call. Callers check this before
        # charging StageEvent.cost_usd, so a cached re-render isn't billed twice.
        self.last_call_was_generated = False

    def generate(self, prompt: str) -> "SourcedAsset | None":
        self.last_call_was_generated = False
        if not self.api_key:
            return None

        full_prompt = f"{prompt}, portrait orientation, vertical format, cinematic, high quality"
        fp = hashlib.sha256(full_prompt.encode()).hexdigest()[:16]

        # Check both possible cached extensions before making an API call
        for cached in (self.store_dir / f"hfvid_{fp}.mp4", self.store_dir / f"hfvid_{fp}.gif"):
            if cached.exists() and _cached_media_ok(cached, "video"):
                local_path = cached
                break
        else:
            try:
                resp = _api_post(
                    f"{self._API}/{self.model}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"inputs": full_prompt},
                    timeout=180.0,  # video generation is slow
                    limit=_MAX_VIDEO_BYTES,
                )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "video/mp4")
                ext = "gif" if "gif" in content_type else "mp4"
                local_path = self.store_dir / f"hfvid_{fp}.{ext}"
                if _body_too_large(resp, _MAX_VIDEO_BYTES):
                    _log.warning("Discarding a HuggingFace download that is too large for a video")
                    return None
                if not _sniff_ok("video", resp.content[:16]):
                    _log.warning("Discarding a HuggingFace download that is not a valid video")
                    return None
                _atomic_write(local_path, resp.content)
                self.last_call_was_generated = True
            except SoftTimeLimitExceeded:
                raise                               # out of time: do not log it as a model failure
            except _TooLarge:
                _log.warning("Discarding a HuggingFace download that is too large for a video")
                return None
            except Exception:
                _log.exception("HuggingFace video generation failed for model %s", self.model)
                return None

        return SourcedAsset(
            source="huggingface_video",
            source_ref=fp,
            local_path=local_path,
            license_str="generated",
            license_url=None,
            attribution=None,
            safe_to_publish=True,
            duration_s=4.0,  # HF text-to-video models typically output ~4 s clips
        )


def get_asset_sourcer(store_dir: Path) -> PexelsVideoSource:
    return PexelsVideoSource(
        api_key=settings.pexels_api_key,
        store_dir=store_dir / "footage",
    )


def get_wiki_sourcer(store_dir: Path) -> WikipediaImageSource:
    return WikipediaImageSource(store_dir=store_dir / "wiki")


def get_hf_sourcer(store_dir: Path) -> HuggingFaceImageSource:
    return HuggingFaceImageSource(
        api_key=settings.huggingface_api_key,
        model=settings.huggingface_image_model,
        store_dir=store_dir / "hf",
    )


def get_hf_video_sourcer(store_dir: Path) -> HuggingFaceVideoSource:
    return HuggingFaceVideoSource(
        api_key=settings.huggingface_api_key,
        model=settings.huggingface_video_model,
        store_dir=store_dir / "hfvid",
    )


_MUSIC_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}


class LocalMusicSource:
    """Matches a beat's music_cue against a local library of royalty-free tracks.

    No external API — Pixabay's public REST API has never documented a Music
    search endpoint (only Images/Video), so this deliberately isn't another
    hosted source. Settings.music_library_dir is a directory the operator
    populates themselves; filenames are treated as mood keyword bags, e.g.
    "tense_minimal_01.mp3" matches a music_cue containing "tense" or "minimal".
    Returns None (no music mixed in) when the library is empty, missing, or
    nothing overlaps — that's the same as today's behavior, never an error.
    """

    def __init__(self, library_dir: Path):
        self.library_dir = library_dir

    def find(self, music_cue: str | None) -> Path | None:
        if not music_cue or not music_cue.strip():
            return None
        if not self.library_dir.is_dir():
            return None

        cue_words = self._keywords(music_cue)
        if not cue_words:
            return None

        best_path, best_score = None, 0
        for path in sorted(self.library_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _MUSIC_EXTS:
                continue
            score = len(cue_words & self._keywords(path.stem))
            if score > best_score:
                best_score, best_path = score, path
        return best_path

    @staticmethod
    def _keywords(text: str) -> set[str]:
        words = re.split(r"[^a-zA-Z]+", text.lower())
        return {w for w in words if len(w) > 2}


def get_music_sourcer() -> LocalMusicSource:
    return LocalMusicSource(Path(settings.music_library_dir))


def _cache_asset(db, result: SourcedAsset, asset_type: str) -> tuple["models.Asset", Path]:
    existing = (
        db.query(models.Asset)
        .filter(
            models.Asset.source == result.source,
            models.Asset.source_ref == result.source_ref,
        )
        .first()
    )
    if existing:
        # Update license info if we have richer data now
        if result.license_url and not existing.license_url:
            existing.license_url = result.license_url
            existing.attribution = result.attribution
            existing.safe_to_publish = result.safe_to_publish
        return existing, Path(existing.local_path)
    asset = models.Asset(
        type=asset_type,
        source=result.source,
        source_ref=result.source_ref,
        local_path=str(result.local_path),
        license=result.license_str,
        license_url=result.license_url,
        attribution=result.attribution,
        safe_to_publish=result.safe_to_publish,
    )
    db.add(asset)
    db.flush()
    return asset, result.local_path


def _generate_gated_hf_asset(db, reel_id, stage, source, query, cost_fn):
    """Runs source.generate(query), gated on (reel_id is not None and source.api_key) exactly
    like the two call sites below used to inline separately. Gated means wrapped in
    record_stage(..., stage, provider="huggingface") — a StageEvent is written only for a
    call that was actually attempted (generate() is a guaranteed no-op with no api_key, and
    hf_video/hf are always constructed regardless of whether the key is set, so this gate
    can't be pushed onto the caller). cost_fn(result) -> float lets each call site supply its
    own cost shape (hf_video_cost_usd() needs the generated result's duration_s; hf_image_cost_usd()
    takes no args) without this helper knowing which source it's wrapping.

    Returns the raw SourcedAsset | None only — never early-returns from resolve_beat_assets()
    and never calls _cache_asset(): that control-flow decision stays in the caller, which owns
    the fallback-chain tiering (Wikipedia → Pexels → HF Video → HF Image → None).

    cost_fn must not raise: it runs inside record_stage()'s `with` block, which re-raises on
    exit rather than swallowing — a raising cost_fn aborts this beat's whole fallback chain,
    unlike every sourcer in this module's own degrade-silently-and-move-to-the-next-tier
    convention (pre-existing behavior, unchanged by this extraction — the inline cost
    calculations this helper replaced had the identical propagation).
    """
    if _asset_budget_spent():
        _log.warning("Skipping %s: the render's asset budget is spent", stage)
        return None
    if reel_id is not None and source.api_key:
        with record_stage(db, reel_id, stage, provider="huggingface") as ev:
            result = source.generate(query)
            ev.detail["cache_hit"] = result is not None and not source.last_call_was_generated
            if source.last_call_was_generated:
                ev.cost_usd = cost_fn(result)
        return result
    return source.generate(query)


def resolve_beat_assets(
    db,
    query: str,
    min_duration_s: float,
    sourcer: PexelsVideoSource,
    wiki: WikipediaImageSource | None = None,
    hf_video: "HuggingFaceVideoSource | None" = None,
    hf: "HuggingFaceImageSource | None" = None,
    reel_id: int | None = None,
) -> list[tuple["models.Asset | None", "Path | None"]]:
    """Return one (Asset, Path) per named person found via Wikipedia.

    Fallback chain: Wikipedia → Pexels → HF Video → HF Image → None.

    reel_id is optional (this function is documented as usable outside the
    pinned resolve_or_reuse() path, where a reel isn't always at hand) — HF
    generation cost is only recorded as a StageEvent when it's provided, and
    only for calls that actually hit the API (cache hits cost nothing; see
    HuggingFace*Source.last_call_was_generated).
    """
    if wiki:
        names = person_names(query)
        found = []
        for i, name in enumerate(names):
            if i > 0:
                time.sleep(0.5)  # avoid Wikimedia CDN 429s on rapid sequential downloads
            result = wiki.search(name)
            if result:
                found.append(result)
        if found:
            # Cache (flush) only after every network call: flushing per name would hold a write
            # transaction open, idle, across the sleeps and searches for the remaining names.
            return [_cache_asset(db, result, "photo") for result in found]

    result = sourcer.search(query, min_duration_s)
    if result is not None:
        return [_cache_asset(db, result, "footage")]

    if hf_video:
        # generate() is a guaranteed no-op without an api_key (never makes a
        # network call) — skip record_stage entirely rather than writing a
        # StageEvent for a call that was never attempted. hf_video/hf are
        # always constructed by render_cut regardless of whether the key is
        # set, so this check can't be pushed onto the caller.
        hf_vid_result = _generate_gated_hf_asset(
            db, reel_id, "asset_hf_video", hf_video, query,
            lambda r: hf_video_cost_usd(r.duration_s if r else 0.0),
        )
        if hf_vid_result:
            return [_cache_asset(db, hf_vid_result, "footage")]

    if hf:
        hf_result = _generate_gated_hf_asset(
            db, reel_id, "asset_hf_image", hf, query,
            lambda r: hf_image_cost_usd(),
        )
        if hf_result:
            return [_cache_asset(db, hf_result, "photo")]

    return [(None, None)]


def _fp(visual_direction: str) -> str:
    """Short fingerprint of a visual_direction string for change detection."""
    return hashlib.sha256(visual_direction.encode()).hexdigest()[:16]


def compute_pins_fingerprint(db, cut_id: int) -> str | None:
    """Deterministic fingerprint of every CutAsset currently bound to this cut, across all
    beats. Used by render_cut (worker/tasks/render.py) to snapshot what actually built a
    render, and by engine/publish/gate.py::assert_video_matches_pins() to detect a later
    re-render that re-pinned an asset and then failed before video_path caught up — see
    docs/specs/2026-09-video-pins-staleness-gate-system-design.md.

    Returns None (not an empty-string hash) when the cut has zero bound CutAsset rows —
    "nothing to fingerprint yet" (every beat black-framed, or not rendered at all), the same
    nullable-render-artifact semantics as black_frame_beat_indices/thumbnail_candidates.

    Order-independent w.r.t. query result ordering: (beat_index, order_in_beat, asset_id) are
    plain ints, so sorting the tuples before hashing guarantees the same pin set always
    produces the same fingerprint regardless of how the DB returns rows — but NOT order-
    independent in the sense of ignoring which asset plays in which beat; a genuine change to
    any one pin changes the fingerprint.
    """
    rows = (
        db.query(models.CutAsset.beat_index, models.CutAsset.order_in_beat, models.CutAsset.asset_id)
        .filter(models.CutAsset.cut_id == cut_id)
        .all()
    )
    if not rows:
        return None
    canonical = "|".join(f"{b}:{o}:{a}" for b, o, a in sorted(rows))
    return hashlib.sha256(canonical.encode()).hexdigest()[:64]


# Sentinel for "a render completed successfully but bound zero real pins" (every beat
# black-framed — resolve_beat_assets()'s whole fallback chain came up empty). Deliberately
# NOT a valid sha256 hexdigest shape, so it can never collide with a real fingerprint.
#
# Why this needs to be distinct from compute_pins_fingerprint()'s own `None` return: that
# `None` is overloaded to mean two different things — "nothing pinned yet" and "this cut's
# render never happened at all" — and engine/publish/gate.py::assert_video_matches_pins()
# treats `cut.rendered_pins_fingerprint is None` as "legacy row, unknown, don't block" (see
# that function's docstring). If render_cut wrote compute_pins_fingerprint()'s raw `None`
# for a genuinely successful all-black-frame render, that render's cut would look
# indistinguishable from a never-rendered/pre-migration one — and a LATER render that pins
# one or more beats to real assets and then fails before finishing would leave live pins
# non-empty while cut.rendered_pins_fingerprint stayed `None`, silently exempted from the
# mismatch check forever (or until a render happens to also produce zero pins again). That
# is not a bounded rollout gap the way the true legacy-row case is — a black-frame outcome
# can recur indefinitely for a niche/topic where asset sourcing keeps failing, so this would
# permanently defeat the staleness gate for exactly the cuts most likely to need it.
EMPTY_PINS_FINGERPRINT = "no-pins-bound"


def compute_pins_fingerprint_for_render(db, cut_id: int) -> str:
    """Like compute_pins_fingerprint(), but never returns None — a render that completes
    with zero bound pins gets EMPTY_PINS_FINGERPRINT instead, so a completed render is
    always distinguishable from "never rendered." Both render_cut (writer) and
    assert_video_matches_pins() (reader) must use THIS function, not the raw
    compute_pins_fingerprint(), for Cut.rendered_pins_fingerprint — using the raw function
    at either site reopens the black-frame staleness hole described above.
    """
    return compute_pins_fingerprint(db, cut_id) or EMPTY_PINS_FINGERPRINT


def resolve_or_reuse(
    db,
    cut: "models.Cut",
    beat_index: int,
    visual_direction: str,
    min_duration_s: float,
    sourcer: PexelsVideoSource,
    wiki: WikipediaImageSource | None = None,
    hf_video: "HuggingFaceVideoSource | None" = None,
    hf: "HuggingFaceImageSource | None" = None,
) -> list[tuple["models.Asset | None", "Path | None"]]:
    """Reuse previously resolved assets for this beat if visual_direction hasn't changed.

    On first render or when the direction changed, re-resolves and updates the pin.
    This makes re-renders deterministic and skips API calls for untouched beats.

    Commits the caller's session (after the read, and again once the pins are written) so no
    transaction is left idle across the network calls it makes or the TTS/ffmpeg work the
    caller does next.
    """
    fingerprint = _fp(visual_direction)

    pinned = (
        db.query(models.CutAsset)
        .filter(
            models.CutAsset.cut_id == cut.id,
            models.CutAsset.beat_index == beat_index,
        )
        .order_by(models.CutAsset.order_in_beat)
        .all()
    )
    db.commit()   # end the read transaction before any network call below

    if pinned and all(p.resolved_from == fingerprint for p in pinned):
        # All pins are current — reuse without any API call
        results = []
        for pin in pinned:
            asset = db.get(models.Asset, pin.asset_id)
            if asset:
                results.append((asset, Path(asset.local_path)))
        db.commit()   # the asset reads above opened a transaction; end it before the caller's TTS
        if results:
            return results

    # Direction changed (or first render) — re-resolve, then delete stale pins
    # and re-pin. Resolve first, delete second: resolve_beat_assets may call
    # record_stage() for an HF generation call, which commits the session — if
    # the stale-pin delete ran first, that commit would land between the
    # delete and the new pin insert below, leaving a beat with no pin at all
    # if the process died in that window. Resolving first means a crash mid-
    # resolve just leaves the old (stale but valid) pin in place.
    results = resolve_beat_assets(
        db, visual_direction, min_duration_s, sourcer, wiki, hf_video, hf, reel_id=cut.reel_id,
    )

    # Remove stale pins for this specific beat only
    db.query(models.CutAsset).filter(
        models.CutAsset.cut_id == cut.id,
        models.CutAsset.beat_index == beat_index,
    ).delete()

    for order, (asset, _) in enumerate(results):
        if asset:
            db.add(models.CutAsset(
                cut_id=cut.id,
                asset_id=asset.id,
                role="footage",
                beat_index=beat_index,
                order_in_beat=order,
                resolved_from=fingerprint,
            ))
    db.commit()   # the caller goes on to TTS/ffmpeg; don't leave the pins uncommitted and idle
    return results
