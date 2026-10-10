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

- ~~The Wikipedia `page_id` fallback (the page title) and the Pexels video id are interpolated into
  local filenames from remote JSON; an over-long title can raise `ENAMETOOLONG`~~ — fixed
  (follow-up 3, see "Follow-up fixes" below).
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

`tests/test_sourcer_contracts.py` (89 at 7z, 124 now), from a second independent mutation review (216 mutants,
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

Three of the pre-existing gaps above were closed afterwards, one commit each (tests written first, red before the fix, and committed with it).

**1. `PexelsVideoSource.search()` degrades instead of raising.** `resp.json()` and the `videos`
list are now read inside the request `try` (a non-JSON body, or a body that is not an object with a
`videos` list, gives `None`). Each hit is validated by `PexelsVideoSource._pick()`, which returns
`(id, duration_s, link)` or `None`: a missing/`null`/non-numeric `duration`, a missing `id`, a
`video_files` entry with a `null` or non-numeric width/height, a non-string `link`, or a hit that is
not an object skips just that hit and the loop moves on to the next (a hit that raises while being
read is logged at WARNING; the plain-validation skips are silent). A missing
`duration` is now "skip" even with `min_duration_s <= 0` (it used to `KeyError` at the end). Successful
searches are unchanged; the one test that pinned `ValueError` propagation now pins `None`.

**2. Guarded downloads (Pexels video, Wikipedia image).** Every download is now https-only to an
expected host: `*.pexels.com` (`_pexels_url_ok`; the apex and look-alikes such as `evilpexels.com`,
`videos.pexels.com.evil.com`, `videos.pexels.com@evil.com` are rejected) and `upload.wikimedia.org`
(`_wikimedia_url_ok`; both `originalimage.source` and `thumbnail.source` point at it, as the
existing fakes' URLs already did). `_https_host()` additionally rejects userinfo, any port but 443
and a non-ASCII netloc (`str.lower()` maps U+212A "K" to ASCII `k`); a trailing-dot host is rejected by
the predicates' exact/suffix match, not by `_https_host` itself. The Pexels
`link` shape is `https://videos.pexels.com/video-files/<id>/<id>-hd_1080_1920_25fps.mp4`; the
test fakes used `https://cdn/...` and were moved to that shape.

Redirects: httpx is never asked to follow them. `_open_download()` streams one hop at a time with
`follow_redirects=False`, re-validates each `Location` (relative ones resolved against the current
URL) with the same predicate, and gives up after `_MAX_REDIRECTS` (3) hops, so an allowed host
cannot bounce the worker to an arbitrary one. Size: `_iter_capped()` rejects a declared
`Content-Length` over the cap before reading and otherwise counts bytes, raising as soon as the
cap is exceeded (`_MAX_VIDEO_BYTES` 250 MB, `_MAX_IMAGE_BYTES` 50 MB); a Pexels partial `.tmp` is
removed and the next hit tried, an oversized Wikipedia original falls back to the thumbnail. To
cap the body, Wikipedia image downloads moved from `httpx.get` to `httpx.stream` (since renamed `_http_stream`, see the Follow-up; the 429 retry
once after 2 s is unchanged). The check guards the download only: a file already cached on disk is
served without a request, as before.

Test changes beyond the new `tests/test_sourcer_download_guards.py` (199 tests; 100 before the review rounds): the three tests
that pinned `follow_redirects=True` now pin `False`; fake image hosts (`https://x/...`, `https://u/...`)
became `upload.wikimedia.org`; and the legacy Wikipedia fakes, which describe a download as a
response returned from a patched `httpx.get`, are adapted to `httpx.stream` (now `_http_stream`) by one autouse fixture
(`_wikipedia_downloads_via_get_fakes` in `test_sourcer_selection.py`, re-exported to the other
sourcer test files) instead of rewriting ~60 call sites. Mutation-checked (12 targeted mutants, all now failing a test; the post-second-429 sleep needed an added
assertion): dropping the host check,
either size check, the redirect bound, relative-`Location` resolution, the scheme, userinfo, port,
`wikimedia` exactness and apex checks, or `follow_redirects=False` each fails a test.

**3. Ids that become file names are validated.** `pexels_{id}.mp4` and `wiki_{id}.{ext}` are built
from remote JSON. A Pexels id must now be a non-negative integer (not a bool) or a string of at most
20 ASCII digits (`_numeric_id`; an int of more than 20 digits is rejected too; `fullmatch` on `[0-9]`, so a trailing newline or Arabic-Indic digits
do not pass); a hit with any other id is skipped like any other malformed hit, before the cache is
consulted. A Wikipedia page id uses the same numeric rule; when `pageid` is missing or unusable the
id comes from the underscored title (`_title_id`): the title itself if it is `[A-Za-z0-9_-]{1,64}` (lowercase-only since the later "Follow-up: the last open limits")
(so `Lionel_Messi` still gives `wiki_Lionel_Messi.jpg`, byte-identical to before), otherwise `t` plus
the first 16 hex of its SHA-256 (deterministic, so the cache still hits). That removes the
`ENAMETOOLONG` from `lp.exists()` (a 5000-character title now works) and means `/`, `..`, `.`,
control characters and non-ASCII never reach a path. Behavior change for the fallback only: an
accented or dotted title without a `pageid` used to be cached as `wiki_<that title>.jpg` and is now
`wiki_t<digest>.jpg` (one re-download; the Wikipedia summary endpoint returns `pageid` for real
pages, so this is a defensive path). A title with a lone surrogate was already unreachable: the
summary URL cannot encode it, so `search()` returns `None`.

`tests/test_sourcer_ids.py` (77) holds the follow-up 3 tests: Pexels id accept/skip tables (bool,
float, negative, traversal, Arabic-Indic digits, trailing newline, 21 digits, a 5000-character id),
Wikipedia page-id fallback, plain-title identity, digest ids for unsafe titles, the 5000-character
title (no `ENAMETOOLONG`), digest determinism and cache reuse. Follow-up 1 replaced one test in
`test_sourcer_contracts.py` (the `ValueError` characterization) and added 24 more (89 -> 113); review rounds 1-2 added 11 more (-> 124).

### Review round 1 (deep 4-persona review of PR #43)

No exploitable allowlist or redirect bypass was found. Fixed afterwards (tests written first):

- **`_pick` could still raise** on a 400-digit `duration` (`OverflowError` from `float()`), and JSON
  `NaN`/`Infinity` passed as durations. Now `math.isfinite` first, and `ArithmeticError` joins the
  caught set.
- **`WikipediaImageSource.search` had the same bug class**: `originalimage: null`, a non-string
  `source` or a non-object body raised out of `search()`. It now degrades to no photo.
- **Host check vs httpx.** `urlsplit` strips tab/CR/LF and accepts hosts httpx rejects or reads
  differently. `_https_host` now refuses a URL containing any whitespace, control character or
  backslash and requires the host to be plain `[a-z0-9.-]` (percent-encoded paths are unaffected).
- **Decompression amplification.** The byte cap counts decoded bytes, but httpx inflates a gzip read
  whole (~270 MB peak from a 3 MB body before the cap tripped). `Accept-Encoding: identity` is now
  always sent and `_iter_capped` rejects a non-identity `Content-Encoding`.
- **No total deadline.** httpx timeouts are per read, so a 1 byte/0.2 s trickle ran for 9 s past a
  1 s timeout. `_iter_capped` now enforces a wall-clock budget (`_VIDEO_DEADLINE_S` 600,
  `_IMAGE_DEADLINE_S` 120), checked per chunk.
- **Silent rejections.** A disallowed URL (first or redirected) is logged at WARNING by host only;
  a CDN host change would otherwise silently push every render to the paid HF tiers.
- **Test gaps** (mutation audit, ~174 mutants): the redirect-status test asserted only `is not None`
  (dropping 301/303/307/308 survived), `_MAX_REDIRECTS`'s value, the single request for a
  Location-less redirect, a cached file vs an unusable link, bool durations, a relative redirect on
  a non-`videos` host, `_iter_capped` yielding a chunk past the cap, and no test used real httpx
  objects (header-name case, gzip) -- all closed; the `_wiki` fake's "must go through httpx.stream"
  assertion was swallowed by `search()`'s `except Exception` and now fails the test.

Known and left open at that time (content sniffing was added later, see "Follow-up: the four limits"): downloaded bytes are not content-sniffed (a 200 `text/html` from an allowlisted
host would be cached as `.mp4`/`.jpg`; HF video accepts any 200 body), HF/search/summary responses
are read without a cap, `_title_id` is case-sensitive (collides on case-insensitive filesystems, only
when `pageid` is missing), and a Wikipedia page with no `pageid` and a non-plain title is re-downloaded
once under its new digest name. Operational note: the allowlist assumes Pexels serves `link` from
`*.pexels.com` (`videos.pexels.com` today); if it moves to another CDN host every Pexels hit is
skipped (now visible as a WARNING) and the render falls through to HF or a black frame.

### Review round 2

Round 2 of the same 4-persona review confirmed round 1's host-differential (300k fuzzed URL mutations,
0 mismatches against `httpx.URL`), gzip, NaN/overflow and malformed-summary fixes closed, a 95-scenario
old-vs-new differential showed only the intended divergences, and a live fetch of a real
`upload.wikimedia.org` image passed the stricter guard (the Pexels host was not checked live). It found, and this round fixed (tests written first):

- **The deadline still did not fire for a slow body.** `iter_bytes(chunk_size=65536)` makes httpx buffer
  64 KB before yielding anything, so the per-chunk check ran only per 64 KB (1 byte every 0.1 s: still
  running after 6 s). `iter_bytes()` without a size yields on every network read; a real-httpx test
  (`MockTransport` with a chunked body) pins it.
- **The budget was per call.** One `deadline_at` is now computed per download and shared by its redirect
  hops (`_open_download` checks it before each hop) and the Wikipedia 429 retry. Still not bounded: a
  server dripping response *headers* inside the read timeout (h11 caps an incomplete event at ~100 KiB) and the up-to-15
  Pexels hits / 2 Wikipedia candidates each getting their own budget; the render task's
  `soft_time_limit` is the outer bound.
- **Log hygiene.** `urlsplit().hostname` strips only tab/CR/LF, so ESC/BEL/NEL and a 100 KB host could
  reach the log. `_host_for_log` returns a short (<=253) plain `[a-z0-9.-]` host or `?`.
- **Test gaps** (158 mutants, 120 killed at HEAD of round 1; the rest equivalent or the gaps below): the
  bool-duration test was vacuous (it searched with a 5 s minimum, so `True` == 1 was skipped by value; now a
  0.5 s minimum), both deadline constants were unpinned to their caller, `>` vs `>=` at the deadline, the
  real-httpx Content-Length test passed on byte counting alone (now only the declared length can reject
  it), userinfo in a logged URL, the `_pick` WARNING, and a literal non-ASCII path character.
- **Docs**: "logged by host" overclaimed (only a disallowed URL is logged); a missing `id` is logged, not
  silent; stale test counts in this spec; "int or <=20 digits" omitted that the cap and the sign rule apply
  to ints too.

Known and left open (unchanged; no content sniffing and the empty-body caching were closed later, see "Follow-up: the four limits"): no content sniffing, uncapped JSON/HF responses, case-sensitive
`_title_id` on case-insensitive filesystems, empty (0-byte) 200 bodies are cached by the existing
`exists()` caches, and the JSON API calls (`opensearch`, summary, `imageinfo`, Pexels search) still send
the default `Accept-Encoding: gzip`.

### Review round 3

Round 3 confirmed round 2's fixes (a real-httpx trickle now stops at the deadline through `_open_download`
+ `_iter_capped` and through the real `search()` paths; a redirect chain and the 429 retry share one
budget; a 20 MB body downloads in ~0.02 s with `iter_bytes()`; 200k tiny chunks in ~1.5 s) and found no
defect of medium severity or higher. Fixed (tests first): `_host_for_log` raised on bytes input and had a
dead `.lower()`; four test gaps from a 104-mutant audit (the Pexels body sharing the connect-phase budget,
the pre-hop deadline boundary, the exact 253-character host bound, a hostile host reaching the log raw
through the download path); dead test code and a stale test name. The two hardening gaps round 3 raised were first documented and then closed in a follow-up (below);
the remaining documented one is a deterministic shared `.tmp` name (two concurrent renders downloading
the same asset could truncate each other; render concurrency is 1; pre-existing). A read overrunning the
budget by up to its httpx timeout, listed here at the time, is bounded by the watchdog (the connect phase before
it arms was bounded later by the hop-timeout clamp; only DNS is left, see the final Follow-up).

