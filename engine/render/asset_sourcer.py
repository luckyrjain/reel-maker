import hashlib
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from api import models
from api.config import settings
from engine.observability import record_stage
from engine.render.pricing import hf_image_cost_usd, hf_video_cost_usd

_log = logging.getLogger(__name__)


_NAME_RE = re.compile(r'\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+)+\b')

_PERMISSIVE_LICENSES = {
    "cc0", "cc-0", "public domain", "cc by", "cc-by", "cc by 2.0", "cc by 4.0",
    "pexels", "pexels_free",
}


def _extract_first_person_name(query: str) -> str | None:
    matches = _NAME_RE.findall(query)
    return matches[0] if matches else None


def _extract_all_person_names(query: str) -> list[str]:
    return _NAME_RE.findall(query)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes via a temp file + os.replace.

    Every sourcer caches by `if path.exists()`, so a process killed mid-write
    would otherwise leave a truncated file that is reused on every later render.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


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
            short = meta.get("LicenseShortName", {}).get("value", "unknown")
            return {
                "license": short,
                "license_url": meta.get("LicenseUrl", {}).get("value"),
                "attribution": _strip_html(meta.get("Artist", {}).get("value", "")),
                "safe_to_publish": short.lower() in _PERMISSIVE_LICENSES,
            }
        except Exception:
            return {"license": "unknown", "license_url": None, "attribution": None, "safe_to_publish": False}

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
            safe = urllib.parse.quote(page_title.replace(" ", "_"))
            resp = httpx.get(f"{self._SUMMARY}/{safe}", headers=self._HEADERS, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

        original = data.get("originalimage", {}).get("source")
        thumbnail = data.get("thumbnail", {}).get("source")
        candidate_urls = [u for u in (original, thumbnail) if u]
        if not candidate_urls:
            return None

        page_id = str(data.get("pageid", page_title.replace(" ", "_")))

        # Fetch license metadata for rights tracking (needed at publish time)
        image_filename = original.rsplit("/", 1)[-1].rsplit("?", 1)[0] if original else ""
        license_info = self._fetch_license(image_filename) if image_filename else {
            "license": "unknown", "license_url": None, "attribution": None, "safe_to_publish": False
        }

        local_path = None
        for img_url in candidate_urls:
            ext = img_url.rsplit(".", 1)[-1].split("?")[0].lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg"
            lp = self.store_dir / f"wiki_{page_id}.{ext}"
            if lp.exists():
                local_path = lp
                break
            try:
                r = httpx.get(img_url, headers=self._HEADERS, timeout=30.0, follow_redirects=True)
                if r.status_code == 429:
                    time.sleep(2.0)
                    r = httpx.get(img_url, headers=self._HEADERS, timeout=30.0, follow_redirects=True)
                if r.status_code == 429:
                    continue
                r.raise_for_status()
                _atomic_write(lp, r.content)
                local_path = lp
                break
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
        except Exception:
            return None

        for video in resp.json().get("videos", []):
            if video.get("duration", 0) < min_duration_s:
                continue

            files = video.get("video_files", [])
            if not files:
                continue

            _FHD = 1920
            portrait = [f for f in files if f.get("width", 1) <= f.get("height", 1)]
            fhd_portrait = [f for f in portrait if f.get("height", 0) <= _FHD]

            if fhd_portrait:
                chosen = max(fhd_portrait, key=lambda f: f.get("height", 0))
            elif portrait:
                chosen = min(portrait, key=lambda f: f.get("height", 0))
            else:
                fhd_any = [f for f in files if f.get("height", 0) <= _FHD]
                chosen = max(fhd_any, key=lambda f: f.get("height", 0)) if fhd_any else files[0]

            if not chosen.get("link"):
                continue

            vid_id = str(video["id"])
            local_path = self.store_dir / f"pexels_{vid_id}.mp4"

            if not local_path.exists():
                tmp_path = local_path.with_suffix(".tmp")
                try:
                    with httpx.stream(
                        "GET", chosen["link"], follow_redirects=True, timeout=120.0
                    ) as r:
                        r.raise_for_status()
                        with open(tmp_path, "wb") as fh:
                            for chunk in r.iter_bytes(chunk_size=65536):
                                fh.write(chunk)
                    os.replace(tmp_path, local_path)
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
                duration_s=float(video["duration"]),
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
        names = _extract_all_person_names(query)
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
        if reel_id is not None and hf_video.api_key:
            with record_stage(db, reel_id, "asset_hf_video", provider="huggingface") as ev:
                hf_vid_result = hf_video.generate(query)
                ev.detail["cache_hit"] = hf_vid_result is not None and not hf_video.last_call_was_generated
                if hf_video.last_call_was_generated:
                    ev.cost_usd = hf_video_cost_usd(hf_vid_result.duration_s if hf_vid_result else 0.0)
        else:
            hf_vid_result = hf_video.generate(query)
        if hf_vid_result:
            return [_cache_asset(db, hf_vid_result, "footage")]

    if hf:
        if reel_id is not None and hf.api_key:
            with record_stage(db, reel_id, "asset_hf_image", provider="huggingface") as ev:
                hf_result = hf.generate(query)
                ev.detail["cache_hit"] = hf_result is not None and not hf.last_call_was_generated
                if hf.last_call_was_generated:
                    ev.cost_usd = hf_image_cost_usd()
        else:
            hf_result = hf.generate(query)
        if hf_result:
            return [_cache_asset(db, hf_result, "photo")]

    return [(None, None)]


def _fp(visual_direction: str) -> str:
    """Short fingerprint of a visual_direction string for change detection."""
    return hashlib.sha256(visual_direction.encode()).hexdigest()[:16]


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
