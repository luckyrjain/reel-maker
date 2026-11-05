# `safe_fetch` and `media_policy` — splitting the hardened fetch and the media check out of `asset_sourcer`

Status: implemented (Phase 8g, branch `c2-safe-fetch-media-policy`; §10 is the implementation record). Spec revision 7 (after six spec-review rounds; rounds 1–5 were NOT CLEAN for all four personas; round 6 found one MEDIUM (the oracle `kind` rule) and wording items, all fixed in revision 7; findings and what
changed are in §11). Source: candidate C2 of the 2026-11-03 full-codebase architecture review (strength "Worth
exploring", medium confidence), grilled and settled in a twelve-question session (Q1–Q12, §3). Scope:
`engine/render/asset_sourcer.py` (split), two new modules, `worker/tasks/render.py` (its import and three use
sites), `tests/` (retarget, a guard file, behavior probes), and the docs that describe the module. No schema
change, no new endpoint, no behavior change except the deltas listed in §5.

Line numbers refer to `origin/main` (`d096524`); refer to code and tests by name. Counts in this document come
from `docs/specs/2026-11-safe-fetch-media-policy-counts.py` run on that commit (its output is in §10), not estimates.

Terminology: **fetch budget** — the nested wall-clock limits on remote asset I/O (per download, per `search()`,
render-wide `asset_budget()`); **media policy** — admit, downscale or refuse a downloaded asset (both in
`CONTEXT.md`). **Adapter** — one of the four sources (Pexels, Wikipedia, HF image, HF video); `LocalMusicSource` and the id/selection
helpers stay with them. **Seam** — a moved
function or constant that another module calls or that tests patch (**Adapter** and **Seam** are added to
`CONTEXT.md` by §8). **Bridge** — the temporary forwarding shim of §6 commit 1 (not a lasting term).

## 1. Problem

`engine/render/asset_sourcer.py` is 1773 lines and holds four things with different reasons to change:

| Concern | Where (by name) | Changes when |
|---|---|---|
| Hardened HTTP: URL/host guards, manual redirects, size caps, gzip/deflate under a cap, wall-clock deadlines, `_Watchdog`, DNS gate, fetch budget | `_https_host` … `_iter_capped`, `asset_budget` and the clock, the redirect/API caps | a download attack or limit is found |
| Media policy: magic-byte sniff, Pillow decode, pixel caps, downscale, TIFF tile guard, `.refused` markers | `_sniff_ok` … `_cached_media_ok`, the `_MAX_*_PIXELS`/decode caps | a format or decode risk is found |
| Adapters and their helpers: Pexels, Wikipedia, HF image, HF video, music, id/selection rules | `PexelsVideoSource` … `LocalMusicSource`, `_choose_video_file`, `_numeric_id`, … | an upstream API or selection rule changes |
| Ledger and chain: `_cache_asset`, `resolve_beat_assets`, `resolve_or_reuse`, pins fingerprint | `_cache_asset` … `resolve_or_reuse` | the fallback chain or pin policy changes |

The first two are policy with no code that depends on any source (a few comments still name Pexels or Wikipedia as examples), yet they are interleaved with the adapters in one
file. 27 test files mention the module and 15 of them reach into it through `AS.<name>` (474 `AS.` references:
361 private names, 31 `AS.__name__`, the rest public or stay-names). Four autouse conftest fixtures plus one
fixture for the Wikipedia download adapter switch the policy off for every other test (the "five switch-offs",
§6). The tests already treat "fetch under a budget" and "is this usable media" as separate things; the code does
not.

A unified `MediaSource` interface across the four adapters stays rejected (their `search()`/`generate()`
shapes differ). C2 cuts a different line: policy (fetch, media check) versus adapters.

## 2. Goals, non-goals, known gaps

Goals
- Two new modules whose interfaces are the test surface of the policy: `engine/render/safe_fetch.py`,
  `engine/render/media_policy.py`.
- `asset_sourcer.py` keeps adapters, factories, `LocalMusicSource`, id/selection helpers, the cache ledger and
  the fallback chain, and contains no socket, redirect, decode or pixel logic.
- A pure move: every moved definition is unchanged apart from renamed references and the docstring/comment edits
  listed in §5, and every rewritten adapter call site differs from its original only by the renamed identifier and
  the module qualifier (and `_monotonic()` → `safe_fetch.now()`) (§6: fidelity checks A, B and C plus a dependency
  check).
- Tests retarget to the module that owns each name; guards make a mis-targeted or captured seam a failing test
  (§4.5), and behavior probes show each seam is consumed from where tests patch it (§6).

Non-goals
- No behavior change, no new limit, no new check, no change to any cap or deadline value.
- No `MediaSource` interface; no move of the adapters; no change to `_atomic_write`/`_tmp_path_for`/
  `_sweep_stale_tmp` (adapter-only users).
- No re-export from `asset_sourcer` in the final tree (Q2); the bridge exists from commit 1 until commit 2 deletes it.
- No change to `engine/publish/gate.py` or `compute_pins_fingerprint*` (C3 territory).

Known gaps (stated, not closed)
- `caplog.set_level(..., logger=<name>)` at WARNING cannot detect a stale logger name (records propagate to the
  root handler); all 22 uses are retargeted by hand, and logger-name tests per new module (§6) pin D4 directly.
- The default `url_ok` of the public `api_get`/`api_post` is "any https host except IP literals/localhost"
  (inherited). `safe_fetch` is host-agnostic for `open_download` (the caller supplies `url_ok`), not for the API
  helpers; making `url_ok` required there is a follow-up, not part of a pure move. The two docstrings say so (§5).
- The refusal-marker *write* stays in the Wikipedia adapter (Q5), so the marker's name/stamp (media_policy) and
  its write (asset_sourcer) live in different modules by design.
- `body_too_large`/`TooLarge` are public in `safe_fetch` because the adapters use them (Q7's rule); a pure move does
  not redesign them behind an `api_get`-level helper.
- The fidelity script is a one-time proof pinned to a baseline revision, not a standing CI guard (§6).

## 3. Settled decisions

From the grilling session:

| # | Decision |
|---|---|
| Q1 | Two modules, not one: fetch and media check have independent change reasons; `media_policy` depends on nothing in `safe_fetch`. |
| Q2 | Tests retarget to the owning module; no re-exports in the final tree; conftest patches become `raising=True`. |
| Q3 | The PR is a pure move. |
| Q4 | The clock and the asset budget live in `safe_fetch`. |
| Q5 | `_atomic_write`, `_tmp_path_for`, `_sweep_stale_tmp` stay in `asset_sourcer`; the refusal-marker write stays adapter-side. |
| Q6 | Names: `engine/render/safe_fetch.py`, `engine/render/media_policy.py`. |
| Q7 | Only names that cross a module boundary become public (§4.2). |
| Q8 | `_pexels_url_ok`/`_wikimedia_url_ok` stay with the adapters, passed as `url_ok`. |
| Q9 | An AST guard fails on a stale patch target and on conftest `raising=False` (extended in §4.5). |
| Q10 | Order: baseline (recorded in §10, no commit), move with a bridge, retarget and drop the bridge, guard test, docs. **Refined by D5:** the bridge forwards `setattr`; a plain re-export cannot work. |
| Q11 | `CONTEXT.md` gains **fetch budget** and **media policy**. |
| Q12 | Same pipeline as C1: spec, up to 6 review rounds, plan, subagent-driven development, 4-persona code review until clean, PR. |

Derived while writing the spec (not separately grilled; reviewers and the user may challenge them):

- **D1 — adapter-owned constants stay with the adapters.** `_VIDEO_DEADLINE_S`, `_IMAGE_DEADLINE_S`,
  `_PEXELS_SEARCH_BUDGET_S`, `_WIKI_SEARCH_BUDGET_S`, `_MAX_VIDEO_BYTES`, `_MAX_IMAGE_BYTES` are read only by
  adapters (AST-verified); they are the adapter's *request* of the fetch layer (`deadline_at=` for downloads, the
  byte limit given to `iter_capped`). Constants moved code reads move with it (`_MAX_REDIRECTS`,
  `_REDIRECT_STATUSES`, `_MAX_API_JSON_BYTES`, the pixel/decode constants).
- **D2 — one clock, one patch point.** `safe_fetch` owns `_monotonic = time.monotonic` and `now()`; adapters and
  `worker/tasks/render.py` call `safe_fetch.now()`. Tests patch `safe_fetch._monotonic` and never `safe_fetch.now`
  (patching `now` would move the adapters' deadlines but not `asset_budget`/`open_download`/`iter_capped`/
  `_Watchdog`, which read `_monotonic` directly: a split clock). G1 rejects a patch target of `now`. Today
  `render.py` binds its own copy at import (`_monotonic as _asset_clock`), so `AS._monotonic` patches never
  reached it and two render tests patch both; after the move one patch reaches both and the `_asset_clock`
  patches are deleted (§6). Moved `open_download` keeps its local variable `now` (baseline), which shadows the
  module-level `now()` inside that function; harmless because moved code uses `_monotonic()`, and a later edit must
  not call `now()` there.
- **D3 — module-attribute access at every seam.** Code outside a policy module calls its seams as
  `safe_fetch.open_download(...)`: never `from … import <name>` (public *or* private), never a star-import, and
  never a reference that holds a seam beyond the expression that uses it (no alias, `functools.partial`,
  container, decorator argument, instance attribute or default argument). Two kinds of seam exist, recorded per
  name in `tests/_seam_oracle.py`: **callables** (functions and classes) may appear only as the callee of a call or
  as the type in an `except` clause; **constants** (`MAX_API_JSON_BYTES`, `RENDER_ASSET_BUDGET_S`) may be *read*
  inside a function body as a call argument or operand (evaluated at call time, so a test patch is seen). A
  `from`-import, alias or default binds the object at import time, so a test patching the owning module's
  attribute would silently not affect the caller. Inside a module calls stay plain global lookups, so a patch on
  that module affects its own internals as `AS._x` did. Outside the two modules, production code references only
  the 22 public names (no underscore attribute).
- **D4 — loggers.** Each new module defines `_log = logging.getLogger(__name__)` immediately after its imports (as the baseline does). Exception for commit 1 only:
  both use `logging.getLogger("engine.render.asset_sourcer")` (a comment marks it temporary), because
  `test_wikipedia_a_downscale_is_logged` sets INFO on `AS.__name__` and asserts a `fit_image` record, and the
  bridge forwards attributes, not loggers. Commit 2 switches both modules to `__name__` together with the
  retarget of the 22 `caplog` uses.
- **D5 — the bridge forwards writes.** A plain `from safe_fetch import …` re-export in `asset_sourcer` cannot
  keep tests green: `monkeypatch.setattr(AS, "_image_ok", fake)` would rebind only the re-exported name while
  moved code reads `media_policy`'s own global, so the conftest switch-offs (5 `monkeypatch.setattr` calls, all `raising=False`), the
  129 `setattr`/`patch.object` calls that target moved names and the fake HTTP transport would silently stop
  applying (real DNS — a failed lookup "passes" —, real Pillow, real clocks; negative-assertion tests would pass
  for the wrong reason). The commit-1 bridge is a module-class swap (`sys.modules[__name__].__class__ = …`)
  with these requirements, each pinned by the bridge test:
  - `__getattr__(name)` (reached only when normal lookup fails) returns the owner's attribute for table names and
    raises **`AttributeError`** (not `KeyError`) for anything else;
  - `__setattr__`/`__delattr__` for table names write/delete on the **owner**; non-table names (`settings`,
    `Path`, `os`, `_atomic_write`, …) behave as on a normal module;
  - a non-local `patch`/`patch.object` exit performs `delattr` then `hasattr` then `setattr` on the owner (it
    briefly lacks the attribute; fine single-threaded — the Watchdog/DNS threads exist only in `real_*` tests);
  - `from engine.render.asset_sourcer import X` for a table name binds the owner's value at import time (why
    commit 1 can leave `worker/tasks/render.py` alone);
  - the extras `httpx`, `threading`, `socket` forward to the same-named attribute of `safe_fetch` (so `asset_sourcer`
    needs no import of them even during commit 1, and the removal set of §4.1 applies from commit 1);
  - `delattr`/`setattr` of a non-table name behave as on a normal module;
  - `asset_sourcer.__dict__` contains none of the moved names (a leftover copy would shadow `__getattr__`).
  It exists to let the *unmodified* suite drive the *moved* code — the proof that the move lost nothing — and is
  deleted in commit 2.