### Follow-up: the watchdog and the soft-limit pass-through

- **Watchdog.** The deadline checks in `_open_download` (before each hop) and `_iter_capped` (after each
  network read) only run when httpx yields a response or a body chunk. A host dripping response headers,
  or a chunked-encoding size line / extension, yields neither, and h11 only caps those at ~100 KiB
  (`MAX_INCOMPLETE_EVENT_SIZE`) with each byte allowed up to the read timeout (about 140 days at a 119 s
  drip). `_Watchdog` arms a daemon `threading.Timer` for the remaining budget from httpcore's
  `connection.connect_tcp.complete` trace event, which fires with the raw socket before any response
  exists, and `shutdown(SHUT_RDWR)`s a `dup()` of it when the budget runs out (see Review round 4 for why a
  dup); the blocked read then fails at once and `_open_download` raises
  `ValueError("download too slow: deadline exceeded (connection closed)")` (logged by host). It is
  cancelled when each hop ends and is never armed without a budget. The trace hook needs
  `httpx.Client.stream`: module-level `httpx.stream()` has no `extensions` parameter, so downloads now go
  through a small `_http_stream(method, url, *, extensions=None, **kwargs)` seam (same call shape as
  `httpx.stream`); tests patch `_http_stream`, not `httpx.stream` (a mechanical rename of 22 patch
  targets, plus the two tests that pin the full request kwargs — one per source — now also assert the
  `extensions` trace hook).
  Verified with real raw-socket servers (header drip, chunk-size-line drip, one-byte-per-read body,
  silent server: each cut off near a 0.6 s budget, not the 30 s read timeout), through `_open_download`
  and through the real Pexels and Wikipedia `search()` paths, with no partial file and no Timer thread
  left behind. `tests/test_sourcer_watchdog.py`: 52 tests after review round 4 (31 when first written); 13 targeted
  mutants, 12 killed by the suite and the 13th (the double-arm guard) pinned by a direct trace-hook test;
  7 more after round 4, 5 killed and 2 equivalent.
