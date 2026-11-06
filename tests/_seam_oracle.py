"""Oracle for the safe_fetch / media_policy split (spec 2026-11-safe-fetch-media-policy-module-design.md, 4.5).

Exact literals, no globs, nothing computed from the new modules. Imported by the fidelity script
(docs/specs/2026-11-safe-fetch-media-policy-fidelity.py) and by the guards in tests/test_module_seams.py and
tests/_seam_ast.py (the temporary bridge test of commit 1 used it too and was deleted in commit 2). The names
are those of the baseline revision d096524.

MOVED maps each of the 58 baseline top-level names of engine/render/asset_sourcer.py that leave it to
(owner module name, new name, kind). kind is "callable" (functions, classes and _monotonic: a seam may only be
called or caught), "constant" (a public constant: MAX_API_JSON_BYTES and RENDER_ASSET_BUDGET_S, readable at call
time) or "data" (every other private assignment: never referenced outside its own module).
"""

MOVED = {
    # -> safe_fetch (29)
    "_HOP_TIMEOUT_GRACE_S": ("safe_fetch", "_HOP_TIMEOUT_GRACE_S", "data"),
    "_HOST_RE": ("safe_fetch", "_HOST_RE", "data"),
    "_https_host": ("safe_fetch", "https_host", "callable"),
    "_host_for_log": ("safe_fetch", "_host_for_log", "callable"),
    "_any_https_host": ("safe_fetch", "_any_https_host", "callable"),
    "_http_stream": ("safe_fetch", "_http_stream", "callable"),
    "_Watchdog": ("safe_fetch", "_Watchdog", "callable"),
    "_dns_in_time": ("safe_fetch", "_dns_in_time", "callable"),
    "_decode_bounded": ("safe_fetch", "_decode_bounded", "callable"),
    "_content_encoding": ("safe_fetch", "_content_encoding", "callable"),
    "_api_call": ("safe_fetch", "_api_call", "callable"),
    "_API_MIN_DEADLINE_S": ("safe_fetch", "_API_MIN_DEADLINE_S", "data"),
    "_API_DECODABLE_ENCODINGS": ("safe_fetch", "_API_DECODABLE_ENCODINGS", "data"),
    "_API_DROPPED_HEADERS": ("safe_fetch", "_API_DROPPED_HEADERS", "data"),
    "_open_download": ("safe_fetch", "open_download", "callable"),
    "_api_get": ("safe_fetch", "api_get", "callable"),
    "_api_post": ("safe_fetch", "api_post", "callable"),
    "_iter_capped": ("safe_fetch", "iter_capped", "callable"),
    "_body_too_large": ("safe_fetch", "body_too_large", "callable"),
    "_TooLarge": ("safe_fetch", "TooLarge", "callable"),
    "_MAX_API_JSON_BYTES": ("safe_fetch", "MAX_API_JSON_BYTES", "constant"),
    "_MAX_REDIRECTS": ("safe_fetch", "_MAX_REDIRECTS", "data"),
    "_REDIRECT_STATUSES": ("safe_fetch", "_REDIRECT_STATUSES", "data"),
    "_monotonic": ("safe_fetch", "_monotonic", "callable"),
    "RENDER_ASSET_BUDGET_S": ("safe_fetch", "RENDER_ASSET_BUDGET_S", "constant"),
    "asset_budget": ("safe_fetch", "asset_budget", "callable"),
    "_asset_deadline_at": ("safe_fetch", "_asset_deadline_at", "data"),
    "_clip_to_asset_budget": ("safe_fetch", "clip_to_asset_budget", "callable"),
    "_asset_budget_spent": ("safe_fetch", "asset_budget_spent", "callable"),
    # -> media_policy (29)
    "_IMAGE_MAGIC": ("media_policy", "_IMAGE_MAGIC", "data"),
    "_VIDEO_MAGIC": ("media_policy", "_VIDEO_MAGIC", "data"),
    "_MP4_BOXES": ("media_policy", "_MP4_BOXES", "data"),
    "_sniff_ok": ("media_policy", "sniff_ok", "callable"),
    "_MAX_IMAGE_PIXELS": ("media_policy", "_MAX_IMAGE_PIXELS", "data"),
    "_MAX_FULL_DECODE_PIXELS": ("media_policy", "_MAX_FULL_DECODE_PIXELS", "data"),
    "_MAX_DECODE_BYTES": ("media_policy", "_MAX_DECODE_BYTES", "data"),
    "_MAX_DOWNSCALE_DECODE_BYTES": ("media_policy", "_MAX_DOWNSCALE_DECODE_BYTES", "data"),
    "_DECODE_BYTES_PER_PIXEL": ("media_policy", "_DECODE_BYTES_PER_PIXEL", "data"),
    "_DEFAULT_DECODE_BYTES_PER_PIXEL": ("media_policy", "_DEFAULT_DECODE_BYTES_PER_PIXEL", "data"),
    "_JPEG_FORMATS": ("media_policy", "_JPEG_FORMATS", "data"),
    "_MAX_TIFF_TILE_SIDE": ("media_policy", "_MAX_TIFF_TILE_SIDE", "data"),
    "_bytes_per_pixel": ("media_policy", "_bytes_per_pixel", "callable"),
    "_pixel_cap": ("media_policy", "_pixel_cap", "callable"),
    "_decode_window": ("media_policy", "_decode_window", "callable"),
    "_tiff_tiles_ok": ("media_policy", "_tiff_tiles_ok", "callable"),
    "_is_transient": ("media_policy", "_is_transient", "callable"),
    "_image_ok": ("media_policy", "image_ok", "callable"),
    "_downscale_jpeg": ("media_policy", "_downscale_jpeg", "callable"),
    "_beyond_downscale_window": ("media_policy", "_beyond_downscale_window", "callable"),
    "_ORIENTATION_TRANSPOSE": ("media_policy", "_ORIENTATION_TRANSPOSE", "data"),
    "_downscale_decoded": ("media_policy", "_downscale_decoded", "callable"),
    "_OversizedImage": ("media_policy", "OversizedImage", "callable"),
    "_fit_image": ("media_policy", "fit_image", "callable"),
    "_refusal_marker": ("media_policy", "refusal_marker", "callable"),
    "_refusal_stamp": ("media_policy", "refusal_stamp", "callable"),
    "_refusal_recorded": ("media_policy", "refusal_recorded", "callable"),
    "_require_media": ("media_policy", "require_media", "callable"),
    "_cached_media_ok": ("media_policy", "cached_media_ok", "callable"),
}

