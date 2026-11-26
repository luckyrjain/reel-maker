# Implementation plan — `platform_http`

Spec: `docs/specs/2026-11-platform-http-module-design.md` (r9). Branch `c4-platform-http` in
`/Users/luckyjain/Projects/reel-maker`, base `main` = `1b49607` (spec commit `22cb24e`). Single repository, single PR. Tasks are
sequential on one branch (T2 needs T1's test, T3 needs T2's module, T4 needs T3); running them in parallel would only add merge
noise.

## Ground rules (every task)

1. Interpreter and tests: `/Users/luckyjain/Projects/reel-maker/.venv/bin/python -m pytest` from the repo root (never a bare
   `pytest`). Lint gate: `/opt/homebrew/bin/ruff check --select F,E9 .` (CI lints with exactly this). Baseline at `1b49607`:
   3160 selected (3157 passed, 3 skipped, 1 golden deselected, 79 test files). Run the full suite before every commit.
2. Stage **explicit files only** (`git add <path>`). Never stage `.gitignore` (a local tool edits it), `.ignore`, `.mcp.json`,
   `.claude/`, `graphify-out/`.
3. Touch only files named in the task. No schema, migration, route or template change.
4. Commit 2 is a **move**: behavior is preserved exactly (R7): no guard, no validation, no `server_supplied_url`.
5. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push or open a PR (T5 does).
6. Report truthfully; the controller re-runs every verification itself. Mutation tests apply a mutant to a **copy or by an
   in-place edit that is reverted**; `git diff` must be clean of mutants before every commit.
7. Spec wins over this plan on any conflict; report the conflict instead of guessing.

## Task graph

```
T0 baseline capture -> T1 characterization test (old code) -> T2 the move -> T3 guard, validation, tests, battery -> T4 docs -> T5 review loop + PR
```

### T0 — baseline capture (no commit)
Record `pytest --collect-only -q` node ids of `1b49607` (3160 selected) into the scratchpad. Snapshot the six test files of spec
§6.2 (`test_oauth.py`, `test_youtube_token_refresh.py`, `test_metrics_fetcher.py`, `test_youtube_publisher.py`,
`test_instagram_publisher.py`, `test_tasks_real_db.py`) for the normalized-diff script. Write the throwaway normalized-diff script
(scratchpad, not committed): strip docstrings, ignore comments, rewrite the seven `<module>.httpx.<fn>` patch-string forms of §6.2
by that fixed table, require `ast.dump` equality, require the count of string constants matching `\.httpx\.(get|post|put)\b`
within those six files to go 65 -> 0. Negative controls that must each fail: an extra edit, a wrong-module string, a
wrong-function string.

### T1 — pin the call sites on the old code (commit 1)
Files: new `tests/_platform_http_helpers.py`, new `tests/test_platform_http_callsites.py`.
- Helper: the recording fake (`inspect.signature(real).bind(url, **kw)`, records `(method, url, kwargs)` in order with the URL
  positional, answers through `httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)` with the same keywords) and
  `network_guard() -> Iterator[list]` (generator: patch `httpx.request`, `socket.socket.connect`, `connect_ex`,
  `socket.getaddrinfo` to raise `AssertionError` and append to its own list, yield the list, restore in a `finally`, then assert
  the list empty) wrapped by an autouse fixture, imported by test files as `from tests._platform_http_helpers import <fixture>  #
  noqa: F401` (precedent `tests/test_wikipedia_image_source.py:14`). Literal patch targets only (`tests/test_module_seams.py` G1).
- Test file: spec §6.3, 14 rows, the full literal wire request per row, the three status variants (400, 500, 302 with an absolute
  `Location` and a handler-called-once check) with a non-JSON body, the literal secret-name list **inside the test file**, the
  drive recipes (site 7: `uuid.uuid4` patched; sites 8/9: variants **A** multi-line caption over 5000 chars with a first line over
  100 chars, a distinct short second line and 16+ hashtags, **B** short multi-line caption and 16+ short hashtags with the exact
  description `caption + "\n\n" + "#h1 ... #h15"` and exactly 15 tags, **C** whitespace-only caption with `hashtags=None` and a
  `hashtags=[]` variant; absolute `https://` `Location`; site 10 caption over 2200 chars; site 14 on an in-memory SQLite session
  with real `Reel`/`Cut` rows, asserting a `StageEvent(ok=False)` on a 400). Row 8/9 comparisons of the JSON body use parsed JSON.