- **`SoftTimeLimitExceeded`.** It is an `Exception` subclass (billiard), so the search loops' broad
  `except Exception: continue` swallowed it and started the next hit on a fresh budget (the same
  pattern is on `main`). Both loops now catch `celery.exceptions.SoftTimeLimitExceeded` first and
  re-raise (the Pexels loop after removing its partial `.tmp`; Wikipedia's `_atomic_write` already
  cleans up), and `_open_download` never converts it into a deadline error. Every other download
  failure is still swallowed. `engine/` now imports this one exception class from Celery (the
  `worker/` modules already do), preferred over matching on the class name.
- Corrected claims: the earlier "the soft limit is the outer bound" wording was wrong while the swallow
  existed; with the re-raise the task's time limits are again the outer bound for the per-hit budgets
  adding up.

### Review round 4 (the watchdog and soft-limit change)

Four reviewers ran on c2085ca. The soft-limit re-raise, the `_http_stream` seam (a 95-scenario old-vs-new
differential: 0 divergences; no test silently reaches the real network) and the timer lifecycle held. Two
real defects in the watchdog itself, found independently by two reviewers, were fixed (tests first):

- **It did nothing over HTTPS — i.e. for every allowlisted host.** The watchdog kept the raw `socket.socket`
  from `connect_tcp.complete`; httpcore's TLS wrap then *detaches* that object (fileno -1), so at the deadline
  `shutdown()` raised `EBADF`, which was swallowed, while `fired` was already set and the log/error said
  "connection closed". A header drip over TLS ran 21 s against a 1 s budget. All 31 tests used plain
  `http://127.0.0.1`, so none could see it. Now it keeps a `dup()` taken at connect time (`shutdown()` acts
  on the connection, not the descriptor, so the dup works after the wrap and also during a TLS handshake
  drip, which arming at `start_tls.complete` would miss), closed in `cancel()` under a lock shared with
  `_fire`. `fired` is set before the shutdown and reverted if it raised.