- **D6 — `asset_sourcer` drops `import httpx`.** No adapter uses an `httpx` attribute (AST-verified); only moved
  code does. About 128 test patch targets mention `httpx` through `asset_sourcer`; they retarget to the bare
  module (`"httpx.get"`, `"httpx.post"` — the same object `patch` patches today).

## 4. Design

### 4.1 What goes where

`safe_fetch.py` (host-agnostic for `open_download`; imports `logging`, `re`, `socket`, `threading`, `time`,
`zlib`, `ipaddress`, `urllib.parse`, `urllib.request`, `contextlib.contextmanager`,
`contextvars.ContextVar`, `httpx`, `SoftTimeLimitExceeded`; no `api`/`engine` imports — the final list is
decided by `ruff F401/F821`):

| Name today | New name | Visibility |
|---|---|---|
| `_HOP_TIMEOUT_GRACE_S`, `_HOST_RE` | same | private |
| `_https_host` | `https_host` | public (used by the URL predicates that stay in `asset_sourcer`) |
| `_host_for_log`, `_any_https_host` | same | private |
| `_http_stream` | same | private (the httpx seam; tests patch it on `safe_fetch`) |
| `_Watchdog`, `_dns_in_time`, `_decode_bounded`, `_content_encoding`, `_api_call` | same | private |
| `_API_MIN_DEADLINE_S`, `_API_DECODABLE_ENCODINGS`, `_API_DROPPED_HEADERS` | same | private |
| `_open_download` | `open_download` | public |
| `_api_get`, `_api_post` | `api_get`, `api_post` | public |
| `_iter_capped` | `iter_capped` | public |
| `_body_too_large`, `_TooLarge` | `body_too_large`, `TooLarge` | public |
| `_MAX_API_JSON_BYTES` | `MAX_API_JSON_BYTES` | public (adapters use it in `body_too_large(resp, …)`; `api_get`/`api_post` apply it by default) |
| `_MAX_REDIRECTS`, `_REDIRECT_STATUSES` | same | private |
| `_monotonic` | `_monotonic` (+ new `now()`) | private + public accessor |
| `RENDER_ASSET_BUDGET_S`, `asset_budget` | same | public (already public) |
| `_asset_deadline_at` | same | private |
| `_clip_to_asset_budget`, `_asset_budget_spent` | `clip_to_asset_budget`, `asset_budget_spent` | public |

`media_policy.py` (module-level imports `hashlib`, `io`, `logging`, `math`, `warnings`, `pathlib.Path`,
`SoftTimeLimitExceeded`; not `os`; Pillow stays imported lazily inside the functions that use it, unchanged — a
module-level `from PIL import …` would be a behavior change; depends on nothing in `safe_fetch` or `asset_sourcer`):

| Name today | New name | Visibility |
|---|---|---|
| `_IMAGE_MAGIC`, `_VIDEO_MAGIC`, `_MP4_BOXES` | same | private |
| `_sniff_ok` | `sniff_ok` | public |
| `_MAX_IMAGE_PIXELS`, `_MAX_FULL_DECODE_PIXELS`, `_MAX_DECODE_BYTES`, `_MAX_DOWNSCALE_DECODE_BYTES`, `_DECODE_BYTES_PER_PIXEL`, `_DEFAULT_DECODE_BYTES_PER_PIXEL`, `_JPEG_FORMATS`, `_MAX_TIFF_TILE_SIDE` | same | private |
| `_bytes_per_pixel`, `_pixel_cap`, `_decode_window`, `_tiff_tiles_ok`, `_is_transient` | same | private |
| `_image_ok` | `image_ok` | public |
| `_downscale_jpeg`, `_beyond_downscale_window`, `_ORIENTATION_TRANSPOSE`, `_downscale_decoded` | same | private |
| `_OversizedImage` | `OversizedImage` | public (the Wikipedia adapter catches it) |
| `_fit_image` | `fit_image` | public |
| `_refusal_marker`, `_refusal_stamp`, `_refusal_recorded` | `refusal_marker`, `refusal_stamp`, `refusal_recorded` | public |
| `_require_media`, `_cached_media_ok` | `require_media`, `cached_media_ok` | public |

Definition order inside each new module follows the original relative order (so the default-argument binding
`_api_call(url_ok=_any_https_host)` and `asset_budget` after `_asset_deadline_at` keep working at import time).
Comment blocks explaining a control move with it; the exact edits are listed in §5 delta 2.

`asset_sourcer.py` after the move keeps: `_PERMISSIVE_LICENSES`, `_strip_html`, the tmp/atomic-write helpers,
the six adapter-owned constants of D1, `_pexels_url_ok`, `_wikimedia_url_ok`, `_NUMERIC_ID_RE`/`_SAFE_ID_RE`/
`_numeric_id`/`_title_id`, `_FHD`/`_IMAGE_EXTS`/`_choose_video_file`/`_license_from_extmetadata`/
`_image_extension`, the four adapters, `SourcedAsset`, the factories, `LocalMusicSource`, `_cache_asset`,
`_generate_gated_hf_asset`, `resolve_beat_assets`, the pins fingerprint functions and `resolve_or_reuse`. It
imports `from engine.render import media_policy, safe_fetch` (modules, D3); `ruff F401` removes the imports only
moved code used (`httpx`, `socket`, `threading`, `zlib`, `ipaddress`, `warnings`, `io`, …); `logging`, `math`,
`os`, `re`, `time`, `urllib.parse`, `uuid`, `hashlib`, `Path` stay if adapters use them. The removal set is "baseline
imports with no staying-code user", stated explicitly here and asserted by the fidelity script (which resolves attribute chains, so `urllib.parse`
stays — staying code calls `urllib.parse.quote`/`unquote` — while `urllib.request` goes): `ipaddress`, `socket`,
`threading`, `zlib`, `warnings`, `io`, `httpx`, `urllib.request`, `contextlib.contextmanager`,
`contextvars.ContextVar`.

Dependency direction: `asset_sourcer → {safe_fetch, media_policy}`; `worker/tasks/render.py → safe_fetch`
(`asset_budget`, `RENDER_ASSET_BUDGET_S`, `now`) and `→ asset_sourcer` (factories, `resolve_or_reuse`,
`compute_pins_fingerprint_for_render`); `engine/publish/gate.py → asset_sourcer` (unchanged); `media_policy` and
`safe_fetch` import nothing from each other or from `asset_sourcer`.

### 4.2 Public surface (22 names)

```
safe_fetch   (13): open_download, api_get, api_post, iter_capped, body_too_large, TooLarge, https_host,
                   MAX_API_JSON_BYTES, now, asset_budget, clip_to_asset_budget, asset_budget_spent,
                   RENDER_ASSET_BUDGET_S
media_policy (9):  sniff_ok, image_ok, fit_image, OversizedImage, require_media, cached_media_ok,
                   refusal_marker, refusal_stamp, refusal_recorded
```