- The transitional `bearer=None` behavior is **not** pinned (spec §6.3).
- **Old-code mutation (scripted, exhaustive):** a throwaway script in the scratchpad generates and applies each mutant to the old
  call-site modules and runs `test_platform_http_callsites.py`; every mutant must turn at least its own row red; revert. Set:
  14 `timeout` arguments removed, 14 `raise_for_status()` calls removed, 7 bearer headers removed, an `Authorization` header added
  at site 9, and one value-changing mutant for every literal in the enclosing function of each call, in
  `_build_multipart_related`, and in the module/class constants a call reads (`_GRAPH`, `_UPLOAD_URL`, `_CAPTIONS_UPLOAD_URL`,
  `token_url`, the site-8 metadata such as `privacyStatus`), plus the **swap table** (each mutant exchanges two operands of the
  same call): sites 1-4 `client_id`/`client_secret`/`redirect_uri`/`code` pairwise; sites 5-6 `user_access_token`/`page_token`,
  `page_id`; site 7 `video_id`/`access_token`; sites 10-12 `ig_user_id`/`creation_id`/`access_token`; site 13 `access_token`/
  `cut.platform_post_id`; site 14 `credential.token_blob`/`cut.platform_post_id`. The fixtures give every operand a distinct
  value, so a swap is never equivalent; a genuinely equivalent literal mutant is named in the record with its reason.
- Commit: `test: pin the platform call sites before moving them`.

### T2 — the move (commit 2)
Files: new `api/platform_http.py`; edit `api/oauth.py`, `engine/publish/youtube.py`, `engine/publish/instagram.py`,
`engine/publish/metrics.py`; the retargets of the six test files (spec §6.2).
- Module per R7 / spec §10 commit 2, nothing more: `DEFAULT_TIMEOUT_S = 30.0`, `UPLOAD_TIMEOUT_S = 300.0`, `_UNSET`, `get`/`post`/
  `put(url, *, bearer=_UNSET, headers=None, params=None, [data, json, content,] timeout=DEFAULT_TIMEOUT_S)`: build kwargs from
  arguments that are not `None` (plus `timeout` as given), `bearer` given -> new dict `{"Authorization": f"Bearer {bearer}",
  **(headers or {})}`, call `httpx.<verb>(url, **kwargs)` (module attribute, URL positional), `resp.raise_for_status()`, `return
  resp`. `put` has **no** `server_supplied_url`.
- The 14 call sites per spec §4.3 (site 9 without `server_supplied_url`), called as `platform_http.<fn>(...)` after
  `from api import platform_http`; remove `import httpx` from `youtube.py`, `instagram.py`, `metrics.py` (keep it in `oauth.py` for
  `QueryParams`).
- The 65 mechanical retargets; nothing else in the test files changes.
- Verify: full suite = baseline set + T1's tests (the unmodified `test_platform_http_callsites.py` passes), ruff clean, the
  normalized-diff script passes on every retargeted file and fails each negative control. Record the outputs. No new tests.
- Commit: `refactor: route platform API calls through api/platform_http.py`.

### T3 — guard, validation, tests, battery (commit 3)
Files: `api/platform_http.py` (full behavior), `engine/publish/youtube.py` (site 9 gets `server_supplied_url=True`), new
`tests/test_platform_http.py`.
- Module: spec §4.1-4.2 in full: `SECRET_QUERY_NAMES` (37 names), `_normalise` (decode to a fixpoint, cap 8, reject at the cap),
  the guard (params keys via `httpx.QueryParams`, `str`/`bytes` params split by the module, the URL query split on `&` and `;`,
  userinfo, whitespace URL, unparseable paths raised after the `except` block), bearer validation (`re.fullmatch`, https only,
  not with `server_supplied_url`), header validation (names RFC 7230 token, values `fullmatch`), timeout `0 < t <= 3600` before
  `float()`, type checks, `server_supplied_url` allowlist (`Content-Type`/`Content-Length`, exact names after `str.lower()`,
  no `params`, no `bearer`, duplicates rejected, own constant messages). Calls only `urllib.parse.urlsplit`,
  `urllib.parse.unquote_plus`, `httpx.QueryParams` as module attributes. Imports exactly `httpx`, `re`, `urllib.parse`,
  `collections.abc`, `__future__`; no `json`.
- Tests: spec §6.3 commit-3 list, PH-G1..PH-G5 with controls. **What is written new** (no precedent in `tests/_seam_ast.py`): a
  module-binding multiplicity counter (every binding form; module `Name` exactly one per file), a class-attribute resolver
  (enclosing class body only, annotation-only ignored, stores via `self.`/`cls.`/`ClassName.`/`setattr`), a JoinedStr flattener
  (module-Name substitution, `ast.dump`-pinned id holes per (file, function, expression), no `format_spec`/`conversion`, a
  literal `/` before the first id hole), an authority splitter, the call-shape checker (`url` positional or keyword, no `*`/`**`),
  the per-callee `upload_url` allowlist, PH-G2's non-callee walk (captures, tuple-unpacking and conditional aliases). Reused:
  `parse`/`annotate`, `_class_of`, `import_bindings`, `dotted`, `Problem`, `_kw`, `g5_import_problems`-style import checks.