- **A shutdown could read as a successful, truncated download.** For a close-delimited body (no
  Content-Length, no chunking) the shutdown looks like a valid EOF, so `_iter_capped` ended normally and the
  partial file was `os.replace`d / `_atomic_write`n into an `exists()` cache that serves it forever
  (chunked and Content-Length bodies raised correctly). `_open_download` now raises after the `yield` when
  `fired`.
- **Test gaps** (~90 mutants): a hostile redirect target having its own watchdog (a shared guard survived),
  the real `_http_stream` (the `MockTransport` helper replaces it: kwargs forwarded, Client closed, 302 not
  followed), `_fire`'s error handling, a non-socket stream, and the `body_trickle` cases never needing the
  watchdog (the per-chunk check covers them; the other drips fail without it). New tests use a self-signed
  cert for 127.0.0.1 (`SSL_CERT_FILE`), a handshake-hang server, close-delimited servers and a
  redirect-then-drip server.
- **Docs**: 22 (not 28) renamed patch targets, two (not three) kwargs-pin tests, a stale
  `httpx.stream` test-suite note, "a read can overrun by its httpx timeout" (now only the connect phase
  before the watchdog arms), "deadline rejections are silent" (a watchdog-enforced one is logged), the h11
  cap (~100 KiB, not ~16/~80 KB), and the `.tmp` wording on soft-limit cleanup (Pexels only).

