import hashlib
import logging
import math
import os
import re
import sys
import time
import types
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded

from api import models
from api.config import settings
from engine.names import person_names
from engine.observability import record_stage
from engine.render import media_policy, safe_fetch
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


# ── what the adapters ask of the fetch layer ───────────────────────────────
# The mechanism (URL guards, redirects, caps, deadlines, watchdog, asset budget) is in `engine.render.safe_fetch`;
# the limits below are the adapters' own request of it. Byte limits, given to `safe_fetch.iter_capped`:
_MAX_VIDEO_BYTES = 250 * 1024 * 1024   # FHD portrait Pexels clips are tens of MB
_MAX_IMAGE_BYTES = 50 * 1024 * 1024    # Wikipedia originals are rarely over ~20 MB
# Per-download wall-clock budgets, turned into the `deadline_at` given to `safe_fetch.open_download`:
_VIDEO_DEADLINE_S = 600.0
_IMAGE_DEADLINE_S = 120.0
# Per-search budgets (the adapters' own; `safe_fetch.clip_to_asset_budget` clips them to the render's budget).
# One budget per search() call, so the per-download budgets of its hits/candidates cannot add up (a
# Pexels search tries up to 15 hits, a Wikipedia search 2 candidates). A download's own budget is
# clipped to what is left of it, and no further download is started once it is spent.
_PEXELS_SEARCH_BUDGET_S = 900.0
_WIKI_SEARCH_BUDGET_S = 240.0


def _pexels_url_ok(url) -> bool:
    """`*.pexels.com` (videos.pexels.com serves the files), not the apex or a look-alike."""
    host = safe_fetch.https_host(url)
    return host is not None and host.endswith(".pexels.com") and host != ".pexels.com" \
        and ".." not in host