- Battery M1-M171 (M59 retired): apply each mutant to the module or to a synthetic snippet, run the named test, confirm red,
  revert. **The M-id -> test map** (test names in `tests/test_platform_http.py`; each row's exact failing assertion goes into the
  implementation record):

| Mutants | Test (function) |
|---|---|
| M1-M7, M70, M95a-b | `test_raise_for_status_per_verb`, `test_status_variants`, `test_returns_resp_not_raise_result`, `test_uses_httpx_verb_directly` |
| M8-M15, M82, M83, M109, M113, M115, M138 | `test_bearer_*` (format, omitted sends no header, None/empty/whitespace/non-str/control/non-ASCII/DEL/trailing-newline rejected, https only, with `server_supplied_url`, no echo) |
| M16-M23, M84, M110, M111, M114, M139, M140 | `test_headers_*` (merge, collision any case, no mutation of the caller dict, Authorization first, Mapping/str types, name token, value `fullmatch`, empty value, no echo) |
| M24-M33, M92a-c, M93, M94, M118, M119a-c, M120a-c | `test_forwards_only_supplied`, `test_url_positional`, `test_never_forwards_httpx_defaults`, `test_params_same_object` (bind-only recorder, per verb) |
| M34-M41, M90, M91, M154, M157, M158 | `test_timeout_*` (defaults per verb, constants by value, override, type, range accept/reject table) |
| M42-M63, M87-M89, M112, M117, M156, M159 | `test_guard_params_*`, `test_guard_url_query_*`, `test_guard_variants` (case, decode layers 9+, `;`, key not first, bare key, forms a-h), `test_guard_message` (matched name, `bearer=`, no raw key/value/URL), `test_normalise_fixpoint` |
| M62, M86, M107, M108 | `test_unparseable_url`, `test_unparseable_params` (patched raises + smokes; message equality; `__context__`/`__cause__` None) |
| M64-M69, M81, M148, M162, M168-M171, M65 | `test_server_supplied_url_*` (query scan skipped only; allowlist accept/reject tables incl. benign names and normalised spellings; `params` `None`/`{}`/`{"part": "x"}`; `bearer=`; duplicates; own messages) and the site-9 `?key=` publish test |
| M85, M88, M63 | `test_type_checks_no_echo` (url/headers/params/timeout types, one sentinel per message) |
| M97, M98 | `test_transport_errors_not_wrapped`, `test_no_import_time_capture` |
| M71-M72, M80, M99-M100, M121-M123, M136, M147, M166 | PH-G1 tests with controls |
| M72, M77, M124, M125, M141, M142, M160 | PH-G2 tests |
| M73-M76, M102-M106, M126-M135, M143-M146, M149-M153, M163, M165, M167 | PH-G4 tests (controls per spec §4.5) |
| M78, M79, M101, M161 | PH-G5 tests |
| M137, M164 | `test_network_guard_control` |
| M51 | `test_secret_names_literal_pin` |

- Commit: `test: pin platform_http's rules and confine raw httpx`.

### T4 — docs (commit 4)
Everything in spec §8 (CLAUDE.md Module layout, Key conventions bullet, tests list, Build-phase paragraph, Commands count, Dependency
caps clause; CONTEXT.md "Platform HTTP"; README.md:347; `docs/architecture.md` tables, counts (79 -> 81 test files, three helper
modules), prose; `docs/prd.md:226`) and the implementation record of spec §10 (baseline, normalized-diff output, old-code mutation
results with the count of value mutants, the M1-M171 table with equivalent mutants named, final counts from
`pytest --collect-only`). Commit: `docs: document platform_http and record the evidence`.

### T5 — review loop and PR
Four personas on `main..HEAD` until a full round is clean; fixes in their own commits; re-verify after each. Then push and open the
PR (summary, proof output, review history), bind with the ccd_pr tools, offer Auto-fix. **Do not merge** (merge only on "check ci
and merge").

## Verification matrix

| Check | After T1 | After T2 | After T3 | After T4 |
|---|---|---|---|---|
| Suite | 3160 + new callsite tests | same set, unmodified T1 tests green | + `test_platform_http.py` | same |
| `ruff check --select F,E9 .` | clean | clean | clean | clean |
| Normalized test diff | n/a | all six files pass, controls fail | n/a | n/a |
| Mutants | old-code set (T1) | n/a | M1-M171 | recorded in §10 |