At that point the soft-limit pass-through covered the two download loops only; the other `except Exception`
blocks (Pexels search, Wikipedia opensearch/summary/license, HuggingFace) were closed afterwards, see
"Follow-up: the four limits".

### Follow-up: the four limits left after review round 4

Four limits had been documented as open at the end of round 4. All four are closed except DNS; tests first,
one commit each (`6bd81c8`, `b3bc584`, `cabf5b3`, `fd7a732`).

- **`SoftTimeLimitExceeded` swallowed by the other `except Exception` blocks.** The two download loops
  re-raised it; the Pexels search call, the Wikipedia opensearch / summary / license lookups and both
  HuggingFace sources still reported it as "no result" / "unknown license" / a logged model failure and let
  the render carry on past an exhausted soft limit. Each now re-raises it first. Every
  `except SoftTimeLimitExceeded` clause in the module (10 now: these 6, the 2 download loops, `_open_download`'s
  pass-through and, after review round 5, `_Watchdog.trace`'s) was removed in turn: each removal fails a test.
  `tests/test_sourcer_soft_limit.py` (18).
- **The connect/TLS phase before the watchdog arms.** It ran on httpx's full timeout (120 s Pexels / 30 s
  Wikipedia), and one read could outlive the budget by its whole read timeout. Each hop's timeout is now
  `min(configured, time left + 1 s)`, from the same single `_monotonic()` reading as the pre-hop deadline
  check (no extra clock call); the 1 s grace (`_HOP_TIMEOUT_GRACE_S`) lets the exact watchdog win once the
  connection exists instead of a racing `ReadTimeout`. Verified through the real httpx/httpcore stack (the
  timeout that reaches `socket.create_connection`). **DNS stays unbounded by us**: `getaddrinfo` takes no
  timeout, only the OS resolver bounds it.
- **Per-hit budgets adding up.** A Pexels search tries up to 15 hits (600 s each: 2.5 h worst case), a
  Wikipedia search 2 candidates. Each `search()` now has one budget (`_PEXELS_SEARCH_BUDGET_S` 900,
  `_WIKI_SEARCH_BUDGET_S` 240, computed once per call); a download's budget is clipped to what is left, and no
  further download starts once it is spent (`now >= search_deadline`; a cached hit is still served — see round 5 for the `break` that first broke that). The
  exact-boundary case (a download that would start exactly when the budget ends) is pinned, because the
  pre-hop check alone would let it through with a 1 s timeout. The eight fake-clock tests in
  `test_sourcer_download_guards.py` were re-counted for the one extra clock reading per search.
  `tests/test_sourcer_budgets.py` (23). Across beats, the render
  task's own time limits remain the outer bound.
