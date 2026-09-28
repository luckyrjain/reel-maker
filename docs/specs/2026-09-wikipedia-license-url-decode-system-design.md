# System design: Wikipedia licence lookup uses a percent-encoded filename — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Open Issues table — `Wikipedia licence lookup uses a percent-encoded
filename` (Low severity)

## 1. Problem and non-goals

**Problem, quoting the roadmap's own description verbatim:** *"`_fetch_license()` passes the raw
URL segment, so accented/spaced filenames return 'unknown' and default to `safe_to_publish=False`.
Now live: `assert_safe_to_publish()` blocks these cuts from publishing (Phase 4b), so this
under-detection means some legitimately-safe Wikipedia assets get blocked rather than the reverse (a
licensing false-negative, not a false-positive)."*

Root cause, confirmed by reading `engine/render/asset_sourcer.py::WikipediaImageSource.search()`:

```python
image_filename = original.rsplit("/", 1)[-1].rsplit("?", 1)[0] if original else ""
license_info = self._fetch_license(image_filename) if image_filename else {...}
```

`original` is `data.get("originalimage", {}).get("source")` — a full image URL straight from
Wikipedia's REST summary API response (e.g.
`https://upload.wikimedia.org/wikipedia/commons/x/xx/Lionel_Messi_-_2022146154227%20%28cropped%29.jpg`
for a file with parentheses, or one containing `%C3%A9` for an accented `é`). The last path segment
is taken as-is — still percent-encoded — and passed straight into `_fetch_license()`:

```python
def _fetch_license(self, page_title: str) -> dict:
    resp = httpx.get(self._SEARCH, params={
        "action": "query", "titles": f"File:{page_title}", "prop": "imageinfo", ...
    })
```

MediaWiki's `action=query&titles=` parameter expects the actual page title — the real filename with
spaces/parentheses/accented characters, not its URL-percent-encoded form. Querying
`File:Lionel_Messi_-_2022146154227%20%28cropped%29.jpg` matches no real page (the real title is
`File:Lionel Messi - 2022146154227 (cropped).jpg`, using literal characters). The API returns an
empty/missing-page result, `_fetch_license()`'s parsing falls through to `short = "unknown"`, and
`safe_to_publish` ends up `False` for an asset that may well carry a genuinely permissive license.

**Why this is worth fixing despite being Low severity:** it's a false negative in a *safety* gate
(`assert_safe_to_publish()`, Phase 4b) — the failure direction is "block a legitimately safe asset,"
not "let an unsafe one through," so there's no security regression risk in getting this wrong, only
lost/degraded content (a Wikipedia photo silently replaced by the Pexels/HuggingFace fallback chain,
or a black frame if the whole chain comes up empty) for any subject whose Wikimedia filename happens
to contain a space, accented character, or punctuation Wikipedia URL-encodes — which is common for
non-English names, exactly the population most likely to need Wikipedia (rather than Pexels stock
footage) as a source in the first place.

**Non-goals:**
- Any change to `_PERMISSIVE_LICENSES`, the license-decision logic itself, or `_strip_html()`. This
  fix only concerns getting the *right page* looked up, not what to do once found.
- Any change to `search()`'s own two-step flow (opensearch → REST summary → license lookup) or to
  the `safe` variable at line 111 (`urllib.parse.quote(page_title.replace(" ", "_"))`), which
  constructs the REST *summary* URL from the *already-correctly-decoded* `page_title` returned by
  the opensearch step — that call site is unaffected; the bug is specific to the *second*,
  independent filename extracted from the `originalimage` URL for the *license* lookup, not the
  `page_title` used for the summary fetch.
- An explicit, deliberate backfill migration or admin action for existing rows. Not needed — see §4:
  `_cache_asset()`'s existing-row branch already retroactively updates a cached `Asset`'s license
  fields whenever a fresh `search()` returns richer data than what's stored, so this fix partially
  self-heals already-persisted false negatives without any extra work. This design does not add or
  change that behavior; it only fixes what `search()` looks up, which self-heal already depends on.

## 2. Fix

Decode the extracted filename with `urllib.parse.unquote()` before passing it to
`_fetch_license()`:

```python
image_filename = original.rsplit("/", 1)[-1].rsplit("?", 1)[0] if original else ""
image_filename = urllib.parse.unquote(image_filename)
license_info = self._fetch_license(image_filename) if image_filename else {...}
```

`urllib.parse.unquote()` reverses percent-encoding (`%20` → space, `%C3%A9` → `é`, etc.) and is a
no-op on a filename that was never percent-encoded to begin with (the common case for a plain ASCII
filename with no special characters) — so this fix has zero effect on the majority of lookups that
already work today, and only changes behavior for the exact class of filename the roadmap item
names. No other line in `search()` or `_fetch_license()` needs to change: `_fetch_license()` already
correctly builds `f"File:{page_title}"` from whatever string it's given — it was always doing the
right thing with the wrong input.

**Where exactly to decode, and why not elsewhere:** decoding happens once, right after extraction,
before the `if image_filename else {...}` branch — not inside `_fetch_license()` itself. This keeps
`_fetch_license()`'s existing contract (it already expects a real title-shaped string, as its own
`f"File:{page_title}"` construction implies) unchanged, and keeps the fix colocated with the bug's
actual cause (the raw URL-segment extraction one line above), rather than pushing a URL-decoding
concern into a function whose job is licence lookup, not URL parsing.

