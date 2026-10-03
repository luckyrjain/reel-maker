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
outside any `try`, so a `null` width/height in a Pexels file still raised `TypeError` out of
`search()` exactly as before (fixed afterwards, see "Follow-up fixes").

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
- ~~Download URLs come straight from remote JSON with no host check, redirects are followed, no
  size cap~~ — fixed for the Pexels and Wikipedia downloads (follow-up 2, see "Follow-up fixes"
  below). Still open: no size cap on HuggingFace `resp.content` (fixed host, trusted API, but the
  body is read whole into memory).
- `_strip_html` is a naive regex and `Artist` is editable by any Commons uploader (attribution only
  reaches published captions; no template renders it).
- ~~Pexels `video["id"]` missing raises `KeyError`, `duration: null` / `height: null` raise
  `TypeError`, and a non-JSON 200 body raises `ValueError`~~ — fixed (follow-up 1, see
  "Follow-up fixes" below).
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

`tests/test_sourcer_contracts.py` (89), from a second independent mutation review (216 mutants,
92 survivors): the fail-closed license set pinned exactly (additions are as dangerous as removals),
`SourcedAsset`'s fail-closed default, `_atomic_write`, the four factories, `mkdir(parents)`,
`raise_for_status`/malformed-body handling on every HTTP call, Pexels method/stale-tmp/failure paths,
HF URLs/auth/bytes/fingerprints/license fields/cache order/gif detection, `_cache_asset`, and
`resolve_beat_assets` tiers. Re-running the 216-mutant harness against the merged suite leaves 12
survivors: 11 equivalent (missing-dimension defaults Pexels always supplies, a second 429 check a
5xx already preempts, `reel_id=0`, a tmp suffix, a docstring) and the `<>` HTML-strip case, which
then got its own test. A fifth review (457 mutants, 15 meaningful survivors, all re-verified killed)
added 14 more: case preservation of the Pexels query / HF model id / forwarded HF query, the license
lookup title decoded exactly once, no lookup for an empty filename, the 429 retry refetching the same
candidate and only on 429, near-miss and fractional duration boundaries, multi-digit video ids, any
`image/*` content type, the gated StageEvent's `cut_id` and exact `detail`, all-whitespace HTML stripping.

Mutation testing, ten batches. The author's ~28 targeted mutants found one vacuous fixture
(square-as-portrait: a height tie let both branches pick the same file). An independent reviewer's
~160 mutants then found 24 gaps (above). A final ~65-mutant battery over the whole file —
ladder comparators/min/max/keys/defaults, license set members/case/strip/default, HTML strip,
extension rules, Pexels endpoint/params/timeouts/redirects/chunking/tmp file/duration, Wikipedia
User-Agent/limit/title quoting/canonical title/decode/query strip/atomic write/429/thumbnail order/
cache/page-id, HF content-type/fingerprint/prefix/timeouts/headers/logging/no-key guard — has 0
survivors. The fourth batch, the 216-mutant review above, is the one behind
`test_sourcer_contracts.py`. A fifth batch, from a third review, is behind
`test_sourcer_cache_and_chain.py`; its 22 claimed-kill mutants were re-verified with 0 survivors. A
sixth, from a fourth review, added 11 tests and was re-verified the same way (12 claimed kills, 0
survivors); a seventh, from a fifth review, added 14 to `test_sourcer_contracts.py` (15 claimed
kills, 0 survivors); an eighth, from a sixth review (489 mutants, 7 meaningful survivors), added 11
to `test_sourcer_cache_and_chain.py` (7 claimed kills, 0 survivors); a ninth, from a seventh review
(1,112 mutants, 20 meaningful survivors in 7 groups), added 14 tests to `test_sourcer_contracts.py`
and 4 to `test_sourcer_cache_and_chain.py` (15 representative mutants covering every group
re-verified killed, 0 survivors); a tenth, from an eighth review (~830 mutants, mostly
lower-confidence contracts), added 13 tests to `test_sourcer_cache_and_chain.py` (11 claimed kills,
0 survivors).