- **No content sniffing.** A 200 `text/html`/JSON body (CDN error page, captive portal, compromised allowlisted
  host) or an empty one was cached as `pexels_N.mp4` / `wiki_N.jpg` / `hf_*.png` and reused forever by the
  `exists()` caches (it also closes the "empty body cached forever" note from the security reviews). The first
  16 bytes are checked (`_sniff_ok`, `_require_media`) against what the pipeline decodes: JPEG, PNG, GIF,
  `RIFF…WEBP`, TIFF for images; an MP4 box (`ftyp`/`moov`/`mdat`/`free`/`wide`/`skip` at offset 4), WebM or GIF
  for videos. SVG and BMP are refused on purpose (PIL/MoviePy cannot use an SVG, and Wikimedia's thumbnail of
  an SVG is a PNG, which the fallback then takes). Pexels checks the finished `.tmp` before `os.replace`;
  Wikipedia checks the bytes before `_atomic_write`; HuggingFace checks before writing, so a lying
  `Content-Type` is neither cached nor billed. Files cached before this check existed are served as before.
  **Test seam**: the suite's fakes serve placeholder bytes (`b"orig"`), so `tests/conftest.py` has an autouse
  fixture that makes `_sniff_ok` permissive for every test, and tests that exercise the check carry
  `@pytest.mark.real_media_sniffing` (registered in `pyproject.toml`). `tests/test_sourcer_sniffing.py` (79);
  18 mutants, all killed but one equivalent (the `isinstance` guard, which a non-bytes head also fails
  downstream). One real survivor (WebP accepted without the `RIFF` prefix) got its own case.

Still open at that point (closed later, see "Follow-up: the last open limits"): DNS resolution time,
HuggingFace/JSON API response size, `_title_id` case-sensitivity on a case-insensitive filesystem, the
deterministic shared `.tmp` name, and the per-beat total across a render.

### Review round 5 (the four follow-ups above)

The four reviewers (security, correctness, test quality, docs) found no exploitable issue and no
regression of the earlier guards: a real-file differential (JPEG, MPO, PNG, WebP, GIF, TIFF, MP4 in every layout
ffmpeg can produce, M4V, MOV, WebM, MKV; 44 cases, only the 10 intended rejections differ), polyglot files
(`free`/GIF/EBML heads followed by `#EXTM3U`/`ffconcat` payloads pointing at `file:///etc/passwd`: ffprobe does
not follow them), `httpx.Timeout(0.0)` (never "no timeout"), timing thresholds under 30 busy-loop processes,
~150 mutants. Fixed (tests first):

- **A cache poisoned before the check existed was served forever.** The four `exists()` cache hits were not
  validated, so an old `<html>429…</html>` at `pexels_1.mp4` was returned as a valid asset and failed every render
  in the compositor until someone deleted it by hand. `_cached_media_ok(path, kind)` now reads the first 16
  bytes of a cached file; an invalid or unreadable one is deleted (WARNING) and the code falls through to a
  fresh download. Pexels, Wikipedia and both HuggingFace sources; an empty file counts as invalid.
- **The Pexels budget `break` skipped later cached hits.** After a slow failing hit spent the budget, a LATER hit
  that was already on disk was never served and the render fell through to paid HuggingFace. It is now
  `continue` (later uncached hits cost one clock reading each and start nothing; Wikipedia already behaved
  this way, and now has its own test for a cached second candidate).
- **MP4 first-box list widened** (`styp`, `moof`, `sidx`, `junk`, `pnot`, `uuid` added): decodable CMAF /
  fragmented / QuickTime files starting with those boxes were rejected, and a false reject silently drops a hit
  to the paid tier. A reviewer asked for the opposite (tighten to `ftyp` only, because `\0\0\0\x08free` + MPEG-TS
  passes); declined: ffprobe treats those polyglots as ordinary media, and `_sniff_ok` is documented as a
  sanity check, not a format guarantee.