# The one public name with no baseline counterpart.
NEW = {"now": ("safe_fetch", "callable")}

# Stdlib modules tests used to reach as AS.<x> (the commit-1 bridge forwarded them to safe_fetch); G1 now flags that.
BRIDGE_EXTRAS = frozenset({"httpx", "threading", "socket"})

# Every non-moved baseline top-level definition of asset_sourcer, plus the imported names tests reach on it.
STAYS = frozenset({
    "_log",
    "_PERMISSIVE_LICENSES",
    "_strip_html",
    "_STALE_TMP_AGE_S",
    "_tmp_path_for",
    "_sweep_stale_tmp",
    "_atomic_write",
    "_MAX_VIDEO_BYTES",
    "_MAX_IMAGE_BYTES",
    "_VIDEO_DEADLINE_S",
    "_IMAGE_DEADLINE_S",
    "_PEXELS_SEARCH_BUDGET_S",
    "_WIKI_SEARCH_BUDGET_S",
    "_pexels_url_ok",
    "_wikimedia_url_ok",
    "_NUMERIC_ID_RE",
    "_SAFE_ID_RE",
    "_numeric_id",
    "_title_id",
    "_FHD",
    "_IMAGE_EXTS",
    "_choose_video_file",
    "_license_from_extmetadata",
    "_image_extension",
    "WikipediaImageSource",
    "SourcedAsset",
    "PexelsVideoSource",
    "HuggingFaceImageSource",
    "HuggingFaceVideoSource",
    "get_asset_sourcer",
    "get_wiki_sourcer",
    "get_hf_sourcer",
    "get_hf_video_sourcer",
    "_MUSIC_EXTS",
    "LocalMusicSource",
    "get_music_sourcer",
    "_cache_asset",
    "_generate_gated_hf_asset",
    "resolve_beat_assets",
    "_fp",
    "compute_pins_fingerprint",
    "EMPTY_PINS_FINGERPRINT",
    "compute_pins_fingerprint_for_render",
    "resolve_or_reuse",
    # imported names that tests legitimately reach on asset_sourcer
    "settings",
    "os",
    "Path",
    "time",
    "record_stage",
})
