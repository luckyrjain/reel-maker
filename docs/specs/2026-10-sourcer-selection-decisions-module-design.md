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
against the extracted code, as do the 42 pre-existing sourcer tests. Verified further by a
25,000-case differential fuzz of old vs new (randomized Pexels/Wikipedia/Wikimedia payloads,
including malformed values, with identical fakes) comparing results, exceptions, request sequences
and files: no divergence. The exception scope is unchanged — `_license_from_extmetadata` is still
called inside `_fetch_license`'s `try`, while `_choose_video_file`/`_image_extension` are still
outside any `try`, so a `null` width/height in a Pexels file still raises `TypeError` out of
`search()` exactly as before (pre-existing, not pinned, not changed).

Conservative behaviors pinned as current, not endorsed. `_PERMISSIVE_LICENSES` is
`cc0`, `cc-0`, `public domain`, bare `cc by`/`cc-by`, `cc by 2.0`, `cc by 4.0`, `pexels`,
`pexels_free`; matching is exact and case-insensitive, no whitespace stripping. Every other string
— `CC BY-SA` (any version), `CC BY 3.0`, anything padded — is NOT safe. `pexels`/`pexels_free` are
dead entries for the Wikimedia mapper (it never sees them); kept because removing them is a behavior
change. A Wikipedia page with a thumbnail but no original image gets no license lookup and is unsafe.

Pre-existing, found by the security review and left out of this refactor (follow-ups): the
Wikipedia `page_id` fallback (the page title) and the Pexels video id are interpolated into local
filenames from remote JSON (the fixed `wiki_`/`pexels_` prefix means a `/` fails the write rather
than escaping `store_dir`); downloads follow redirects with no host allowlist; `_strip_html` is a
naive regex (attribution only reaches published captions, no template renders it); a Pexels `video["id"]`
missing key raises `KeyError`.

## Tests

`tests/test_sourcer_selection.py` (95): the 61 characterization tests, 10 direct helper tests, and
24 added after review — request shape (User-Agent, params, timeouts, redirects), atomic writes, HF
fingerprints/filenames/headers/logging, every license-set member, ranking by height rather than
width, the landscape FHD boundary, and a search-level test that a literal `+` in a filename reaches
the license lookup unchanged (replacing a pre-existing test that only exercised `urllib.parse`).

Mutation testing, two passes. The author's ~28 targeted mutants found one vacuous fixture
(square-as-portrait: a height tie let both branches pick the same file). An independent reviewer's
~160 mutants then found 24 gaps (above). A final ~65-mutant battery over the whole file —
ladder comparators/min/max/keys/defaults, license set members/case/strip/default, HTML strip,
extension rules, Pexels endpoint/params/timeouts/redirects/chunking/tmp file/duration, Wikipedia
User-Agent/limit/title quoting/canonical title/decode/query strip/atomic write/429/thumbnail order/
cache/page-id, HF content-type/fingerprint/prefix/timeouts/headers/logging/no-key guard — has 0
survivors.