Rule (Q7): a name is public iff code outside its own module calls it. The grilling round listed 15 names
(including `now`); the AST analysis of §6 added six by the same rule (`body_too_large`, `TooLarge`, `https_host`,
`MAX_API_JSON_BYTES`, `OversizedImage`, `refusal_recorded`), and `RENDER_ASSET_BUDGET_S` is public today:
15 + 6 + 1 = 22. Tests may still reach a module's own private internals.

### 4.3 The clock and the budget (Q4, D2)

```python
# safe_fetch.py
_monotonic = time.monotonic      # indirection so tests can drive a fake clock
def now() -> float:
    return _monotonic()
```

`asset_budget`, `clip_to_asset_budget`, `asset_budget_spent` and the fetch internals read `_monotonic` and
`_asset_deadline_at` as `safe_fetch` globals. The `ContextVar` is defined once, in `safe_fetch` (G4). Adapters
call `safe_fetch.now()` where they used `_monotonic()`; `worker/tasks/render.py` uses `safe_fetch.now` where it
used `_monotonic as _asset_clock` (module-attribute form, D3).

### 4.4 URL allowlists (Q8)

`_pexels_url_ok`, `_wikimedia_url_ok` stay in `asset_sourcer` and call `safe_fetch.https_host(...)`. Adapters
keep passing them as `url_ok` to `safe_fetch.open_download(url, url_ok, ...)`.

### 4.5 Guards — `tests/test_module_seams.py` (commit 3) and the shared oracle

`tests/_seam_oracle.py` (commit 1; imported by the bridge test, the guards and the fidelity script) holds:
- `MOVED`: the 58 baseline names of §4.1 → `(owner module, new name, kind)` where kind is `callable`, `constant` or
  `data` (below)
  (so `asset_budget` and `RENDER_ASSET_BUDGET_S` map to themselves); `BRIDGE_EXTRAS = {httpx, threading, socket}`;
- `NEW = {"now": ("safe_fetch", "callable")}`: the one public name with no baseline counterpart (G3 needs its kind,
  G5 its existence);
- `STAYS`: **every** non-moved baseline top-level name (including `_log`, `_STALE_TMP_AGE_S`, `_MUSIC_EXTS`, `_fp`,
  `EMPTY_PINS_FINGERPRINT`, the six D1 constants, the adapters, …) plus the imported names tests legitimately
  reach on `asset_sourcer` (`settings`, `os`, `Path`, `time`, `record_stage`), spelled out, no globs.
The fidelity script derives the moved set from the baseline AST (top-level definitions minus `STAYS` minus
imports), asserts it equals `MOVED`'s keys (58), that `MOVED ∩ STAYS = ∅`, that each owner module defines its
`new name`, and that each **kind is derived, not trusted**, as a function of the *new* name's visibility: `callable` for
`FunctionDef`/`ClassDef` names and `_monotonic`/`now`; `constant` for an `Assign`/`AnnAssign` whose new name is public
(the set must equal exactly `{MAX_API_JSON_BYTES, RENDER_ASSET_BUDGET_S}`); `data` for every other
`Assign`/`AnnAssign` (about 24 private names such as `_HOST_RE`, `_MAX_REDIRECTS`, `_IMAGE_MAGIC`,
`_asset_deadline_at` — never seam-eligible and never referenced outside their module) — so a hand-copy error, a wrongly classified name or a mislabelled
kind shows up mechanically.

`test_module_seams.py` is AST-based, in the style of `test_job_claim.py`'s guard. Each rule has a
synthetic-violation test proving it fires and a positive-control test proving it is not vacuous on the real tree.

- **G1 — no stale or moved-name access through `asset_sourcer` in tests.** Over every `tests/**/*.py` (excluding
  the guard and the oracle): resolve string constants **including those imported across test files** (`from
  tests.test_sourcer_selection import _MOD` is how 8 of the 9 `_MOD` users get it; `_LOG` is defined per file),
  f-strings built from them, `AS.__name__`, and any import form that binds `asset_sourcer`. Flag any
  `Load`/`Store`/`Del` of `AS.<x>` **where `x ∈ MOVED ∪ BRIDGE_EXTRAS`** (`AS.settings`, `AS.os`, `AS.Path`, the adapter
  classes, `AS._atomic_write`, … stay legal), and the second argument of `setattr`/`delattr`/`getattr`/`patch.object`
  **when the first argument resolves to the `asset_sourcer` module object and the name is in `MOVED ∪ BRIDGE_EXTRAS`** (the same names are correct against
  `safe_fetch`/`media_policy`, e.g. `monkeypatch.setattr(media_policy, "_MAX_IMAGE_PIXELS", …)`), and any string
  patch target of the form `engine.render.asset_sourcer.<first>[.…]` whose **first component after that module
  path** is in `MOVED ∪ BRIDGE_EXTRAS`
  (`…asset_sourcer.httpx.get` is flagged through `httpx`; `{_MOD}.time.sleep` and `…settings.*` pass because
  `time`/`settings` are in `STAYS`). A patch target of `safe_fetch.now`, directly or through an alias, is flagged
  (D2). The guard distinguishes `unittest.mock.patch` (bound by import) from `TestClient.patch` /
  `client.patch("/api/…")` (HTTP PATCH, ignored: five sites in `test_cuts_publish_router.py`) and resolves
  `patch(patch_target)`-style variables (the one in `test_cuts_publish_router.py`, a helper parameter at line 325, is
  an explicit allowlist entry: it patches a router, not the sourcer). Non-literal names are resolved through
  `@pytest.mark.parametrize` literals including comma-separated argnames (split on commas, strip spaces; the
  `limit_name` case in `test_sourcer_api_streaming.py`, argnames `"cls,ctype,limit_name"`, resolves to `_MAX_IMAGE_BYTES`/`_MAX_VIDEO_BYTES`, both in `STAYS`); an
  unresolved constant or name in a patch position is a **failure**, allowed only through a short allowlist with a
  justification.
  Positive control: after commit 2 the fidelity script's resolver mode counts, per owning module
  (`asset_sourcer`/`safe_fetch`/`media_policy`) and per form, the resolved targets on the real tree; those figures
  are recorded in §10 and the guard asserts the recorded per-form counts exactly (they are deterministic once commit 2 is in). The positive control is not
  circular: on the **baseline** test tree (`git show main:`) the resolver's per-form totals must equal the counting
  script's independent regex totals (152 / 17 / 6 / 9 / 22 …), checked once and recorded in §10. The real check is
  the synthetic violation fixture for each form (including a two-file fixture for the imported-constant path).
- **G2 — patches are strict.** In every `tests/**/*.py`, no `monkeypatch.setattr`/`patch` targeting
  `asset_sourcer`, `safe_fetch` or `media_policy` passes `raising=False` or `create=True`, and every target
  resolves with `hasattr`. This covers `tests/conftest.py` and `test_sourcer_selection.py`
  (`_wikipedia_downloads_via_get_fakes`, imported by three other files) and any hand-edited retarget.
- **G3 — no captured seam anywhere in production code.** Over `api/`, `engine/`, `worker/`: resolve every name
  bound to `engine.render.safe_fetch` or `engine.render.media_policy` (plain, `import … as sf`, relative `from .
  import safe_fetch`, dotted `engine.render.safe_fetch.x`). No from-import of a name out of either module, no
  star-import; `getattr(safe_fetch, …)`/`vars(...)` forbidden. For a `callable` seam every `ast.Attribute` must be
  the callee of a `Call` or a type in an `except` clause (including a tuple of types); for a `constant` seam a
  Load is allowed only inside a function body as a call argument or operand. Anything else — module/class scope,
  default argument, decorator, right side of an assignment, container, `partial` argument, instance attribute — is
  a violation. Outside `safe_fetch`/`media_policy` themselves, any attribute of either module whose name starts
  with an underscore is a violation (production code uses only the 22 public names).
  Positive controls: the five legitimate constant reads of the final tree pass (the four
  `body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES)` sites and the `render.py` budget expression), one
  synthetic test per forbidden form fails.
- **G4 — one clock, one ContextVar.** `safe_fetch` defines `_asset_deadline_at` and `_monotonic` exactly once; no
  other module under `engine/` or `worker/` defines either name.
- **G5 — direction, completeness, no bridge.** `media_policy` imports nothing from `safe_fetch`/`asset_sourcer`;
  `safe_fetch` imports nothing from `media_policy`/`asset_sourcer`; for every `MOVED` entry the new name exists on
  the owner and the old name is **not in `vars(asset_sourcer)`**; `asset_sourcer` has no module-level
  `__getattr__`/`__setattr__`/`__delattr__` and no `__class__` assignment.
- **G6 — transport and allowlist stay in the adapters' contract** (production code under `api/`, `engine/`,
  `worker/`; the commit-3 probe tests, which call `open_download`/`api_get` with sentinels, are out of scope). (a) `asset_sourcer.py` imports none of `httpx`,
  `socket`, `ssl`, `http.client`, `requests`, `urllib.request` (D6 as a standing check). (b) Every
  `safe_fetch.open_download` call passes `url_ok` (second positional or keyword) as exactly `_pexels_url_ok` or
  `_wikimedia_url_ok`, and those two are `asset_sourcer` functions whose bodies call `safe_fetch.https_host`.
  (c) Every `safe_fetch.api_get`/`api_post` call's first argument is a string literal, a name/attribute that resolves
  to a class- or module-level string constant (`self.<ATTR>` resolves against the enclosing class body), or an
  f-string whose first element is a literal prefix or such a constant (never a parameter or a value parsed from a
  response). (The default `url_ok` of those helpers stays a documented known gap, §2.)

