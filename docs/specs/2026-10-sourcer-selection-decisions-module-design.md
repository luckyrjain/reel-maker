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
for failing the deletion test) and any behavior change (one separate-commit bug fix excepted,
see Behavior).

## Behavior

Pure refactor (extraction commit 3a01b49; the one bug fix is the separate commit ed0831a, below).
61 characterization tests were committed first on the old code and pass unmodified against the
extracted code, as do 41 of the 42 pre-existing sourcer tests (the 42nd, a stdlib-only
`+`-decoding test, was replaced in review by a search-level test). Verified further by a
25,000-case differential fuzz of old vs new (randomized Pexels/Wikipedia/Wikimedia payloads,
including malformed values, with identical fakes) comparing results, exceptions, request sequences
and files: no divergence. The exception scope is unchanged — `_license_from_extmetadata` is still
called inside `_fetch_license`'s `try`, while `_choose_video_file`/`_image_extension` are still
outside any `try`, so a `null` width/height in a Pexels file still raises `TypeError` out of
`search()` exactly as before (pre-existing, not pinned, not changed).

One deliberate bug fix, in its own commit: the Wikipedia summary URL used `quote()`'s default
`safe="/"`, so a title containing a slash ("AC/DC") requested `.../summary/AC/DC` rather than one
encoded path segment; it is now `quote(..., safe="")` (found by the second mutation review).

Conservative behaviors pinned as current, not endorsed. `_PERMISSIVE_LICENSES` is
`cc0`, `cc-0`, `public domain`, bare `cc by`/`cc-by`, `cc by 2.0`, `cc by 4.0`, `pexels`,
`pexels_free`; matching is exact and case-insensitive, no whitespace stripping. Every other string
— `CC BY-SA` (any version), `CC BY 3.0`, anything padded — is NOT safe. `pexels`/`pexels_free` are
dead entries for the Wikimedia mapper (it never sees them); kept because removing them is a behavior
change. A Wikipedia page with a thumbnail but no original image gets no license lookup and is unsafe.

Pre-existing, found by the security reviews and left out of this refactor (follow-ups, none
introduced here):

- The Wikipedia `page_id` fallback (the page title) and the Pexels video id are interpolated into
  local filenames from remote JSON. The fixed `wiki_`/`pexels_` prefix means a `/` fails the write
  inside a `try` rather than escaping `store_dir` (no traversal), but `wiki_{page_id}` is unbounded
  in length, so an over-long title can raise `ENAMETOOLONG` from `lp.exists()` outside the `try`.
- Download URLs (`chosen["link"]` for Pexels, `originalimage`/`thumbnail` `source` for Wikipedia)
  come straight from remote JSON with no host check at all, and redirects are then followed — blind
  SSRF if an upstream response is tampered with. The Pexels bytes are saved as `.mp4` and handed to
  MoviePy/ffmpeg, which probes by content. Precondition is a hostile or compromised upstream over
  HTTPS (low to medium). There is also no size cap on downloads or on HuggingFace `resp.content`.
- `_strip_html` is a naive regex and `Artist` is editable by any Commons uploader (attribution only
  reaches published captions; no template renders it).
- Pexels `video["id"]` missing raises `KeyError`, `duration: null` / `height: null` raise
  `TypeError`, and a 200 response with a non-JSON body raises `ValueError` from `resp.json()` — all
  outside any `try` in `search()`, so they propagate into the render task (pinned for the last one
  as characterization).
- The publish gate checks the license string only, not that an attribution is present: bare
  `CC BY` / 2.0 / 4.0 with no `Artist` is "safe" with an empty attribution, which
  `build_attribution_block` then omits. `LicenseShortName` is itself uploader-editable, so
  `pexels`/`pexels_free` being "dead" only means our code never produces them.

## Tests

`tests/test_sourcer_selection.py` (96): the 61 characterization tests, 10 direct helper tests, and
25 added after review — request shape (User-Agent, params, timeouts, redirects), atomic writes, HF
fingerprints/filenames/headers/logging, every license-set member, ranking by height rather than
width, the landscape FHD boundary, and a search-level test that a literal `+` in a filename reaches
the license lookup unchanged (replacing a pre-existing test that only exercised `urllib.parse`).

`tests/test_sourcer_contracts.py` (61), from a second independent mutation review (216 mutants,
92 survivors): the fail-closed license set pinned exactly (additions are as dangerous as removals),
`SourcedAsset`'s fail-closed default, `_atomic_write`, the four factories, `mkdir(parents)`,
`raise_for_status`/malformed-body handling on every HTTP call, Pexels method/stale-tmp/failure paths,
HF URLs/auth/bytes/fingerprints/license fields/cache order/gif detection, `_cache_asset`, and
`resolve_beat_assets` tiers. Re-running the 216-mutant harness against the merged suite leaves 12
survivors: 11 equivalent (missing-dimension defaults Pexels always supplies, a second 429 check a
5xx already preempts, `reel_id=0`, a tmp suffix, a docstring) and the `<>` HTML-strip case, which
then got its own test.

Mutation testing, four batches. The author's ~28 targeted mutants found one vacuous fixture
(square-as-portrait: a height tie let both branches pick the same file). An independent reviewer's
~160 mutants then found 24 gaps (above). A final ~65-mutant battery over the whole file —
ladder comparators/min/max/keys/defaults, license set members/case/strip/default, HTML strip,
extension rules, Pexels endpoint/params/timeouts/redirects/chunking/tmp file/duration, Wikipedia
User-Agent/limit/title quoting/canonical title/decode/query strip/atomic write/429/thumbnail order/
cache/page-id, HF content-type/fingerprint/prefix/timeouts/headers/logging/no-key guard — has 0
survivors. The fourth batch, the 216-mutant review above, is the one behind
`test_sourcer_contracts.py`.
