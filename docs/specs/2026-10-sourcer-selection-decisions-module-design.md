# Asset sourcers' selection decisions — module design (Phase 7z)

Candidate 4 of the full-codebase `improve-codebase-architecture` review: the sourcer adapters'
selection logic had no tests.

## Problem

`engine/render/asset_sourcer.py` mixes HTTP with real decisions, and the decisions were only
reachable through live HTTP: which Pexels file to download (a 4-step portrait/FHD ladder), which
Wikipedia image URL wins and what its file is called, how a Wikimedia license string becomes
`safe_to_publish`, and HuggingFace's content-type / extension / cache rules. Only the HF
`last_call_was_generated` flag and Wikipedia's license-title decoding had tests. The
`safe_to_publish` mapping is the input to the publish gate (`engine/publish/gate.py`), so a wrong
branch is a licensing bug.

## Decision

Characterize first, then extract three pure functions (module-private, same file):

- `_choose_video_file(files, max_height=_FHD) -> dict` — the Pexels ladder: tallest portrait within
  the cap; else smallest portrait; else tallest within the cap; else the first file. A square file
  is portrait. `_FHD = 1920` moved from a local to a module constant.
- `_license_from_extmetadata(meta) -> dict` — Wikimedia `extmetadata` -> `license`, `license_url`,
  `attribution` (HTML stripped), `safe_to_publish` (exact case-insensitive match against
  `_PERMISSIVE_LICENSES`).
- `_image_extension(url) -> str` — jpg/jpeg/png/webp, anything else jpg.

HTTP, caching, retry (Wikipedia 429) and the fallback chain stay in the adapters. Deliberately NOT
done: a unified `MediaSource` interface across the four sourcers (rejected in the HF-gate design
for failing the deletion test) and any behavior change.

## Behavior

Pure refactor. 61 characterization tests were committed first on the old code and pass unmodified
against the extracted code, as do the 42 pre-existing sourcer tests. Conservative behaviors pinned
as current, not endorsed: `CC BY-SA` and `CC BY 3.0` are NOT safe (exact-match set lists only
CC BY 2.0/4.0, CC0, public domain), and a Wikipedia page with a thumbnail but no original image
gets no license lookup and is unsafe.

## Tests

`tests/test_sourcer_selection.py` (71): Pexels (key/params, duration filter, every ladder branch,
no-link and download-failure skipping, cache), Wikipedia (opensearch/summary failures, original ->
thumbnail, 429 retry, extension, cache, page-id fallback, license travel, thumbnail-only), the
license mapping table, HF (non-image 200 rejected, request shape, cache key, gif/mp4, cached gif),
and direct tests of the three helpers. A 28-mutant battery (ladder comparators and min/max, license
case/default/safe, HTML strip, extension default/query/webp, duration boundary, no-link skip, page
size, 429 sleep/continue, thumbnail order, thumbnail license lookup, page-id fallback, cache bypass,
HF content-type/gif/cache/duration) each fails at least one test; the one that initially survived
(square-counts-as-portrait) was a vacuous fixture — a height tie let both branches pick the same
file — and was fixed.