Premise, stated correctly: once the bridge is deleted and `raising=True` is the default, a stale `AS.<moved>`
target raises `AttributeError` by itself, loudly. G1 is belt-and-braces against the forms that can stay silent: a
name that still exists on `asset_sourcer`, `raising=False`, a rename leaving a same-named attribute behind. G2–G3
cover the genuinely silent class: a seam reached through a binding other than the one tests patch.

## 5. Behavior preservation

Preserved: every function body, constant value, exception type and message, log message text and level, cache
and file effect, call order, the `Accept-Encoding: identity` header, redirect handling, deadline and watchdog
behavior.

Deliberate, harmless deltas:
1. **Logger name of moved code** (from commit 2). Records emitted by moved functions (`open_download`'s warnings,
   `fit_image`, `require_media`, `cached_media_ok`) carry `engine.render.safe_fetch` or
   `engine.render.media_policy` instead of `engine.render.asset_sourcer`. Message text unchanged; nothing in the
   repo filters on the old name outside tests. External log monitors keyed on the old name would go quiet: one
   line in the PR notes.
2. **Docstring/comment text.** Comments are not in the AST and are reviewed by hand; **docstrings are compared by
   fidelity check A** after a word-boundary rename of the §4.1 old public names inside the docstring text, the only
   remaining differences being the edits named here (the script lists each by name; they are recorded in §10).
   Beyond the renames these are the genuine edits, all in commit 1 (they are inside moved code or at the seam):
   - `_any_https_host`'s docstring "The hosts are constants in this module (not remote-controlled)" is false in
     `safe_fetch`; reworded (the host set is the caller's). The `api_get`/`api_post` docstrings gain a sentence
     that the default `url_ok` is not an allowlist.
   - The new modules' own docstrings (compared like any other docstring, §6 check A): `safe_fetch` states that
     `open_download` enforces https, hop-by-hop re-checking, caps and deadlines but takes the host allowlist from
     the caller's `url_ok`, and that `api_get`/`api_post` default to "any https host except IP literals and
     localhost"; `media_policy` states that it decides admit/downscale/refuse and never fetches.
   - The three comment blocks that describe the whole hardening move into `safe_fetch` with the mechanism: main
     lines 90–94 (SSRF/size-cap rationale; "https-only to an expected host" is reworded to "to hosts the caller's
     `url_ok` allows"), 124–135 (the three-layer deadline design; "each search() has one overall budget (below)"
     is reworded to say the adapters choose per-search budgets, and "raised in ANY network call of this module is
     re-raised" to name the fetch layer, the adapters keeping their own `except SoftTimeLimitExceeded` clauses),
     and 145–151 (the asset budget; "the per-search budgets above" reworded to "the adapters' per-search
     budgets"). The six D1 constants stay in `asset_sourcer`, each group with a one-line comment pointing at
     `safe_fetch`.
3. **Public identifiers.** Of the 22 public names, 19 are renamed (`_open_download` → `open_download`, …),
   `asset_budget` and `RENDER_ASSET_BUDGET_S` keep their identifier but move module (an import-path break for any
   external importer; only `render.py` and tests today), and `now` is added. The old private names no longer exist on
   `asset_sourcer`, which also no longer has an `httpx` attribute (D6). `TooLarge`/`OversizedImage` lose the
   underscore (their `repr` changes; both are caught by the adapters before any `record_stage`).
4. **`render.py` clock.** `worker/tasks/render.py` reads `safe_fetch._monotonic` (via `now`) rather than a copy
   bound at import, so it follows one patch; production behavior is identical (`time.monotonic` either way).

Risks the move could introduce, each covered by a check (§6, §7): a patch that binds nothing; a second
`ContextVar`/clock; a captured seam; an import cycle; a helper left behind (`_is_transient`, `_content_encoding`,
`_HOST_RE`, `_ORIENTATION_TRANSPOSE`, magic tables); a stdlib module (`threading`, `socket`, `httpx`) reached as
`AS.<mod>` in tests.

## 6. Test strategy and acceptance

### Facts about the existing tests (output of the counting script, `d096524`)
- 27 test files mention `asset_sourcer`; 15 use `AS.<name>`; 9 files use the `_MOD` string constant (defined in
  `test_sourcer_selection.py`, imported by 8).
- `AS.` references 474 (361 private names, 31 `AS.__name__`, 82 others). `monkeypatch.setattr(AS, "<name>", …)`:
  170 (169 literal, 1 name from a variable); `patch.object(AS, …)`: 13, plus 9 on `AS.threading`; so 182
  literal-name calls, of which **129 target moved names**; string-target `monkeypatch.setattr`: 5 (conftest).
- String patch targets: literal `"engine.render.asset_sourcer.<x>"` 17; `f"{_MOD}.<x>"` 152 (`httpx.get` 57,
  `httpx.post` 55, `_http_stream` 24, `time.sleep` 8, `_atomic_write` 5, and one each of
  `settings.huggingface_price_per_video_second`, `record_stage`, `os.replace`); `f"{_LOG}.<x>"` 6;
  `f"{AS.__name__}.<x>"` 9; `caplog.set_level(…, logger=AS.__name__)` 22 (exactly one at INFO: "Downscaled",
  emitted by `fit_image`); one `caplog.at_level(DEBUG, logger=_LOG)` (HF adapter, stays). About 128 patch targets
  reach `httpx` through `asset_sourcer`: 112 `{_MOD}.httpx.get|post`, 7 literal `…asset_sourcer.httpx.*`, 7
  `{AS.__name__}.httpx.post`, 2 `monkeypatch.setattr(AS.httpx, …)`.
- Most-patched moved names (literal `setattr`/`patch.object(AS, …)`): `_MAX_IMAGE_PIXELS` 45, `_monotonic` 24,
  `_MAX_FULL_DECODE_PIXELS` 18, `_http_stream` 14, `_MAX_DECODE_BYTES` 6, `_MAX_API_JSON_BYTES` 4,
  `_MAX_DOWNSCALE_DECODE_BYTES` 4, `_dns_in_time` 3.
- Non-name reaches: `AS.threading` 9, `AS.httpx` 2, `AS.socket` 1 (all become bare `threading`/`httpx`/`socket`:
  the same process-global objects, no coupling to `safe_fetch`'s imports); `AS.settings` 5, `AS.os` 2, `AS.Path` 1
  stay valid (`asset_sourcer` keeps them) and the rewrite leaves them alone. No `AS.time`.
- The direct tuple-assignment form `AS._monotonic, saved = clock, AS._monotonic`
  (`test_sourcer_download_guards.py`) is neither a read nor a `setattr`.
- `worker.tasks.render._asset_clock` is patched in `tests/test_render_task.py` (2 places, with `AS._monotonic`).
- Five switch-offs: conftest autouse fixtures for `_sniff_ok`, `_dns_in_time`, `_api_get`/`_api_post`,
  `_image_ok`, and `_wikipedia_downloads_via_get_fakes` (defined in `test_sourcer_selection.py`, imported by
  three other files; autouse only in files that import it, no opt-out marker). Markers (`real_media_sniffing`,
  `real_dns`, `real_api`, `real_image_check`) are unchanged.
- Non-test importers of `asset_sourcer` names: `worker/tasks/render.py` and `engine/publish/gate.py`; other
  files only mention the module in comments.

### Running the tests
`python -m pytest` (a bare `pytest` from a worktree can import the main checkout). Baseline on `main` before any
edit (run by the author and, independently, by several reviewers): **2760 passed, 3 skipped, 1 deselected** (2763 selected; the
deselected one is the `golden` test).

### Commit 0 — spec
This spec, `CONTEXT.md` (draft terms) and the counting script, added with explicit `git add` of those files only (the
user's local `.gitignore`, `.ignore`, `.mcp.json`, `.claude/`, `graphify-out/` stay out). No code. The baseline run is recorded
in §10.

### Commit 1 — move, with a forwarding bridge; no existing test edited
Adds `tests/_seam_oracle.py` and the fidelity script (`docs/specs/2026-11-safe-fetch-media-policy-fidelity.py`,
parameterized by baseline revision, default `d096524`, F/E9-clean because CI lints it, inserting the repo root
into `sys.path` itself so it can import `tests/_seam_oracle.py` when run as `python docs/specs/…`; a one-time proof, not a
standing guard — §2). Creates the two modules and moves the code (renames per §4.1; adapters call
`safe_fetch.x`/`media_policy.x`; both new modules log via `getLogger("engine.render.asset_sourcer")`, D4; the
comment/docstring edits of §5 delta 2). `asset_sourcer.py` ends with the bridge (D5): a `ModuleType` subclass
assigned to `sys.modules[__name__].__class__` with a literal table `old name → (owner module, new name)` covering
`MOVED ∪ BRIDGE_EXTRAS`. `worker/tasks/render.py` is unchanged in this commit (its
`from … import _monotonic as _asset_clock` resolves through the bridge).
`tests/test_bridge_forwarding.py` (temporary, deleted in commit 2) is driven by the oracle, not by the table:
`set(table) == MOVED ∪ BRIDGE_EXTRAS`; every owner has the new name; `asset_sourcer.__dict__` has none of the moved
names; a name outside the table raises `AttributeError` on read and behaves as on a normal module on
`setattr`/`delattr`; `from engine.render.asset_sourcer import X` binds the owner's value; and for every entry a
read, a `monkeypatch.setattr` with undo, `patch.object` (the `delattr`/`hasattr`/`setattr` exit path),
`patch("engine.render.asset_sourcer.<x>")` and the direct assignment each reach the owner and leave the original
object on the owner afterwards.
Acceptance: the whole existing suite passes with **no existing test edited** (2760 passed + N bridge tests,
3 skipped), `ruff` clean, fidelity checks A and B pass. B's allowed additions in this commit are the bridge class, its table,
the one `sys.modules[__name__].__class__ = …` statement and the imports it needs (`sys`, `types`), which must be
absent after commit 2 (checked by B and F401, and by G5 for the class swap).

### Commit 2 — retarget and drop the bridge
A source-position rewrite (not `ast.unparse`, so comments/formatting survive; driven by the oracle; diff reviewed
by hand) covering: attribute `AS.<x>` Load/Store/Del including the tuple-assignment Store form; the string name in
`setattr`/`delattr`/`patch.object`; string and f-string patch targets; per-file imports of the new modules
(including function-local `from engine.render import asset_sourcer as AS`, and parenthesised multi-line imports),
and **removal of `AS` imports left unused** (F401 applies to function-local imports too: `test_sourcer_sniffing.py`,
`test_sourcer_soft_limit.py`, `test_render_task.py`). Specifics:
- `_MOD` stays for names that stay (`time.sleep`, `_atomic_write`, `settings`, `record_stage`, `os.replace`; a
  `{AS.__name__}.<staying name>` target becomes `{_MOD}.<name>`);
  `_FETCH_MOD = "engine.render.safe_fetch"` is added next to it (one added assignment, in
  `test_sourcer_selection.py`) for `_http_stream` (24 uses). The `from tests.test_sourcer_selection import …`
  statements are edited accordingly: `_FETCH_MOD` is added where used (download_guards, contracts, soft_limit,
  cache_and_chain, watchdog) and `_MOD` is dropped where no use remains (image_check, sniffing, watchdog — whose
  remaining uses are all bare `httpx.*` or `_FETCH_MOD`), like an unused `AS` import (CI lints `tests/` with F401); media names appear only as the conftest string literals, written out with the module path;
  `{_MOD}.httpx.get|post`, `{AS.__name__}.httpx.post`, the literal `…asset_sourcer.httpx.*` and `AS.httpx` become
  `httpx.get|post` / `httpx`.
- Both new modules switch to `getLogger(__name__)`; all 22 `caplog.set_level(…, logger=AS.__name__)` are
  retargeted by hand to the module that emits the asserted record (the one INFO case, "Downscaled", is `fit_image`
  → `media_policy`); the HF DEBUG case stays on `asset_sourcer`. The list goes in the PR.
- `tests/test_render_task.py`: delete the two `worker.tasks.render._asset_clock` patches, keep the
  `safe_fetch._monotonic` ones; the expected deadlines must be shown unchanged.
- Conftest patches the owning modules, flipping its five `monkeypatch.setattr(..., raising=False)` calls to the
  default (`media_policy.sniff_ok`, `media_policy.image_ok`, `safe_fetch._dns_in_time`,
  `safe_fetch.api_get|api_post`); `_wikipedia_downloads_via_get_fakes` already uses strict `patch(...)` and is only
  retargeted to `safe_fetch._http_stream`. `pyproject.toml` marker descriptions, conftest fixture docstrings, that
  fixture's docstring and the test docstrings that say "HTTP is faked at `engine.render.asset_sourcer.httpx`"
  follow.
- `worker/tasks/render.py` imports `from engine.render import safe_fetch` and uses `safe_fetch.asset_budget`,
  `safe_fetch.RENDER_ASSET_BUDGET_S`, `safe_fetch.now`.
- The bridge and `tests/test_bridge_forwarding.py` are deleted.
Allowed test edits (exactly the transform of fidelity check C): target names, whitelisted imports (including
the name list of `from tests.test_sourcer_selection import …`), logger arguments, docstring wording, the `raising` flip, the one `_FETCH_MOD` assignment, the two `_asset_clock` patch
deletions, unused-`AS` removal. **No assertion changes, no test deleted (other than the temporary bridge test),
none added.**
Acceptance: same 2760 passed / 3 skipped; ruff clean; fidelity checks A, B (now also all of `worker/tasks/render.py`)
and C pass; the control-wiring mutants are re-run on the retargeted suite.

### Commit 3 — guards, behavior probes, docs
`tests/test_module_seams.py`: guards G1–G6 (§4.5) plus behavior tests defined at the consumer, not on the attribute:
- **Seam probes (belt-and-braces; the existing fake-clock, `real_api` and stub-dependent tests already fail on a
  stale binding).** Patch a sentinel on the owning module and assert each consumer observes it: `media_policy.
  image_ok` via `fit_image`, `cached_media_ok` and the HF image adapter; `sniff_ok` via `require_media`,
  `cached_media_ok` and both HF adapters; `safe_fetch._monotonic` via `now()`, `open_download`, `asset_budget` and
  both search budgets; `_dns_in_time` via `open_download` (called with a `deadline_at`, since the gate only runs
  with one); `api_get`/`api_post` via the adapters; `iter_capped` via `api_call` and the download adapters;
  `_http_stream` via `open_download`/`api_call`. Seams with no probe (`asset_budget_spent`, `clip_to_asset_budget`,
  `body_too_large`, `https_host`, `TooLarge`, `OversizedImage`, `refusal_*`) are covered by the existing behavior
  tests in `test_sourcer_render_budget.py` and the sourcer files, by G3 and by the control mutants.
- **Fixture effectiveness at the consumer.** Unmarked: `require_media("image", b"junk")` does not raise and
  `fit_image`/`cached_media_ok` accept a placeholder image; `open_download` performs no real DNS lookup (a non-IP
  hostname, a `deadline_at`, proxy variables cleared with `monkeypatch.delenv` as `test_sourcer_dns.py` does, a
  sentinel resolver); an unmarked `api_get`/`api_post` call reaches `httpx.get`/`httpx.post`. Each `real_*` marker:
  the same call does raise / does look up / does stream (with `_http_stream` faked where needed). The Wikipedia
  download fixture has no opt-out and cannot be imported into this file without turning it on for every test
  there; it stays covered by the existing Wikipedia adapter tests.
- **`now()`**: patch `_monotonic`; `now()` follows it (a `time.time()` or bound-copy mutant fails).
- **Logger names**: a known WARNING from `open_download` has `record.name == "engine.render.safe_fetch"`; one from
  `fit_image`/`require_media` has `"engine.render.media_policy"`.
Then the docs of §8. Acceptance: 2760 + N guard/probe tests passing, 3 skipped, ruff clean; every guard mutant red.
Also the extended resolver mode of the fidelity script is run and its per-form figures are recorded in §10.

### Fidelity checks (one script, run at each commit; output recorded in §10)
- **A — moved definitions.** Parse each moved top-level `FunctionDef`, `ClassDef`, `Assign`, `AnnAssign` on the
  baseline and in the new module. Apply the §4.1 rename table to `FunctionDef.name`, `ClassDef.name`, `Name.id`
  and `Attribute.attr` of the old tree and require `ast.dump` equality (comments are not in the AST; **docstrings
  are compared** after a word-boundary rename of the old public names inside the docstring text, the only allowed
  differences being the §5 delta 2 edits, each named in the script) and the same relative definition order. Each new module's top-level body is exactly: one leading
  docstring, the imports, `_log` immediately after them (literal name in commit 1, `__name__` from commit 2),
  the moved nodes in baseline order and, in `safe_fetch`, `now` immediately after `_monotonic` — nothing else.
  Each import is compared with the baseline import statement it came from (module and name), and there is no
  module-level `PIL` import.
- **B — staying definitions, constants and call sites.** For `asset_sourcer.py` and, after commit 2, all of
  `worker/tasks/render.py`: the multiset of top-level node dumps of the **inverse-transformed baseline** equals the
  multiset of the new file's, minus the planned import changes (removals = the explicit list of §4.1, which the script also derives as the baseline imports with no
  staying-code user, resolving attribute chains; additions = `from engine.render import …`, and in commit 1 the bridge class, its table, the `__class__` assignment,
  `sys`, `types`). The inverse
  transform: `Name(x)` for a moved `x` → `Attribute(Name(<module>), <new name>)`; `_monotonic()` →
  `safe_fetch.now()`; for `render.py` also `_asset_clock()` → `safe_fetch.now()` and `asset_budget`/
  `RENDER_ASSET_BUDGET_S` → `safe_fetch.<name>`; arguments untouched. This covers the six D1 constants and every
  adapter call site. **Precondition**, checked over the compared definition bodies only (not import statements):
  no binder (assignment target, argument, keyword, `ExceptHandler.name`) is named `safe_fetch` or `media_policy`,
  and none is named like an old moved name. Locals named `now` are fine (the transform emits `safe_fetch.now()`,
  never a bare `now`; the baseline has three).
- **C — tests (a deterministic transform, no exclusion zones).** Apply a rewrite function (one expected-form rule
  per shape, derived from the oracle: attribute, `setattr`/`patch.object` name string, plain string target,
  `{_MOD}`/`{_LOG}`/`{AS.__name__}` f-string target → `{_MOD}`, `{_FETCH_MOD}` or a bare constant, `AS.httpx|
  threading|socket` → bare name) to every baseline test tree and require `ast.dump` equality with the retargeted
  tree. The only residual differences are listed exhaustively: imports whose module is one of
  `asset_sourcer`/`safe_fetch`/`media_policy`/`engine.render` or the three bare stdlib names, and the name list of
  `from tests.test_sourcer_selection import …` (`_FETCH_MOD` added, `_MOD` dropped when unused, names only); `logger=` keyword
  values that are one of the three module `__name__`s; the `raising` flag in conftest; one added `_FETCH_MOD`
  assignment; the two `worker.tasks.render._asset_clock` with-items (matched by shape); a changed docstring
  (`body[0]` `Expr(Constant)`) only; removed unused `AS` imports. A swapped `httpx.get`↔`httpx.post` or a changed
  asserted value therefore fails. Also: the sorted pytest node-id set before and after commit 2 is identical,
  minus the bridge test; and per file the count of `assert` nodes and of `pytest.raises`/`approx` calls is
  unchanged.
- **Dependency check.** `safe_fetch`/`media_policy` reference no name defined in `asset_sourcer`; every public
  name has at least one outside caller and no private name has one.
After a rebase, the script is re-run with `--baseline <current main>`; any movement of `asset_sourcer.py`,
`worker/tasks/render.py` or the tests on `main` is a blocker until reviewed (a concurrent security fix must not be
dropped in conflict resolution). Any difference is explained in §10 or the move is wrong.

### Mutations (apply, the named check/test must fail, revert; results recorded in §10)
Mutants are recorded in §10 by id: `B1…` (bridge/oracle), `Gm1…` (guards), `F1…` (fidelity), `C1…C24`
(control wiring), each in the order listed.
- **Bridge and oracle mutants (commit 1):** drop one table entry; `__setattr__` writes locally instead of on the
  owner; `__delattr__` does not forward; `__getattr__` raises `KeyError`; a non-table name is forwarded; a moved
  name left in `asset_sourcer.__dict__`; one oracle entry with the wrong owner; one name moved between `MOVED`
  and `STAYS`; one oracle kind flipped (`callable` ↔ `constant`).
- **Guard mutants:** a conftest patch left on the old module (G1); `raising=False` and `create=True` against a new
  module (G2); an adapter using a from-imported `open_download` (G3), one with a module-level alias, one with an
  instance attribute `self._open = safe_fetch.open_download`, one with `functools.partial(safe_fetch.x)`, one
  with a default argument `=safe_fetch.now`, one with `getattr(safe_fetch, "…")`, one using an underscore
  attribute (`safe_fetch._http_stream`), and a star-import (G3); a constant read at module scope (G3); a second
  `_asset_deadline_at` in `asset_sourcer` (G4); `media_policy` importing `safe_fetch`, and `safe_fetch` importing
  `media_policy` (G5); the bridge left behind (G5); one test patching the old module through an imported `_MOD`
  f-string, and one patching `safe_fetch.now` through an alias (G1); `import httpx` reintroduced into
  `asset_sourcer`, an `open_download` call with a lambda or local-function `url_ok`, an `api_get` call with a
  variable URL (G6); a callable labelled `constant` in the oracle, then `functools.partial(safe_fetch.open_download)`
  (G3); a wrong `getLogger("x")` in each new module (logger-name tests).
- **Fidelity mutants (each must fail A, B or C):** drop `limit=` in an HF call; drop `url_ok` or `deadline_at`;
  swap `_pexels_url_ok`/`_wikimedia_url_ok` between adapters; change `_MAX_VIDEO_BYTES`; flip a comparison inside a
  moved function; change a moved docstring outside the §5 list; add a stray top-level statement to a new module and
  to `asset_sourcer`; change one import's source module; change one asserted value in a test; delete one test
  assertion; swap `httpx.get` for `httpx.post` in one retargeted string.
- **Control-wiring mutants** (numbered so §10 can record each; each must turn an existing test red): (1) adapter
  `url_ok=lambda u: True`; (2) HF `api_post` without `limit=`; (3) HF `generate()` skips `asset_budget_spent()`
  (image and video); (4) `search()` skips `clip_to_asset_budget` (Pexels and Wikipedia); (5) `Accept-Encoding:
  identity` removed; (6) `_tiff_tiles_ok` dropped from `image_ok`, and (7) from `downscale_decoded`; (8)
  `refusal_recorded` always `False`; (9) the `fired` check after `open_download`'s `yield` removed (both sites);
  (10) the watchdog `dup()` replaced by the original socket; (11) an adapter's `safe_fetch.now()` replaced by
  `time.monotonic()` (each of the four sites); (12) `require_media` removed (Pexels, Wikipedia); (13) `fit_image`
  removed; (14) the `OversizedImage` marker write removed; (15) `cached_media_ok` removed or forced `True`; (16)
  `iter_capped` removed from a download path; (17) `body_too_large` skipped; (18) per-hop `url_ok` re-check
  skipped after hop 0; (19) `follow_redirects` flipped; (20) each `except SoftTimeLimitExceeded` re-raise removed
  (the clauses now live in three modules); (21) `refusal_marker` URL keying dropped, and the direct HF `sniff_ok`
  (2 sites) / `image_ok` (1 site) calls removed, `api_get` replaced by `httpx.get`, the DNS gate removed; (22) the
  conftest `sniff_ok` stub flipped to `False`; (23) boundary mutants that exist only after the split: an adapter
  calling a different function of the right module (e.g. `safe_fetch.api_get` where `open_download` is required),
  an adapter bypassing `safe_fetch.open_download`, `render.py` using `time.monotonic()`; (24) one body mutation per
  new module (flip the cap comparison in `iter_capped`; change the `pixel_cap` division), each noting whether it is
  equivalent. Mutants 1–21 were confirmed killed on the baseline by the existing suite (experiments by three
  reviewers); mutant 17 is killed only through the conftest-stubbed non-streaming `api_get`/`api_post` path (it
  would be equivalent under `real_api`), and one baseline mutant (`_Watchdog.trace` using `time.monotonic`) is
  equivalent in production. The post-move re-run after commit 2 is the acceptance evidence for 1–23.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| A patch targets the wrong module and binds nothing / the real path runs | Bridge in commit 1 (the suite drives moved code with the same patches); `raising=True` + G1/G2 in the final tree; seam probes and consumer-level fixture tests |
| A seam captured by a from-import, alias or default argument | D3; G3; seam probes |
| Two clocks or two `ContextVar`s; patching `now` | D2; G4; G1; `now()` test |
| Logger names | D4 (commit 1 keeps the old name, commit 2 switches), hand retarget of all 22 uses, logger-name tests |
| Missed helper/constant left in `asset_sourcer` | Dependency check; `ruff F821`; reference analysis |
| Import cycle | Direction in §4.1; G5 |
| Retarget rewrites a non-seam use | Closed table; stay-names in the oracle; diff reviewed by hand |
| Control wiring lost in the ~60 rewritten adapter call sites, or an edited cap | Fidelity check B (incl. constants and the top-level multiset); control-wiring and fidelity mutants |
| The commit-2 test rewrite weakens an assertion or retargets to the wrong owner | Fidelity check C (test-AST equality, node ids, assert/raises/approx counts); post-move re-run of the control mutants |
| Bridge hides a missing table entry | Oracle-driven bridge test; `__dict__` check; `AttributeError` for non-table names |
| `asset_sourcer.py`, `render.py` or the tests change on `main` while this is open | Branch off current `main`; after any rebase re-run the fidelity script with `--baseline <current main>`; baseline movement in those files blocks the PR until reviewed |
| Docs drift | §8 enumerates |

## 8. Documentation changes (part of the PR)

Mention counts are not stated here; the commit-3 task greps each file for the moved names (`_open_download`,
`_iter_capped`, `_api_call`, `_api_get`, `_api_post`, `_http_stream`, `_image_ok`, `_sniff_ok`, `_fit_image`,
`_cached_media_ok`, `_dns_in_time`, `_monotonic`, `asset_budget`, `_asset_clock`, …) and fixes every hit.

Which commit carries what: comment/docstring edits inside moved code → commit 1 (§5 delta 2); everything in
tests, conftest, `pyproject.toml` markers → commit 2; the rest of this section → commit 3.

- `CLAUDE.md`: new module-layout entries for `safe_fetch.py` and `media_policy.py` (ownership, the 22-name
  surface, D1–D6); the `asset_sourcer.py` entry trimmed to what it keeps; the `worker/tasks/render.py` entry (the
  clock); **the Runtime-requirements "Dependency caps" bullet** (it says `asset_sourcer.py` leans on
  `httpx.Client.stream` and httpcore's `connection.connect_tcp.complete` trace event: now `safe_fetch.py`); the
  Key-conventions bullet "Sourcer downloads are allowlisted, …" rewritten with the new names and a rename map
  (not just a pointer); test-list entries for the sourcer test files that name seams (with or without an `AS.`
  prefix) and the conftest wiring; the totals (tests, files) and a `test_module_seams.py` and `tests/_seam_oracle.py`
  entries; a Build-phase status entry (Phase 8g) in the established shape.
- `docs/architecture.md`: new rows for `safe_fetch.py` and `media_policy.py` in the `engine/render/` module table
  (precedent: `names.py`), the `asset_sourcer.py` row, the `tasks/render.py` row, the Downloads bullet, the totals
  and `test_module_seams.py` / `_seam_oracle.py` rows in the test table.
- `README.md`: the test count line.
- `pyproject.toml`: every moved name in the four marker descriptions (`engine.render.asset_sourcer._image_ok`,
  `_api_get`/`_api_post`, `_dns_in_time`, `_sniff_ok`, and also `_MAX_IMAGE_PIXELS`, `_http_stream`) — commit 2.
- `CONTEXT.md`: remove the "agreed targets / code still lives" wording and the "(target module)" suffix; align
  "image check" to "media policy" in the section's lead line; **Fetch budget** says the mechanism (hop/read
  deadlines, watchdog, asset budget) lives in `safe_fetch` while the per-download and per-search limits are
  chosen in the adapters and cross the boundary as arguments (`url_ok`, `deadline_at`, the byte limit, timeouts),
  with `now`, `clip_to_asset_budget` and `asset_budget_spent` supplied by `safe_fetch`; **Media policy** gains the
  Q5 sentence (the marker's name, stamp and check are in `media_policy`, the write stays in the Wikipedia
  adapter); add **Adapter** and **Seam** entries.
- Older specs and `docs/roadmap.md` that cite the old names are historical records and are left unchanged;
  `docs/roadmap.md` open items are unchanged.
- The PR notes carry the one-line logger-name change (§5 delta 1) and the hand-review lists (22 loggers,
  `httpx`/`threading`/`socket` retargets).
- This spec's §10 filled in at the end.

## 9. Open questions

None blocking. For reviewers and the user to challenge: D1 (adapter-owned constants stay), D2 (`now()`, one patch
point), D3 (module-attribute access), D4 (logger names, temporary in commit 1), D5 (forwarding bridge replaces the
plain re-export of Q10), D6 (`asset_sourcer` drops `httpx`, tests patch the bare module). Left as-is on purpose:
`body_too_large`/`TooLarge` public, and the `api_get`/`api_post` `url_ok` default (both listed in §2's known gaps).

## 10. Implementation record

Baseline run (the author and several reviewers): 2760 passed, 3 skipped, 1 deselected.

Counting-script output on the baseline (`d096524`), abridged (the script also prints the most-patched moved names
and the `{_MOD}` target breakdown, both reproduced in §6), by `python3
docs/specs/2026-11-safe-fetch-media-policy-counts.py`:

```
test files mentioning asset_sourcer: 27      test files using AS.<name>: 15      test files using _MOD: 9
AS. references: 474    AS._ private references: 392 (361 + 31 AS.__name__)    monkeypatch.setattr(AS, ...): 170
patch.object(AS, ...): 13    patch.object(AS.<mod>, ...): 9    string-target monkeypatch.setattr: 5
literal 'engine.render.asset_sourcer.<x>' strings: 17    f'{_MOD}.<x>': 152    f'{_LOG}.<x>': 6
f'{AS.__name__}.<x>': 9    caplog.set_level(logger=AS.__name__): 22    caplog.at_level(logger=_LOG): 1
AS.httpx 2   AS.threading 9   AS.socket 1   AS.settings 5   AS.os 2   AS.Path 1   AS.time 0   _asset_clock 2
setattr/patch.object(AS, '<literal>') calls: 182; targeting moved names: 129
```

### Commits and runs (`.venv/bin/python -m pytest`, repo root; baseline `main` = `d096524`)

| Commit | Hash | Full default run |
|---|---|---|
| 0 spec, plan | `bd26ee9`, `37393ce` | baseline 2760 passed, 3 skipped, 1 deselected |
| 1 move behind the bridge, no existing test edited | `333458b` | 3133 passed = 2760 + 373 bridge tests, 3 skipped |
| 2 retarget, bridge deleted | `401b376` | 2760 passed, 3 skipped, 1 deselected |
| 3 guards, probes, docs | `e9968b2` | 3072 passed = 2760 + 312 (`tests/test_module_seams.py`: 308 at `e9968b2`, +3 G1 alias cases in review round 1 (`1b4ca75`), +1 watchdog-clock pin in round 2), 3 skipped, 1 deselected; 3075 selected (3076 collected) |

Test files: 77 `test_*.py` become 78 (`test_module_seams.py`); `tests/_seam_oracle.py` and `tests/_seam_ast.py` are helper
modules, not test files, and `conftest.py` is counted as before (outside the figure).

### Fidelity script (`python docs/specs/2026-11-safe-fetch-media-policy-fidelity.py --baseline d096524 --stage N`)

Checks printed OK at each stage: **stage 1: 17**, **stage 2: 22**, **stage 3: 25** (with `--cross-check-baseline`;
24 without, the cross-check is skipped). The stage-3 run, abridged:

```
OK ORACLE  baseline defines each top-level name once; MOVED keys == baseline definitions minus STAYS; MOVED and STAYS
           disjoint; 58 moved names, 29 per owner; kinds derived (36 callable, 2 constant, 20 data); NEW and BRIDGE_EXTRAS;
           every STAYS name exists in the baseline (49); 22 public names (13 + 9), new names unique; owners define them
OK A safe_fetch     29 moved nodes; docstring edits allowed: _any_https_host, _api_get, _api_post (commit 3 time; `_pixel_cap`/`_image_ok` added in review round 2)
OK A media_policy   29 moved nodes
OK B asset_sourcer.py  44 staying nodes equal the inverse-transformed baseline; 10 planned import removals == the baseline
                       imports with no staying-code user; imports are the baseline's minus removals plus additions
OK B worker/tasks/render.py  3 nodes equal the inverse-transformed baseline; imports as planned
OK DEP safe_fetch and media_policy reference nothing from asset_sourcer or from each other; every public name has an
       outside caller (22); no private name has one
OK C   78 baseline test files == their deterministic rewrite (loggers: asset_sourcer 8, media_policy 8, safe_fetch 6;
       asserts 3072, raises 270, approx 35, test functions 1771); sorted pytest node ids identical (2763)
OK G1 resolver (81 test files): no stale access, nothing unresolved, safe_fetch.now never patched; figures equal
       G1_EXPECTED_COUNTS; on the baseline tree the resolver equals the counting script's regex totals
```

Allowed docstring edits (the `EDIT_ALLOWLIST`, each compared with its docstring removed): `_any_https_host` (the
"hosts are constants in this module" sentence, false in `safe_fetch`, now says the host set is the caller's),
`_api_get` and `_api_post` (a sentence that the default `url_ok` is not an allowlist), `_pixel_cap` and `_image_ok` (the
inherited "25 Mpx for WebP" statements, corrected to the 20 Mpx the code gives; added in review round 2), `<module:safe_fetch>` and
`<module:media_policy>` (new module docstrings). Comment blocks (not in the AST, reviewed by hand): baseline lines
90-94 -> `safe_fetch` lines 37-42 ("https-only to an expected host" -> "to hosts the caller's `url_ok` allows");
124-135 -> lines 47-59 ("each search() has one overall budget (below)" -> "the adapters choose their own per-search
budgets (and pass the resulting deadline in)"; "ANY network call of this module" -> "of this fetch layer", with a
sentence that the adapters keep their own `except SoftTimeLimitExceeded` clauses); 145-151 -> lines 67-73 ("the per-search
budgets above" -> "the adapters' per-search budgets"). The six D1 constants stay in `asset_sourcer.py` (lines 80-93) in three
groups (byte limits, per-download deadlines, per-search budgets), each with a one-line pointer comment to `safe_fetch`.

### G1 positive control: per-form figures (resolver on the retargeted tree, asserted exactly by `test_module_seams.py`)

| Owning module | Total | Forms |
|---|---|---|
| `asset_sourcer` | 84 | `f{_LOG}`=6, `f{_MOD}`=18, literal=5, `object:literal`=53, `object:name`=2 |
| `media_policy` | 82 | literal=2, `object:literal`=80 |
| `safe_fetch` | 76 | `f{_FETCH_MOD}`=24, literal=3, `object:literal`=49 |
| all | 242 | |

Cross-check on the BASELINE test tree: the resolver gives 368 = `f{AS.__name__}`=9, `f{_LOG}`=6, `f{_MOD}`=152,
literal=17, `object:literal`=182, `object:name`=2, which equals the counting script's independent regex totals (152 / 17 / 6
/ 9 / 182; the two `object:name` are the parametrized `limit_name` case, not in the script's regex).

### The 22 `caplog.set_level(..., logger=AS.__name__)` uses (retargeted by hand in commit 2, hand-review list for the PR)

| Now | Count | Where |
|---|---|---|
| `safe_fetch.__name__` | 6 | `test_sourcer_dns.py` (1), `test_sourcer_download_guards.py` (4), `test_sourcer_watchdog.py` (1): the records of `open_download` |
| `media_policy.__name__` | 8 | `test_sourcer_image_check.py` (4, incl. the one INFO "Downscaled" test of `fit_image`), `test_sourcer_sniffing.py` (4): `fit_image`, `require_media`, `cached_media_ok` |
| `AS.__name__` (stays) | 8 | `test_sourcer_api_streaming.py` (1), `test_sourcer_download_guards.py` (1), `test_sourcer_render_budget.py` (2), `test_sourcer_api_limits.py` (1), `test_sourcer_image_check.py` (1), `test_sourcer_sniffing.py` (2): records emitted by the adapters themselves (the HuggingFace "too large" / "asset budget" / "not a valid image|video" / "not a readable image" warnings, the "malformed Pexels" hit) |

The one `caplog.at_level(DEBUG, logger=_LOG)` (HF adapter) stays on `asset_sourcer`. Logger-name tests in
`test_module_seams.py` pin `engine.render.safe_fetch` for `open_download` and `engine.render.media_policy` for
`fit_image` / `require_media`, and that each new module uses `getLogger(__name__)`.

### Mutations (apply, the named check or test must fail, revert)

- **B1-B8 (bridge and oracle, commit 1):** all eight failed `tests/test_bridge_forwarding.py`.
- **C1-C24 (control wiring):** a first set of representatives (C11, one of each of C5, C9, C10, C12-C16) was killed by an existing test
  against the bridge tree in commit 1; the full set was re-run on the retargeted tree after commit 2: 53 variants across
  the 24 mutants (the per-site variants of 4, 12, 20 and 21 included), every one turned at least one existing test red.
- **F1-F20 (fidelity):** each failed the expected check (A, B or C) of the script.
- **Gm1-Gm8 (guards) and Pm1-Pm8 (probes), commit 3:** every one turned a named `test_module_seams.py` test red (the subset recorded here; the longer lists of §6 were
  run as part of the same batteries); the
  oracle kind flip (`callable` <-> `constant`) and the wrong `getLogger("x")` in each new module are among them.

### Deviations from the spec text (none changes the design)

- `tests/test_sourcer_sniffing.py` keeps its `from engine.render import asset_sourcer as AS` import: its HuggingFace-adapter record
  tests still use `AS.__name__` (they were not part of the "remove unused `AS` import" set).
- `tests/_seam_ast.py` is an added helper file (spec §4.5 names only `_seam_oracle.py` and `test_module_seams.py`): the AST checkers
  live there so the fidelity script's resolver mode can load them by path with a bare `python3` (standard library only), and the
  guard file stays readable. Fidelity stage 3 allows it as a new test-tree file.
- G1, G2, G3, G5 and G6 are stricter or more precise than the spec text, each extra rule with its own synthetic-violation or
  positive-control test. Examples: G2 also rejects positional `raising` / `create` flags; G3 additionally pins that the public
  surface is exactly the 22 names and that the oracle's kinds match the real objects (a callable labelled `constant` is caught); G5
  distinguishes a module-level `__getattr__` from a method of that name inside a class and pins `MOVED` disjoint from `STAYS`;
  G6b flags a valid predicate name used outside `asset_sourcer`, G6c resolves `self.<ATTR>` against the enclosing class only.
- Spec review history is in §11; code review of the implementation: see the pull request.

## 11. Spec-review history

**Round 1** (four personas, all NOT CLEAN), changes in revision 2:
- All four: the plain re-export bridge cannot keep the suite green → D5 forwarding bridge.
- Retarget inventory undercounted (f-string `{_MOD}` 152, `{AS.__name__}` 9, `{_LOG}` 6, ~128 `httpx` targets,
  `AS.threading`/`socket`, tuple assignment, `worker.tasks.render._asset_clock`) → §6 facts by script, commit-2 forms.
- Guard blind spots; premise overstated → G1–G5 rewritten, seam probes, fixture tests.
- Fidelity check covered only moved code → check B, order rule, committed script. Mutations tested guards, not
  controls → control-wiring mutants. Docs list incomplete → §8. Smaller items: 22-name arithmetic, import lists,
  known-gaps block, `now()` and logger-name tests, historical-spec note.

**Round 2** (four personas, all NOT CLEAN: no HIGH from security/correctness/docs, one HIGH from test-quality that
is the same finding as the correctness MEDIUM), changes in revision 3:
- **Commit 1 cannot pass unmodified** (`test_wikipedia_a_downscale_is_logged` sets INFO on `AS.__name__`) → D4:
  new modules log as `engine.render.asset_sourcer` in commit 1, `__name__` from commit 2.
- Bridge test circular → oracle-driven, all four patch protocols, `__dict__` and `AttributeError` checks, bridge
  requirements written into D5; G5 rejects a leftover module `__getattr__`/`__setattr__`/class swap.
- Fidelity: constants and import set in B, exact top-level body in A, `render_cut` transform table
  (`_asset_clock`), B for `render_cut` only after commit 2, parametrized baseline, F/E9-clean, one-time-proof note;
  mutants that exercise A and B themselves.
- Two patch points for one clock → D2 + G1 rejects patching `now`.
- G1 could not resolve imported `_MOD` and used "terminal name" → cross-file constant resolution, "first component
  after the module", parametrize literals, minimum-resolved-targets control. G2 → all of `tests/`, `create=True`.
  G3 → any from-import. Fixture-effectiveness at the consumer; Wikipedia fixture dropped from it.
- Docs: `_api_call` has no host-constants docstring (removed), the 90–95 / 123–135 comment blocks decided,
  `api_get`/`api_post` docstring sentence, CLAUDE.md dependency-caps bullet, architecture.md module rows, count
  claims replaced by a grep instruction, commit-1 count "+ N bridge tests", `raising` flip and unused-`AS` removal
  in commit-2 allowed edits, wrong facts (`AS.os`, `AS.Path`, 9 `_MOD` files, 361/31, D1 `deadline_at`, `MAX_API_JSON_BYTES`
  usage), `logging` import, terminology wording.

**Round 3** (four personas, all NOT CLEAN; no HIGH), changes in revision 4:
- Fidelity check B's precondition failed on the baseline's three local `now` variables → scoped to the names the
  transform rewrites, `now` locals explicitly fine (also the shadowing note in D2).
- Fidelity script landed in commit 3 but gated commits 1–2; bridge class/imports unplanned in B → script and
  `tests/_seam_oracle.py` move to commit 1; B allows the bridge in commit 1 only; commit 0 carries the spec.
- Fidelity scope holes: stray `asset_sourcer` statements, import provenance, Pillow lazy-local, constants → B
  compares the top-level multiset, A pins imports and rejects module-level `PIL`, constants covered.
- No mechanical proof of the commit-2 test rewrite → check C (test-AST equality outside an allow-set, node-id set
  equality, assert/raises/approx counts per file).
- "Oracle" meant two sets, hand-copied three times → `tests/_seam_oracle.py` with `MOVED`/`BRIDGE_EXTRAS`/`STAYS`
  exact literals; fidelity script cross-checks them against the baseline.
- G1 positive control undefined ("about 150") → per-form minima recorded by the counting script, unresolved
  constants fail, synthetic two-file fixture. G2 → also `asset_sourcer`. G3 → any seam attribute that is not a
  callee/except-type (covers partial, containers, instance attributes, aliases, relative imports). New G6.
- Counts: `patch.object(AS, …)` is 13 (+9 on `AS.threading`), 129 of 182 literal calls; `_MOD` files 9; counting
  script written and its output recorded (§10). Mutations: explicit numbered list, pre-move kills attributed,
  security extras added, boundary mutants, post-move re-run required. Bridge test gaps (import binding, non-table
  `setattr`). Fixture-effectiveness environment notes. `AS.threading`/`socket` → bare names. `_MEDIA_MOD` dropped.
- Docs: comment blocks 90–94, 124–135, 145–151 placed and reworded, docstring edits assigned to commits, public
  identifier wording (19 renamed, 2 moved, 1 new), CONTEXT.md sentence corrected, Adapter definition, wording.

**Round 4** (four personas, all NOT CLEAN; one HIGH — G3 failing on the final tree — reported independently by all
four), changes in revision 5:
- G3/D3 contradicted the prescribed code (`safe_fetch.MAX_API_JSON_BYTES` as an argument, `RENDER_ASSET_BUDGET_S` as
  an operand) → seams are `callable` or `constant` per name in the oracle, constants readable at call time; also
  `getattr`/`vars`, dotted access, tuple `except`, no underscore attributes outside the two modules; positive
  controls for the five legitimate reads.
- Check C was categories, not predicates → a deterministic rewrite function, exhaustive residual list, exact
  string equality (a `httpx.get`↔`post` swap fails), logger values restricted to three, deletions matched by shape.
- Check B precondition tripped on legitimate import binders → scoped to compared bodies; B compares transformed
  baseline vs new multisets, removals computed and named (`urllib.request`, `contextmanager`, `ContextVar`, …),
  whole `render.py` after commit 2.
- Oracle: `STAYS` is now every non-moved name; `MOVED` derived from the baseline AST, disjointness and owner
  checks; kinds recorded. D4/A agree (`_log` right after imports, `now` after `_monotonic`). G5 uses `vars()`.
- G1 positive control now produced after commit 2 by the script's resolver mode, per owning module; `mock.patch`
  vs `TestClient.patch`, tuple-unpacked parametrize, unresolved = failure. New G6 (no transport imports in
  `asset_sourcer`, `url_ok` exactly the two named predicates, API URLs constant-rooted). Docstrings now compared
  by check A; the two new module docstrings specified; rebase re-run rule.
- Mutants for the bridge/oracle, G2 `create=True`, G5 both directions, alias patch of `now`, G6, HF direct calls,
  DNS gate; mutant 23 given a referent; pre-move kill attribution corrected (1–21, three reviewers), equivalent
  mutants noted. Commit 3 acceptance line; commit 0 file list; consumer-level probes for the `api_get` stub and the
  DNS `deadline_at` precondition; wording (Q10 baseline, bridge lifetime, Wikipedia fixture already strict,
  `urllib.parse` vs `urllib.request`, "no code that depends on a source", httpx breakdown, tie in most-patched).

**Round 5** (four personas, all NOT CLEAN; no HIGH), changes in revision 6:
- Test-import plumbing (`_MOD`/`_FETCH_MOD` names imported from `tests.test_sourcer_selection`, F401 on unused
  `_MOD`) missing from commit 2 and check C → added to both, with the files named; `{AS.__name__}.<staying>` rule.
- §5 delta 2 said docstrings were ignored while check A compares them → delta 2 and A agree: word-boundary
  rename inside docstring text, named exceptions only.
- Commit-1 bridge vs check B's removal set → the extras forward to `safe_fetch`'s attributes, so no import is kept.
- G1 scope (first argument must resolve to the `asset_sourcer` module; module path = `engine.render.asset_sourcer`),
  `NEW` oracle entry for `now`, G6(c) tightened, `urllib.request` removal made explicit, argnames/five-sites
  facts, bridge `__class__` statement listed, `.ignore` in the commit-0 stay-out list, pyproject marker names.
- Test-quality (round 5, remaining): oracle kinds derived from the baseline AST with the constants pinned to two
  names (+ a kind-flip mutant); G1 positive control cross-checked on the baseline against the independent regex
  counts; the helper-parameter `patch(patch_target)` allowlist entry; comma-separated argnames; setattr/getattr rule
  scoped to `asset_sourcer` bindings; mutant ids so §10 can record every list.

**Round 6** (security, correctness, docs: CLEAN at HIGH/MEDIUM; test-quality: one MEDIUM), changes in revision 7:
- Oracle `kind` rule was not implementable (private `Assign`/`AnnAssign` names, `MAX_API_JSON_BYTES` is private on the
  baseline) → kind is a function of the new name's visibility, with `data` for private data names.
- G1's first clause lacked the `x ∈ MOVED ∪ BRIDGE_EXTRAS` condition; G6 scope = production code; the G1 positive
  control asserts exact recorded counts; fidelity script inserts the repo root into `sys.path`; baseline-run
  provenance and the §9→§2 pointer made consistent; a duplicated D5 bullet removed.