`tests/test_sourcer_cache_and_chain.py` (56), from a third through eighth review: `_cache_asset` lookup
and heal rules,
`resolve_beat_assets` ordering/argument forwarding and the `reel_id=None` gate (the pre-existing
"no StageEvent" assertions were vacuous because `record_stage` swallows the NOT NULL failure), and
adapter details (Pexels `source_ref`, Wikipedia original==thumbnail, per-candidate extension,
page-title fallback, HF failure logging, HF flag on a failed write) and, from the fourth review
(385 mutants, 12 meaningful survivors), list-position independence of the tallest-portrait pick,
png/webp on a dotted host, parentheses in the summary URL, a cache hit stopping the candidate loop,
`_atomic_write`'s original error, three named people, the HF image tier after an empty video tier, and
the HF flag's initial value, and, from the sixth review, a cached HF asset still being returned (a
`None` would turn every cached re-render into a black frame), the license heal keyed on
`license_url` only and overwriting `safe_to_publish`/`attribution`, the opensearch step's exception
scope, a lone over-cap portrait beating an in-cap landscape file, and a long query reaching the HF
tiers unchanged. The seventh review added: `safe_to_publish` wired from the license mapping and
not from license-URL presence, wrong-shape (valid JSON) bodies failing closed, a cached Wikipedia
thumbnail reused after the original fails, Pexels taking the first qualifying video in API order,
a failed final rename skipped, and `resolve_beat_assets` with every tier supplied at once, as
`render_cut` does (a skipped tier would silently trigger paid HF calls). The eighth review added
lower-confidence contracts: `wiki=None` with a named person, `reel_id` omitted never entering
`record_stage`, the positional order `wiki, hf_video, hf` that `resolve_or_reuse` relies on, no commit
inside `resolve_beat_assets`, tallest-by-height rather than by area, a `%3F` in a filename decoded
after the query split, per-instance `store_dir`, the gated provider literal, the factories reading
settings on every call, and `%`/`?` escaped in the summary URL. Deliberately out of scope and
left as separate test debt: the `resolve_or_reuse` pin ledger, `compute_pins_fingerprint`, and
`LocalMusicSource` — the same file, but not the sourcers' selection logic this candidate is about.

## Follow-up fixes

Three of the pre-existing gaps above were closed afterwards, tests first, one commit each.

**1. `PexelsVideoSource.search()` degrades instead of raising.** `resp.json()` and the `videos`
list are now read inside the request `try` (a non-JSON body, or a body that is not an object with a
`videos` list, gives `None`). Each hit is validated by `PexelsVideoSource._pick()`, which returns
`(id, duration_s, link)` or `None`: a missing/`null`/non-numeric `duration`, a missing `id`, a
`video_files` entry with a `null` or non-numeric width/height, a non-string `link`, or a hit that is
not an object skips just that hit (logged at WARNING) and the loop moves on to the next. A missing
`duration` is now "skip" even with `min_duration_s <= 0` (it used to `KeyError` at the end). Successful
searches are unchanged; the one test that pinned `ValueError` propagation now pins `None`.

**2. Guarded downloads (Pexels video, Wikipedia image).** Every download is now https-only to an
expected host: `*.pexels.com` (`_pexels_url_ok`; the apex and look-alikes such as `evilpexels.com`,
`videos.pexels.com.evil.com`, `videos.pexels.com@evil.com` are rejected) and `upload.wikimedia.org`
(`_wikimedia_url_ok`; both `originalimage.source` and `thumbnail.source` point at it, as the
existing fakes' URLs already did). `_https_host()` additionally rejects userinfo, any port but 443,
a trailing-dot host and a non-ASCII netloc (`str.lower()` maps U+212A "K" to ASCII `k`). The Pexels
`link` shape is `https://videos.pexels.com/video-files/<id>/<id>-hd_1080_1920_25fps.mp4`; the
test fakes used `https://cdn/...` and were moved to that shape.

Redirects: httpx is never asked to follow them. `_open_download()` streams one hop at a time with
`follow_redirects=False`, re-validates each `Location` (relative ones resolved against the current
URL) with the same predicate, and gives up after `_MAX_REDIRECTS` (3) hops, so an allowed host
cannot bounce the worker to an arbitrary one. Size: `_iter_capped()` rejects a declared
`Content-Length` over the cap before reading and otherwise counts bytes, raising as soon as the
cap is exceeded (`_MAX_VIDEO_BYTES` 250 MB, `_MAX_IMAGE_BYTES` 50 MB); a Pexels partial `.tmp` is
removed and the next hit tried, an oversized Wikipedia original falls back to the thumbnail. To
cap the body, Wikipedia image downloads moved from `httpx.get` to `httpx.stream` (the 429 retry
once after 2 s is unchanged). The check guards the download only: a file already cached on disk is
served without a request, as before.

Test changes beyond the new `tests/test_sourcer_download_guards.py` (100 tests): the three tests
that pinned `follow_redirects=True` now pin `False`; fake image hosts (`https://x/...`, `https://u/...`)
became `upload.wikimedia.org`; and the legacy Wikipedia fakes, which describe a download as a
response returned from a patched `httpx.get`, are adapted to `httpx.stream` by one autouse fixture
(`_wikipedia_downloads_via_get_fakes` in `test_sourcer_selection.py`, re-exported to the other
sourcer test files) instead of rewriting ~60 call sites. Mutation-checked: dropping the host check,
either size check, the redirect bound, relative-`Location` resolution, the scheme, userinfo, port,
`wikimedia` exactness and apex checks, or `follow_redirects=False` each fails a test.