- **`_Watchdog.trace` could swallow a soft limit** landing in its `dup()` window; it re-raises it.
- **A shared counter made the watchdog redirect tests order-dependent** (`_redirect_then_drip` built its counter
  once at import, so whichever redirect test ran second dripped on its first connection; 5 of 7 shuffled orders
  failed one). It is now a per-server factory, with a regression test that starts two servers in one test.
- **Test hygiene**: `--strict-markers` (a typo'd `real_media_sniffing` silently got the permissive check), an
  unmarked test that pins the permissive seam, near-miss rejects (magic not at offset 0, `II*\x01`, `MMxx`,
  `RIFF…WEBQ`, `\x1a\x45\xdf\x00`), HF image WebP/JPEG (a 12-byte head), `bytearray`, no ERROR-level record on an
  expected HF rejection, a vacuous Wikipedia timeout test renamed to what it checks, unused parameters removed.
- **Docs**: the architecture.md download bullet still listed the closed limits; the re-raise count (9, now 10)
  forgot `_open_download`'s pass-through; "four" fake-clock tests were eight; two spec paragraphs said "still
  swallow"/"only DNS/TCP connect" in the present tense.

Decisions: a sniff-rejected HuggingFace body is **not billed** (consistent with the existing content-type
rejection of a JSON 200; no `asset_hf_*` cost StageEvent is written). Closed afterwards (see "Follow-up: the last three limits"): an image
pixel-count cap (a 140 KB PNG declaring 12000x12000 px decodes to ~430 MB in the compositor; needs PIL parsing in the
sourcer). `record_stage`'s `finally` commit could still swallow a soft limit delivered in that window at that
point (closed afterwards in `engine/observability.py`, see "Follow-up: the last open limits").

### Follow-up: the last open limits

After round 5 the open list was: DNS resolution time, the size of the HuggingFace / JSON API responses,
`_title_id` case-sensitivity, the shared `.tmp` name and `record_stage`'s final commit. All five were taken,
tests first, one commit each (`4a6af13`, `cc1d10a`, `f65aac2`, `e098d4b`, `cdc8eda`); one is only partly
closed, as flagged below.

- **`record_stage` swallowed a soft limit in its `finally` commit** (`engine/observability.py`). The
  `except Exception: db.rollback()` ate Celery's `SoftTimeLimitExceeded` if it landed during the StageEvent
  commit, so the task carried on past its limit. It now rolls the session back and re-raises (replacing any
  error already propagating: the time limit is the more important signal); ordinary commit failures are still
  swallowed so observability never fails the work it observes. `tests/test_observability.py` (3 -> 7).
- **The shared `.tmp` name.** Temp files were fixed per target (`pexels_1.tmp`, `wiki_5.jpg.tmp`), so two
  workers fetching the same asset wrote into one file and the loser's failure or deadline could leave holes
  at the final path. Now `<name>.<8 hex>.tmp` beside the target (same directory, so `os.replace` stays
  atomic), and creating any of the four sources deletes `*.tmp` files older than an hour (`_STALE_TMP_AGE_S`;
  younger ones may be another worker's write in progress; directories and real assets are never touched).
  The simultaneous-write test asserts that no writer raised: a first version passed on the old code only
  because a worker thread died silently, and a Pexels uniqueness test compared names across different
  directories (vacuous until mutation-testing caught it). `tests/test_sourcer_tmp_files.py` (11).
- **`_title_id` and case-insensitive filesystems.** `wiki_Ab.jpg` and `wiki_aB.jpg` are one file on macOS /
  Windows default filesystems, so one page's cached image could be served for another. Only an already
  lowercase `[a-z0-9_-]{1,64}` title is its own id now; any title with a capital goes through the digest
  (computed on the exact title, so `Ab` and `aB` still differ; the digest is itself lowercase hex). A
  capitalised title such as `Lionel_Messi` therefore changes name (one more re-download, new `Asset` row),
  only reachable when `pageid` is missing. `test_sourcer_ids.py` (77 -> 87).
- **DNS resolution time.** `getaddrinfo` takes no timeout, so neither the per-hop httpx timeout nor the
  watchdog (which needs a connection) bounded a stalled resolver. Before each hop with a budget,
  `_dns_in_time` resolves the host on a daemon thread and waits at most the hop's timeout; a lookup still
  pending fails the hop before any request. A lookup that fails passes (httpx reports it), IP literals are
  skipped, and so is any environment-proxy setup (the proxy resolves). **A gate, not a guarantee**: httpx
  resolves again on connect (normally an OS-cache hit), so a resolver that answers once and then stalls is
  not covered; an abandoned hung lookup leaves a daemon thread. A conftest autouse no-op keeps real lookups
  out of the suite; `@pytest.mark.real_dns` opts in. `tests/test_sourcer_dns.py` (25); 14 mutants, all
  killed but a dead `strip("[]")` (removed) and one equivalent.
- **API response size (partly closed).** The six API calls (Pexels search, Wikipedia opensearch / summary /
  imageinfo, both HuggingFace generations) use `httpx.get` / `post`, which read the whole body. Streaming
  them would route six more calls through the `_http_stream` seam and rewrite ~120 test patches whose fakes
  only provide `.json()`, so the transient read buffer was **still unbounded** at this point (closed in "Follow-up: the last three limits"). What is bounded is what is
  done with the body: `_body_too_large` (declared Content-Length, or decoded length, which also catches a
  gzip header that understates) keeps an oversized body from reaching `.json()` (`_MAX_API_JSON_BYTES`, 8 MB)
  or the HuggingFace asset cache (the image / video limits); a rejected HuggingFace body is not billed.
  `tests/test_sourcer_api_limits.py` (22); one survivor (the HF image source using the video limit) got its
  own test.

## Follow-up: the last three limits

* **API read buffer.** The six `httpx.get/post` calls (Pexels search, Wikipedia opensearch / summary / imageinfo,
  both HuggingFace generations) now go through `_api_call`: streamed via `_http_stream`, read with `_iter_capped`
  against the call's limit (`_MAX_API_JSON_BYTES`, or the HF image / video limit), identity encoding, no redirects,
  returned as an ordinary `httpx.Response` built from the capped body. Reading stops at the limit, so the buffer
  is bounded; `_TooLarge` is caught per caller (HF: WARNING, not billed). `_body_too_large` stays as defense in
  depth. To avoid rewriting ~130 test patches, a conftest fixture points `_api_get/_api_post` back at `httpx.get/post`
  unless a test carries `real_api` (`tests/test_sourcer_api_streaming.py`, 30).
* **Image pixel cap.** `_image_ok()` opens only the header with Pillow and refuses an unidentifiable image or one over
  `_MAX_IMAGE_PIXELS` (50 Mpx). Applied to Wikipedia downloads (refused original -> thumbnail), HuggingFace images
  and cached images. Pillow's own bomb warning is silenced so the cap here decides; a test with a lowered Pillow
  limit pins that (the first mutation run found that survivor). `tests/test_sourcer_image_check.py` (36, marker
  `real_image_check`; other tests get a permissive check because their fakes serve placeholder bytes).
* **Per-render total.** `asset_budget(seconds)` (a `ContextVar` deadline, nested budgets only tighten) wraps
  `render_cut`'s beat loop with `RENDER_ASSET_BUDGET_S` = 1200. Each Pexels / Wikipedia search budget is clipped
  to it; once spent no download starts and `_generate_gated_hf_asset` skips the paid tiers (cached files are
  still served by the search paths; a skipped HF tier cannot serve its cache, which is accepted because a
  pinned beat never reaches `resolve_beat_assets()`). TTS time inside the loop counts against the budget.
  `tests/test_sourcer_render_budget.py` (20) plus 2 in `test_render_task.py`.

Mutation checks: 9 + 9 mutants over the image check and the budget, all killed (one survivor, the warning filter,
got its own test). Nothing from the original follow-up lists is open.