def _wikimedia_url_ok(url) -> bool:
    """upload.wikimedia.org: the image CDN both `originalimage` and `thumbnail` point at."""
    return safe_fetch.https_host(url) == "upload.wikimedia.org"


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
            resp = safe_fetch.api_get(
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
            if safe_fetch.body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES):
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
        now = safe_fetch.now()
        if now >= search_deadline:
            return None
        deadline_at = min(now + _IMAGE_DEADLINE_S, search_deadline)
        for attempt in (0, 1):
            with safe_fetch.open_download(url, _wikimedia_url_ok, timeout=30.0, headers=self._HEADERS,
                                deadline_at=deadline_at) as r:
                if r.status_code != 429:
                    r.raise_for_status()
                    return b"".join(safe_fetch.iter_capped(r, _MAX_IMAGE_BYTES, deadline_at))
            if attempt == 0:
                time.sleep(2.0)
        return None

    def search(self, person_name: str) -> "SourcedAsset | None":
        try:
            resp = safe_fetch.api_get(
                self._SEARCH,
                params={"action": "opensearch", "search": person_name, "limit": 1, "format": "json"},
                headers=self._HEADERS,
                timeout=10.0,
            )
            resp.raise_for_status()
            if safe_fetch.body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES):
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
            resp = safe_fetch.api_get(f"{self._SUMMARY}/{safe}", headers=self._HEADERS, timeout=10.0)
            resp.raise_for_status()
            if safe_fetch.body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES):
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
        search_deadline = safe_fetch.clip_to_asset_budget(safe_fetch.now() + _WIKI_SEARCH_BUDGET_S)
        for img_url in candidate_urls:
            lp = self.store_dir / f"wiki_{page_id}.{_image_extension(img_url)}"
            if lp.exists() and media_policy.cached_media_ok(lp, "image"):
                local_path = lp
                break
            if media_policy.refusal_recorded(lp, img_url):
                continue                            # known too big: don't download it again
            try:
                content = self._download(img_url, search_deadline)
                if content is None:
                    continue
                media_policy.require_media("image", content[:16])
                content = media_policy.fit_image(content)       # may be a downscaled JPEG under a .png/.webp name: Pillow and
                _atomic_write(lp, content)          # the sniff go by content, not extension
                local_path = lp
                break
            except SoftTimeLimitExceeded:
                raise                               # the task is out of time: do not try the next candidate
            except media_policy.OversizedImage:
                try:
                    _atomic_write(media_policy.refusal_marker(lp, img_url), media_policy.refusal_stamp().encode())
                except OSError:
                    pass                            # best effort: the cost is one more download next time
                continue
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
            resp = safe_fetch.api_get(
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
            if safe_fetch.body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES):
                raise ValueError("search response too large")
            videos = resp.json().get("videos", [])
            if not isinstance(videos, list):
                return None
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return None

        search_deadline = safe_fetch.clip_to_asset_budget(safe_fetch.now() + _PEXELS_SEARCH_BUDGET_S)
        for video in videos:
            picked = self._pick(video, min_duration_s)
            if picked is None:
                continue
            vid_id, duration, link = picked
            local_path = self.store_dir / f"pexels_{vid_id}.mp4"

            if not (local_path.exists() and media_policy.cached_media_ok(local_path, "video")):
                tmp_path = _tmp_path_for(local_path)
                now = safe_fetch.now()
                if now >= search_deadline:
                    continue                        # budget spent: no more downloads, but a later hit may be cached
                try:
                    deadline_at = min(now + _VIDEO_DEADLINE_S, search_deadline)
                    with safe_fetch.open_download(link, _pexels_url_ok, timeout=120.0,
                                        deadline_at=deadline_at) as r:
                        r.raise_for_status()
                        with open(tmp_path, "wb") as fh:
                            for chunk in safe_fetch.iter_capped(r, _MAX_VIDEO_BYTES, deadline_at):
                                fh.write(chunk)
                    with open(tmp_path, "rb") as fh:
                        media_policy.require_media("video", fh.read(16))
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
        # True when the last generate() refused to call the paid API because the render's asset budget was
        # spent (a cached asset is still served and leaves this False)
        self.last_call_skipped = False

    def generate(self, prompt: str) -> "SourcedAsset | None":
        self.last_call_was_generated = False
        self.last_call_skipped = False
        if not self.api_key:
            return None

        # Portrait-oriented prompt for vertical video
        full_prompt = f"{prompt}, portrait orientation, vertical format, cinematic, high quality"
        fp = hashlib.sha256(full_prompt.encode()).hexdigest()[:16]
        local_path = self.store_dir / f"hf_{fp}.png"

        if not (local_path.exists() and media_policy.cached_media_ok(local_path, "image")):
            if safe_fetch.asset_budget_spent():                # after the cache lookup: a cached asset is free
                _log.warning("Skipping HuggingFace image generation: the render's asset budget is spent")
                self.last_call_skipped = True
                return None
            try:
                resp = safe_fetch.api_post(
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
                if safe_fetch.body_too_large(resp, _MAX_IMAGE_BYTES):
                    _log.warning("Discarding a HuggingFace download that is too large for an image")
                    return None
                if not media_policy.sniff_ok("image", resp.content[:16]):
                    _log.warning("Discarding a HuggingFace download that is not a valid image")
                    return None
                if not media_policy.image_ok(resp.content):
                    _log.warning("Discarding a HuggingFace download that is not a readable image of a reasonable size")
                    return None
                _atomic_write(local_path, resp.content)
                self.last_call_was_generated = True
            except SoftTimeLimitExceeded:
                raise                               # out of time: do not log it as a model failure
            except safe_fetch.TooLarge:
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
        # True when the last generate() refused to call the paid API because the render's asset budget was
        # spent (a cached asset is still served and leaves this False)
        self.last_call_skipped = False

    def generate(self, prompt: str) -> "SourcedAsset | None":
        self.last_call_was_generated = False
        self.last_call_skipped = False
        if not self.api_key:
            return None

        full_prompt = f"{prompt}, portrait orientation, vertical format, cinematic, high quality"
        fp = hashlib.sha256(full_prompt.encode()).hexdigest()[:16]

        # Check both possible cached extensions before making an API call
        for cached in (self.store_dir / f"hfvid_{fp}.mp4", self.store_dir / f"hfvid_{fp}.gif"):
            if cached.exists() and media_policy.cached_media_ok(cached, "video"):
                local_path = cached
                break
        else:
            if safe_fetch.asset_budget_spent():                # after the cache lookup: a cached asset is free
                _log.warning("Skipping HuggingFace video generation: the render's asset budget is spent")
                self.last_call_skipped = True
                return None
            try:
                resp = safe_fetch.api_post(
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
                if safe_fetch.body_too_large(resp, _MAX_VIDEO_BYTES):
                    _log.warning("Discarding a HuggingFace download that is too large for a video")
                    return None
                if not media_policy.sniff_ok("video", resp.content[:16]):
                    _log.warning("Discarding a HuggingFace download that is not a valid video")
                    return None
                _atomic_write(local_path, resp.content)
                self.last_call_was_generated = True
            except SoftTimeLimitExceeded:
                raise                               # out of time: do not log it as a model failure
            except safe_fetch.TooLarge:
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
    call that was actually attempted or deliberately refused (a budget-skipped call carries
    detail["skipped"] = "asset_budget_spent"). generate() is a guaranteed no-op with no api_key, and
    hf_video/hf are always constructed regardless of whether the key is set, so this gate
    can't be pushed onto the caller. cost_fn(result) -> float lets each call site supply its
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
            if getattr(source, "last_call_skipped", False) is True:
                ev.detail["skipped"] = "asset_budget_spent"     # no call was made: not a success, not a failure
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


# ── TEMPORARY forwarding bridge (commit 1 of the safe_fetch / media_policy split; deleted in commit 2) ────
# Tests still say `AS._image_ok`, `AS._monotonic`, `AS.httpx`, ... (AS = this module). The moved code lives in
# `safe_fetch` / `media_policy` and reads ITS OWN globals, so a bare re-export here could not keep those
# patches working: `monkeypatch.setattr(AS, "_image_ok", fake)` would rebind only the re-exported name.
# This module's class is therefore swapped for one that forwards reads, writes and deletes of the old names
# to the module that owns them now. `httpx`, `threading` and `socket` forward to `safe_fetch`'s imports.
# The moved names are deliberately NOT in this module's __dict__ (a copy would shadow __getattr__).
# Table: old name -> (owning module, name there). Hand-written on purpose; tests/test_bridge_forwarding.py
# checks it against tests/_seam_oracle.py.
_BRIDGE_TABLE = {
    "_HOP_TIMEOUT_GRACE_S": ("safe_fetch", "_HOP_TIMEOUT_GRACE_S"),
    "_HOST_RE": ("safe_fetch", "_HOST_RE"),
    "_https_host": ("safe_fetch", "https_host"),
    "_host_for_log": ("safe_fetch", "_host_for_log"),
    "_any_https_host": ("safe_fetch", "_any_https_host"),
    "_http_stream": ("safe_fetch", "_http_stream"),
    "_Watchdog": ("safe_fetch", "_Watchdog"),
    "_dns_in_time": ("safe_fetch", "_dns_in_time"),
    "_decode_bounded": ("safe_fetch", "_decode_bounded"),
    "_content_encoding": ("safe_fetch", "_content_encoding"),
    "_api_call": ("safe_fetch", "_api_call"),
    "_API_MIN_DEADLINE_S": ("safe_fetch", "_API_MIN_DEADLINE_S"),
    "_API_DECODABLE_ENCODINGS": ("safe_fetch", "_API_DECODABLE_ENCODINGS"),
    "_API_DROPPED_HEADERS": ("safe_fetch", "_API_DROPPED_HEADERS"),
    "_open_download": ("safe_fetch", "open_download"),
    "_api_get": ("safe_fetch", "api_get"),
    "_api_post": ("safe_fetch", "api_post"),
    "_iter_capped": ("safe_fetch", "iter_capped"),
    "_body_too_large": ("safe_fetch", "body_too_large"),
    "_TooLarge": ("safe_fetch", "TooLarge"),
    "_MAX_API_JSON_BYTES": ("safe_fetch", "MAX_API_JSON_BYTES"),
    "_MAX_REDIRECTS": ("safe_fetch", "_MAX_REDIRECTS"),
    "_REDIRECT_STATUSES": ("safe_fetch", "_REDIRECT_STATUSES"),
    "_monotonic": ("safe_fetch", "_monotonic"),
    "RENDER_ASSET_BUDGET_S": ("safe_fetch", "RENDER_ASSET_BUDGET_S"),
    "asset_budget": ("safe_fetch", "asset_budget"),
    "_asset_deadline_at": ("safe_fetch", "_asset_deadline_at"),
    "_clip_to_asset_budget": ("safe_fetch", "clip_to_asset_budget"),
    "_asset_budget_spent": ("safe_fetch", "asset_budget_spent"),
    "_IMAGE_MAGIC": ("media_policy", "_IMAGE_MAGIC"),
    "_VIDEO_MAGIC": ("media_policy", "_VIDEO_MAGIC"),
    "_MP4_BOXES": ("media_policy", "_MP4_BOXES"),
    "_sniff_ok": ("media_policy", "sniff_ok"),
    "_MAX_IMAGE_PIXELS": ("media_policy", "_MAX_IMAGE_PIXELS"),
    "_MAX_FULL_DECODE_PIXELS": ("media_policy", "_MAX_FULL_DECODE_PIXELS"),
    "_MAX_DECODE_BYTES": ("media_policy", "_MAX_DECODE_BYTES"),
    "_MAX_DOWNSCALE_DECODE_BYTES": ("media_policy", "_MAX_DOWNSCALE_DECODE_BYTES"),
    "_DECODE_BYTES_PER_PIXEL": ("media_policy", "_DECODE_BYTES_PER_PIXEL"),
    "_DEFAULT_DECODE_BYTES_PER_PIXEL": ("media_policy", "_DEFAULT_DECODE_BYTES_PER_PIXEL"),
    "_JPEG_FORMATS": ("media_policy", "_JPEG_FORMATS"),
    "_MAX_TIFF_TILE_SIDE": ("media_policy", "_MAX_TIFF_TILE_SIDE"),
    "_bytes_per_pixel": ("media_policy", "_bytes_per_pixel"),
    "_pixel_cap": ("media_policy", "_pixel_cap"),
    "_decode_window": ("media_policy", "_decode_window"),
    "_tiff_tiles_ok": ("media_policy", "_tiff_tiles_ok"),
    "_is_transient": ("media_policy", "_is_transient"),
    "_image_ok": ("media_policy", "image_ok"),
    "_downscale_jpeg": ("media_policy", "_downscale_jpeg"),
    "_beyond_downscale_window": ("media_policy", "_beyond_downscale_window"),
    "_ORIENTATION_TRANSPOSE": ("media_policy", "_ORIENTATION_TRANSPOSE"),
    "_downscale_decoded": ("media_policy", "_downscale_decoded"),
    "_OversizedImage": ("media_policy", "OversizedImage"),
    "_fit_image": ("media_policy", "fit_image"),
    "_refusal_marker": ("media_policy", "refusal_marker"),
    "_refusal_stamp": ("media_policy", "refusal_stamp"),
    "_refusal_recorded": ("media_policy", "refusal_recorded"),
    "_require_media": ("media_policy", "require_media"),
    "_cached_media_ok": ("media_policy", "cached_media_ok"),
    "httpx": ("safe_fetch", "httpx"),
    "socket": ("safe_fetch", "socket"),
    "threading": ("safe_fetch", "threading"),
}


class _BridgeModule(types.ModuleType):
    def __getattr__(self, name):                 # reached only when normal lookup fails
        target = _BRIDGE_TABLE.get(name)
        if target is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(sys.modules["engine.render." + target[0]], target[1])

    def __setattr__(self, name, value):
        target = _BRIDGE_TABLE.get(name)
        if target is None:
            super().__setattr__(name, value)
        else:
            setattr(sys.modules["engine.render." + target[0]], target[1], value)

    def __delattr__(self, name):
        target = _BRIDGE_TABLE.get(name)
        if target is None:
            super().__delattr__(name)
        else:
            delattr(sys.modules["engine.render." + target[0]], target[1])


sys.modules[__name__].__class__ = _BridgeModule
