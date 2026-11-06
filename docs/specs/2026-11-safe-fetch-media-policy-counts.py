"""Count how the tests reach into engine/render/asset_sourcer.py (spec section 6 facts).

Run from the repo root on the baseline revision:  python3 docs/specs/2026-11-safe-fetch-media-policy-counts.py
It reads tests/*.py with regexes and AST-free counting, prints one line per fact, and exits 0.
The MOVED set below is the old-name list of section 4.1 (the names that leave asset_sourcer).
"""
import collections
import glob
import re

MOVED = set("""
_HOP_TIMEOUT_GRACE_S _HOST_RE _https_host _host_for_log _http_stream _Watchdog _dns_in_time _decode_bounded
_content_encoding _api_call _API_MIN_DEADLINE_S _API_DECODABLE_ENCODINGS _API_DROPPED_HEADERS _open_download
_api_get _api_post _iter_capped _body_too_large _TooLarge _MAX_API_JSON_BYTES _MAX_REDIRECTS _REDIRECT_STATUSES
_monotonic RENDER_ASSET_BUDGET_S asset_budget _asset_deadline_at _clip_to_asset_budget _asset_budget_spent
_any_https_host
_IMAGE_MAGIC _VIDEO_MAGIC _MP4_BOXES _sniff_ok _MAX_IMAGE_PIXELS _MAX_FULL_DECODE_PIXELS _MAX_DECODE_BYTES
_MAX_DOWNSCALE_DECODE_BYTES _DECODE_BYTES_PER_PIXEL _DEFAULT_DECODE_BYTES_PER_PIXEL _JPEG_FORMATS
_MAX_TIFF_TILE_SIDE _bytes_per_pixel _pixel_cap _decode_window _tiff_tiles_ok _is_transient _image_ok
_downscale_jpeg _beyond_downscale_window _ORIENTATION_TRANSPOSE _downscale_decoded _OversizedImage _fit_image
_refusal_marker _refusal_stamp _refusal_recorded _require_media _cached_media_ok
""".split())

files = sorted(glob.glob("tests/*.py"))
c = collections.Counter()
mod_importers = set()
mentioning = set()
using_as = set()
set_names = collections.Counter()
mod_targets = collections.Counter()

for f in files:
    s = open(f).read()
    if "asset_sourcer" in s:
        mentioning.add(f)
    if re.search(r"\bAS\.", s):
        using_as.add(f)
    if re.search(r"\b_MOD\b", s):
        mod_importers.add(f)
    c["AS. references"] += len(re.findall(r"\bAS\.\w+", s))
    c["AS._ private references"] += len(re.findall(r"\bAS\._\w+", s))
    c["AS.__name__"] += len(re.findall(r"\bAS\.__name__", s))
    c["monkeypatch.setattr(AS, ...)"] += len(re.findall(r"setattr\(\s*AS,", s))
    c["patch.object(AS, ...)"] += len(re.findall(r"patch\.object\(\s*AS\s*,", s))
    c["patch.object(AS.<mod>, ...)"] += len(re.findall(r"patch\.object\(\s*AS\.\w+", s))
    c["string-target monkeypatch.setattr"] += len(re.findall(r"setattr\(\s*[\"']engine\.render\.asset_sourcer", s))
    c["literal 'engine.render.asset_sourcer.<x>' strings"] += len(re.findall(r"[\"']engine\.render\.asset_sourcer\.\w+", s))
    c["f'{_MOD}.<x>' targets"] += len(re.findall(r"\{_MOD\}\.", s))
    c["f'{_LOG}.<x>' targets"] += len(re.findall(r"\{_LOG\}\.", s))
    c["f'{AS.__name__}.<x>' targets"] += len(re.findall(r"\{AS\.__name__\}\.", s))
    c["caplog.set_level(logger=AS.__name__)"] += len(re.findall(r"set_level\([^)]*logger=AS\.__name__", s))
    c["caplog.at_level(logger=_LOG)"] += len(re.findall(r"at_level\([^)]*logger=_LOG", s))
    for name in ("httpx", "threading", "socket", "settings", "os", "Path", "time"):
        c["AS." + name] += len(re.findall(r"\bAS\." + name + r"\b", s))
    c["_asset_clock"] += len(re.findall(r"_asset_clock", s))
    for m in re.finditer(r"setattr\(\s*AS,\s*\"(\w+)\"", s):
        set_names[m.group(1)] += 1
    for m in re.finditer(r"patch\.object\(\s*AS\s*,\s*\"(\w+)\"", s):
        set_names[m.group(1)] += 1
    for m in re.finditer(r"\{_MOD\}\.([\w.]+)", s):
        mod_targets[m.group(1)] += 1

print("test files mentioning asset_sourcer:", len(mentioning))
print("test files using AS.<name>:", len(using_as))
print("test files using _MOD:", len(mod_importers))
for k, v in c.items():
    print(f"{k}: {v}")
literal_set = sum(set_names.values())
moved_set = sum(v for k, v in set_names.items() if k in MOVED)
print(f"setattr/patch.object(AS, '<literal>') calls: {literal_set}; targeting moved names: {moved_set}")
print("most-patched moved names:", [(k, v) for k, v in set_names.most_common(40) if k in MOVED][:8])
print("{_MOD} target breakdown:", dict(mod_targets))
