import glob
import hashlib
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

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


_STALE_TMP_S = 3600.0   # a scratch file untouched this long belongs to a dead process (a download <= 10 min)


def _tmp_for(path: Path) -> Path:
    """A unique scratch path next to `path`, for a write that ends in `os.replace(tmp, path)`.

    The name is unique per call: with a fixed name, two processes fetching the same asset truncated
    each other's file and one then `os.replace`d a mix of both. A process killed mid-download no longer
    has its scratch file overwritten by the next attempt, so this also removes `path`'s scratch files
    older than `_STALE_TMP_S` (best-effort; a live download keeps its file's mtime fresh).
    """
    try:
        cutoff = time.time() - _STALE_TMP_S
        for old in path.parent.glob(glob.escape(path.name) + ".*.tmp"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass
    return path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")


def _is_soft_time_limit(exc: BaseException) -> bool:
    """True for Celery's (billiard's) `SoftTimeLimitExceeded` or a subclass.

    Matched by class name so `engine/` does not import Celery. It is an `Exception` subclass, so an
    `except Exception: continue` swallows it unless the handler re-raises it first.
    """
    return any(c.__name__ == "SoftTimeLimitExceeded" for c in type(exc).__mro__)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes via a temp file + os.replace.

    Every sourcer caches by `if path.exists()`, so a process killed mid-write
    would otherwise leave a truncated file that is reused on every later render.
    """
    tmp = _tmp_for(path)
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
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# httpx timeouts are per read, so a server that trickles never trips them: each download also gets a
# wall-clock budget, shared by its redirect hops / 429 retry. It is enforced three ways: checked before
# every hop and after every network read (cheap, deterministic, covers a trickling body), clamped into the
# httpx timeout of each hop (so one read cannot overrun it by a whole 120 s), and -- the part that covers
# what httpx surfaces no event for, i.e. a server dripping response headers or a chunked-encoding size
# line / extension (h11 caps these at ~80 KB, but each byte may take up to the read timeout) -- a
# `_DeadlineWatchdog` that shuts the connection's socket down when the budget runs out. Still not
# covered: a connect that never completes (bounded by the httpx connect timeout, so by the clamped
# per-hop timeout, but not by the watchdog) and any hit's budget beyond the first: they add up (up to 15
# Pexels hits). The outer bound for those is the render task's soft limit, which the search loops now let
# through (`_is_soft_time_limit`), then its hard limit (+120 s) and the reaper.
_VIDEO_DEADLINE_S = 600.0
_IMAGE_DEADLINE_S = 120.0
_monotonic = time.monotonic     # indirection so tests can drive a fake clock
_MIN_HOP_TIMEOUT_S = 1.0        # floor of the per-hop httpx timeout once the budget is nearly spent


class _DeadlineWatchdog:
    """Shuts a download's socket down when its wall-clock budget runs out.

    Used as httpcore's `trace` hook (a documented request extension) to learn the socket of each
    connection the moment it is made, so it also covers the time before httpx has a response to
    hand out. `threading.Timer` fires `_fire` after `remaining_s`; `shutdown(SHUT_RDWR)` makes the
    blocked read return at once, which httpx reports as a transport error (`_open_download` turns
    that into the usual "too slow" ValueError, see `fired`). `disarm()` is called before a response
    is closed and between hops so a late timer never touches a socket the download has finished with
    (the fd may be recycled); `cancel()` ends the timer thread.
    """

    _SOCKET_EVENTS = ("connection.connect_tcp.complete", "connection.start_tls.complete")

    def __init__(self, remaining_s: float):
        self._lock = threading.Lock()
        self._sock = None
        self.fired = False
        self._timer = threading.Timer(remaining_s, self._fire)     # a negative interval fires at once
        self._timer.daemon = True
        self._timer.start()

    def trace(self, event: str, info: dict) -> None:
        if event not in self._SOCKET_EVENTS:
            return
        try:
            sock = info["return_value"].get_extra_info("socket")
        except Exception:       # a hook must never break the request it observes
            return
        if sock is None:
            return
        with self._lock:
            self._sock = sock
            if self.fired:
                self._shutdown_locked()

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            self._shutdown_locked()

    def _shutdown_locked(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:       # already closed / not connected: nothing left to interrupt
            pass

    def disarm(self) -> None:
        with self._lock:
            self._sock = None

    def cancel(self) -> None:
        self._timer.cancel()
        self.disarm()


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
def _httpx_stream(method: str, url: str, **kwargs):
    """`httpx.stream()` plus the `extensions` argument the module-level helper has no room for.

    `_open_download` needs it to hand httpcore a `trace` hook (see `_DeadlineWatchdog`); everything
    else is what `httpx.stream()` does: a one-shot Client, closed with the response.
    """
    with httpx.Client(follow_redirects=kwargs.pop("follow_redirects", False)) as client:
        with client.stream(method, url, **kwargs) as response:
            yield response


@contextmanager
def _open_download(url: str, url_ok, *, timeout: float, headers: dict | None = None,
                   deadline_at: "float | None" = None):
    """Stream a GET of `url`, following redirects by hand so every hop is checked by `url_ok`.

    Raises ValueError for a disallowed URL (first or redirected), a redirect with no Location, or
    more than `_MAX_REDIRECTS` hops, or if the wall-clock `deadline_at` has passed before a hop.
    Yields the final (non-redirect) response; the caller still
    owns `raise_for_status()` and the status handling. `Accept-Encoding: identity` is always sent
    (media is not worth compressing, and a gzip body would inflate far past its Content-Length
    before the size cap could see it); `_iter_capped` rejects a response that is encoded anyway.
    """
    kwargs = {
        "follow_redirects": False,
        "timeout": timeout,
        "headers": {**(headers or {}), "Accept-Encoding": "identity"},
    }
    watchdog = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            if deadline_at is not None:
                now = _monotonic()
                if now > deadline_at:
                    raise ValueError("download too slow: deadline exceeded before the request")
                if watchdog is None:
                    watchdog = _DeadlineWatchdog(deadline_at - now)
                    kwargs["extensions"] = {"trace": watchdog.trace}
                kwargs["timeout"] = min(timeout, max(deadline_at - now, _MIN_HOP_TIMEOUT_S))
            if not url_ok(url):
                _log.warning("Refusing download from a disallowed URL (host %s)", _host_for_log(url))
                raise ValueError(f"download URL not allowed: {url!r}")
            with _httpx_stream("GET", url, **kwargs) as r:
                if r.status_code not in _REDIRECT_STATUSES:
                    try:
                        yield r
                    finally:
                        if watchdog is not None:
                            watchdog.disarm()
                    return
                location = r.headers.get("location")
                if watchdog is not None:
                    watchdog.disarm()
            if not location or not isinstance(location, str):
                raise ValueError(f"redirect without a Location from {url!r}")
            url = urllib.parse.urljoin(url, location)
        raise ValueError("too many redirects")
    except httpx.HTTPError as exc:
        if watchdog is not None and watchdog.fired:
            raise ValueError("download too slow: deadline exceeded") from exc
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()


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
        raise ValueError(f"download too large: Content-Length {declared} > {limit}")
    total = 0
    for chunk in r.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError(f"download too large: more than {limit} bytes")
        if deadline_at is not None and _monotonic() > deadline_at:
            raise ValueError("download too slow: deadline exceeded")
        yield chunk


# ── ids that become local file names ─────────────────────────────────────────
# `pexels_{id}.mp4` / `wiki_{id}.{ext}` are built from remote JSON, so an id is validated against a
# strict, bounded pattern before it is interpolated (a `/`, `..` or a very long title must never
# reach the filesystem, not even just to be rejected by a failing `Path.exists()`/write).

_NUMERIC_ID_RE = re.compile(r"[0-9]{1,20}")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


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
    """File-name-safe id for a page title: itself if already `[A-Za-z0-9_-]{1,64}`, else a digest."""
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

    def _fetch_license(self, page_title: str) -> dict:
        """Return license metadata for a Wikimedia file via the imageinfo API."""
        try:
            resp = httpx.get(
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
            pages = resp.json().get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            meta = (page.get("imageinfo") or [{}])[0].get("extmetadata", {})
            return _license_from_extmetadata(meta)
        except Exception:
            return {"license": "unknown", "license_url": None, "attribution": None, "safe_to_publish": False}

    def _download(self, url: str) -> "bytes | None":
        """Image bytes from a checked, size-capped GET; None if rate-limited twice.

        A 429 is retried once after a 2 s pause. Any other failure raises (the caller moves on
        to the next candidate URL).
        """
        deadline_at = _monotonic() + _IMAGE_DEADLINE_S
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
            resp = httpx.get(
                self._SEARCH,
                params={"action": "opensearch", "search": person_name, "limit": 1, "format": "json"},
                headers=self._HEADERS,
                timeout=10.0,
            )
            resp.raise_for_status()
            results = resp.json()
            if not results[1]:
                return None
            page_title = results[1][0]
        except Exception:
            return None

        try:
            # safe="": the title is ONE path segment, so a "/" in it ("AC/DC") must become %2F
            safe = urllib.parse.quote(page_title.replace(" ", "_"), safe="")
            resp = httpx.get(f"{self._SUMMARY}/{safe}", headers=self._HEADERS, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
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
        for img_url in candidate_urls:
            lp = self.store_dir / f"wiki_{page_id}.{_image_extension(img_url)}"
            if lp.exists():
                local_path = lp
                break
            try:
                content = self._download(img_url)
                if content is None:
                    continue
                _atomic_write(lp, content)
                local_path = lp
                break
            except Exception as exc:
                if _is_soft_time_limit(exc):
                    raise
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
            resp = httpx.get(
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
            videos = resp.json().get("videos", [])
            if not isinstance(videos, list):
                return None
        except Exception:
            return None

        for video in videos:
            picked = self._pick(video, min_duration_s)
            if picked is None:
                continue
            vid_id, duration, link = picked
            local_path = self.store_dir / f"pexels_{vid_id}.mp4"

            if not local_path.exists():
                tmp_path = _tmp_for(local_path)
                try:
                    deadline_at = _monotonic() + _VIDEO_DEADLINE_S
                    with _open_download(link, _pexels_url_ok, timeout=120.0,
                                        deadline_at=deadline_at) as r:
                        r.raise_for_status()
                        with open(tmp_path, "wb") as fh:
                            for chunk in _iter_capped(r, _MAX_VIDEO_BYTES, deadline_at):
                                fh.write(chunk)
                    os.replace(tmp_path, local_path)
                except Exception as exc:
                    tmp_path.unlink(missing_ok=True)
                    if _is_soft_time_limit(exc):
                        raise
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

        if not local_path.exists():
            try:
                resp = httpx.post(
                    f"{self._API}/{self.model}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"inputs": full_prompt},
                    timeout=60.0,
                )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    return None
                _atomic_write(local_path, resp.content)
                self.last_call_was_generated = True
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
            if cached.exists():
                local_path = cached
                break
        else:
            try:
                resp = httpx.post(
                    f"{self._API}/{self.model}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"inputs": full_prompt},
                    timeout=180.0,  # video generation is slow
                )
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "video/mp4")
                ext = "gif" if "gif" in content_type else "mp4"
                local_path = self.store_dir / f"hfvid_{fp}.{ext}"
                _atomic_write(local_path, resp.content)
                self.last_call_was_generated = True
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