**`unquote()`, not `unquote_plus()`.** `image_filename` comes from a URL *path* segment (the last
component of `originalimage.source`), not a query string — Wikimedia never encodes a literal space
as `+` there, so `unquote_plus()` would be wrong: a real filename containing a literal `+` (e.g.
`C++_conference.jpg`) would be incorrectly turned into a space. `unquote()`'s default
`errors="replace"` also means it cannot raise on a malformed percent-sequence — no new exception
path is introduced.

## 3. Data model / API / events

None. No schema, no new endpoint, no `StageEvent`. This is a pure bugfix inside an existing
synchronous call chain (`search()` → `_fetch_license()`), already fully contained within
`WikipediaImageSource`.

## 4. Consistency / state machines

**Correction (adversarial review round 1, before any code existed):** the first draft of this
section claimed `Asset.safe_to_publish` is set once at creation with no retroactive re-evaluation.
That's wrong — `engine/render/asset_sourcer.py::_cache_asset()` dedupes `Asset` rows by
`(source, source_ref)` (Wikipedia's `source_ref` is the page id) and, on a cache hit, **updates the
existing row's license fields** whenever the fresh result has a `license_url` the stored row lacks:

```python
if existing:
    if result.license_url and not existing.license_url:
        existing.license_url = result.license_url
        existing.attribution = result.attribution
        existing.safe_to_publish = result.safe_to_publish
```

Every `Asset` row broken by this bug has `license_url = None` (the failed-lookup fallback always
sets it that way), so `not existing.license_url` is true for exactly the rows this fix helps.
`resolve_or_reuse()` re-runs `search()` (and therefore `_cache_asset()`) for a beat whenever its
`visual_direction` fingerprint changes (a guide edit) or a beat in a different cut/reel references
the same person for the first time under the fixed code — no explicit backfill needed for those
cases; a currently-blocked asset self-heals the next time anything triggers a fresh `search()` call
for that same Wikipedia page. A row nothing ever re-searches (e.g. a one-off subject never
referenced again) stays as-is, exactly like the original claim assumed for the whole table — the
correction is that self-heal is real and automatic for the common case, not that every row heals.

## 5. Failure strategy

Unchanged in shape: `_fetch_license()`'s own `except Exception: return {"license": "unknown", ...,
"safe_to_publish": False}` still applies for a genuine network/parse failure — decoding the filename
does not touch that path. The only behavior change is that a *successful* HTTP call now queries the
*correct* title for a filename that needed decoding, so a request that previously "succeeded" (200
OK, empty/no-match result, silently defaulting to `unknown`) now genuinely finds the real page and
returns its real license data when one exists.

## 6. Observability

None added or needed — this isn't a new failure mode to instrument, it's a correctness fix to an
existing, already-instrumented-at-the-caller-level lookup (Wikipedia asset fetches don't currently
have their own `StageEvent`, consistent with the rest of `asset_sourcer.py`'s sourcers, and this fix
doesn't change that).

## 7. Rollout plan

Single-PR, no migration, no config, no flag. Safe to deploy/revert independently — purely additive
correctness fix to one internal call site, with a backward-compatible no-op for every input that
was already correct.

## 8. Test plan

No existing test file covers `WikipediaImageSource`/`_fetch_license()` at all — new, focused
coverage needed, not a modification of anything existing. At minimum:
- `_fetch_license()` (or `search()`, mocking `httpx.get`) called with a percent-encoded filename
  (e.g. `Lionel_Messi_-_2022146154227%20%28cropped%29.jpg`) results in the *decoded* string
  (`Lionel_Messi_-_2022146154227 (cropped).jpg`) appearing in the `titles=File:...` request param —
  assert on the mocked `httpx.get` call's actual params, not just the return value, since the bug is
  specifically about what gets *sent*, not what comes back for a given (already-correct) input.
- A filename with no percent-encoding at all is passed through unchanged (the no-op case) —
  regression guard against a decode step that mangles an already-correct plain filename.
- A filename containing a literal `+` character is preserved, not turned into a space — the
  `unquote()` vs `unquote_plus()` distinction from §2, made concrete as a test rather than left as
  prose.
- **The self-heal from §4**: a `_cache_asset()` call for an existing `Asset` row with
  `license_url=None` (simulating a row broken by the pre-fix bug), followed by a fresh `search()`
  result carrying a real `license_url`, results in the existing row's `license_url`/`attribution`/
  `safe_to_publish` being updated in place — confirms the correction to §4 is not just a documentation
  claim but an observed, tested behavior.

## Readiness verdict: **Ready for implementation planning.**

Grounded in the actual current code (`engine/render/asset_sourcer.py::WikipediaImageSource`),
`urllib.parse` is already imported in this file, single-line fix with a precise, testable before/
after contract. No existing test file covers `WikipediaImageSource`/`_fetch_license()` at all
(confirmed by repo-wide grep) — the implementation plan should add focused new coverage, not modify
existing tests.
