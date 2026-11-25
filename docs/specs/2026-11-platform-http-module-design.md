# Design — `api/platform_http.py`: one place for platform API calls

Phase 8i · Candidate C4 of the 2026-11-03 full-codebase architecture review (`improve-codebase-architecture`), rated
"Worth exploring". Status: **r9** (r1 plus the findings of review rounds 1-8; round 8 was the first round with four clean verdicts). Base: `main` = `1b49607`, branch
`c4-platform-http`. Single repository, single PR. No schema, migration, route or template change.

## 1. Problem

Fourteen call sites in four modules talk to platform APIs (Google OAuth/YouTube, Meta Graph) with raw `httpx`:

| Module | Sites | Calls |
|---|---|---|
| `api/oauth.py` | 6 | 4 token POSTs (`exchange_code` x2, `refresh`, `exchange_long_lived_token`), 2 GETs (`discover_account`) |
| `engine/publish/youtube.py` | 3 | captions POST, resumable-init POST, video PUT |
| `engine/publish/instagram.py` | 3 | container POST, poll GET, publish POST |
| `engine/publish/metrics.py` | 2 | YouTube `videos.list` GET, Instagram `insights` GET |

Each site repeats the same decisions by hand: `timeout=30.0` (300.0 for the video PUT), `raise_for_status()` straight after
the call and, for seven of them (sites 5-8, 11, 13, 14 in the numbering of §4.3), `headers={"Authorization": f"Bearer
{token}"}`. The policy that matters most is not written in code at all, only as comments in three files (`oauth.py`,
`instagram.py`, `metrics.py`): **a secret never goes in a URL**. Why it matters: `httpx.HTTPStatusError.__str__` includes the
request URL, and that string reaches `job.error` (persisted, shown in the UI) and `api/routers/credentials.py`
(`detail=f"... failed: {exc}"` in a 502 body). A single `params={"access_token": ...}` added in a future edit would leak a
credential into both places, and nothing would fail. CLAUDE.md's "Token-in-URL is a logging/DB leak" bullet records the
fixes made by hand so far (four functions in `oauth.py`/`instagram.py`, plus "every call site added in Phase 5"), each a
comment, none a rule.

A smaller cost: a change to the platform-call policy (a different default timeout, a log line, a User-Agent) is a 14-site
edit, and the 65 test patch strings (`engine.publish.youtube.httpx.post`, ...) show the call shape duplicated into tests.

Deletion test: removing the repeated `timeout`/`raise_for_status`/bearer lines concentrates complexity into one small module
with a real rule (the secret guard) instead of spreading it. The module is shallow by size and deep by what it enforces; it
earns its place on the rule, not on the line count.

## 2. Scope

In: the 14 sites above, a new `api/platform_http.py`, guards, characterization tests, retargeting of 65 test patch strings,
docs.

Out, deliberately (the policy items are decisions Q1-Q4 in §3; the residual risk is §7):
- `engine/generation/llm.py` (2 sites): different policy (360 s completion timeout, an API key not a credential, and
  `check_model_available` swallows every error by design). Sharing would force flags.
- `engine/render/safe_fetch.py` and the asset sourcers: already behind the C2 hardened fetch layer. Their `timeout=30.0`
  literals (`asset_sourcer.py:233`, `:411`) are the sourcers' own request to that layer and stay.
- Retry, backoff, redaction, size caps, deadlines, redirect handling, a host allowlist for the YouTube upload URL.
- `worker/tasks/common.py::is_transient_error`: the exception type stays `httpx.HTTPStatusError`.

## 3. Decisions

Grilling (Q1-Q10; all recommended answers accepted):

| # | Decision |
|---|---|
| Q1 | Platform calls only. `llm.py` stays out. |
| Q2 | Consolidate **and enforce**: default timeout, `raise_for_status`, bearer header builder, plus a fail-loud guard that rejects secret-named keys in URL parameters. No redaction (the exception type and text are unchanged). |
| Q3 | Separate module with no dependency on `safe_fetch`. "Platform calls are not size-capped or deadline-bounded" is a known gap, not a goal. |
| Q4 | No retry or backoff in the module (retry policy stays in `job_task`; `publish_cut` is `max_retries=0` on purpose). Two named constants, per-call override allowed. |
| Q5 | Home: `api/platform_http.py`. It imports nothing from `api`/`engine`/`worker`, so it sits below every caller. `youtube.py` already imports `api.oauth`; `instagram.py` already imports `api.config`; `metrics.py` gets a new direct `api.*` edge, in the direction `engine -> api` that already exists. |
| Q6 | Interface: three thin functions `get`, `post`, `put` returning the `httpx.Response` after `raise_for_status()`. No client class (nothing to hold: fixed hosts, a different token per call). |
| Q7 | Tests are retargeted mechanically to the bare `httpx.get/post/put` (the C2 "D6" precedent), proved by the normalized-diff script. No bridge or re-exported `httpx` attribute in the old modules. |
| Q8 | Guards PH-G1..PH-G5 (§4.5), each with synthetic positive and negative controls. |
| Q9 | Pin behavior first: a characterization test of all 14 sites, green on the old code, passing unmodified after the move. Then a mutation battery on the module. |
| Q10 | The YouTube resumable `upload_url` (Google's session URL, carried in error text) is a recorded known gap, not redacted. |

Review (R1-R9; each was implicit or wrong before the spec review rounds 1-8):

| # | Decision |
|---|---|
| R1 | `bearer` defaults to a sentinel (omitted = no Authorization header). An explicit `None`, empty, whitespace-only, non-`str` or malformed (§4.2 rule 2) bearer raises `ValueError` before any request. Reason: `bearer=credential.token_blob` (site 14; `Credential.token_blob` is a nullable column) used to send the literal `Bearer None`. Deliberate delta, §5(b). |
| R2 | The guard compares *normalised* keys: percent-decode (`unquote_plus`, repeated until stable, capped at 8 passes; a key still changing at the cap is rejected, fail closed), lower-case, strip, remove every non-alphanumeric character, so `access_token`, `Access-Token`, `accessToken`, `access_token[]`, `access_token `, `%61ccess_token` and `%2561ccess_token` all become `accesstoken`; the decode applies uniformly to every source (a mapping key `%61ccess_token` is thus rejected too: harmless over-reject, one rule instead of per-form scoping). The name set is §4.1's (37 names, including the `oauth_`/`api_`/`auth_`/`session_`/`security_` token forms, `apikey`, `x-goog-api-key`, `secret`, `id_token`, `authorization`, `auth`, `signature`, `sig`, `bearer`). Semicolon-separated query pairs are scanned too. |
| R3 | A URL carrying userinfo (`user:password@host`) is rejected. `auth=` and `cookies=` are not parameters of the wrapper (a call with them is a `TypeError`, pinned). |
| R4 | `put` has an explicit keyword `server_supplied_url=False`. When true, the URL-*query* scan is skipped, userinfo is still checked, and `params=` is not allowed at all. Exactly one site passes it, as a literal `True` (the YouTube upload, site 9: the URL is Google's session URL, so a future `key=`-style parameter there must not fail a publish after the session exists). A host allowlist for that URL is **not** added here (unverified live shape; §7). `server_supplied_url=True` together with `bearer=` is a `ValueError` (a credential must never be sent to a server-chosen host; PH-G2 also asserts the allowlisted call has no `bearer=`). With the flag set the call may carry **only** the headers `Content-Type` and `Content-Length` (names compared after `str.lower()` against exactly `content-type` and `content-length`, so `Authorization`, `Proxy-Authorization`, `Cookie`, `X-Api-Key`, `X_Goog_Api_Key`, `Accept`, `ContentType` and `Content_Type` are all rejected, and two keys that case-fold to the same name are rejected too), no `params=` (`params is not None` is rejected, `{}` included) and no `bearer=`; anything else is a `ValueError` (an allowlist, so a credential-header denylist cannot drift). PH-G2 asserts the allowlisted call's `headers=` is a dict literal with exactly those constant keys and that it has no `params=` or `bearer=`. |
| R5 | `headers` is a `Mapping` with `str` keys that are RFC 7230 tokens and `str` values of printable ASCII without leading or trailing whitespace (anything else is a `TypeError`/`ValueError`, §4.2 rule 7, never echoing the offending text). With `bearer`, the wrapper builds a **new** dict `{"Authorization": ..., **headers}` (Authorization first among the caller-supplied headers; the caller's dict is never mutated, its key case is preserved); the collision check is case-insensitive over the keys. Without `bearer`, the caller's object is forwarded unchanged (same object). The original `params` object is forwarded; the guard works on a normalised copy. |
| R6 | A plan document is written after the spec review is clean (`docs/specs/2026-11-platform-http-implementation-plan.md`), as for C2 and C3. |
| R7 | Commit 2 is the literal move: its module has **only** forward-only-supplied kwargs, the `_UNSET` sentinel for `bearer` (an omitted `bearer` sends no header; an explicit `None` still yields `Bearer None` until commit 3 validates, exactly the old behavior), the two timeout constants, positional URL and `raise_for_status`; `put` has no `server_supplied_url`. Commit 3 adds the guard, the bearer/header/type validation and `server_supplied_url`, and changes the one youtube.py call (site 9) to pass it, together with the tests that pin them. |
| R8 | `timeout` must be an `int`/`float` that is not a `bool` (forwarded as `float(timeout)`); `None`, an `httpx.Timeout` or anything else is a `TypeError` (a `None` timeout would mean "no timeout" to httpx); it must also be in the range `0 < t <= 3600`, checked **before** any `float()` conversion (`nan`, `inf`, zero, negatives and an `int` such as `10**400` all fail the comparison and raise a generic `ValueError`; `float(t)` runs only after it passes, so no raw `OverflowError` escapes). |
| R9 | PH-G4 fails closed on any URL expression it cannot resolve and on any URL text containing `?`, `;` or `#` (§4.5), instead of inspecting only constants. |

## 4. Design

### 4.1 Public surface of `api/platform_http.py`

```python
DEFAULT_TIMEOUT_S = 30.0          # every platform call except the video upload
UPLOAD_TIMEOUT_S = 300.0          # the YouTube video PUT only
SECRET_QUERY_NAMES = frozenset({  # NORMALISED names (see _normalise); pinned as a literal by a test
    "accesstoken", "refreshtoken", "idtoken", "clientsecret", "secret", "fbexchangetoken",
    "apikey", "xgoogapikey", "key", "token", "code", "password", "auth", "authorization", "bearer",
    "signature", "sig",                                                          # 17
    "oauthtoken", "apitoken", "authtoken", "sessiontoken", "securitytoken", "accesskey",
    "secretkey", "privatekey", "accesssecret", "jwt", "passwd", "pwd",           # + 12 = 29
    "appsecret", "apisecret", "sessionid", "credential", "assertion", "clientassertion",
    "authorizationcode", "authkey",                                              # + 8 = 37
})

def get(url, *, bearer=_UNSET, headers=None, params=None, timeout=DEFAULT_TIMEOUT_S) -> httpx.Response
def post(url, *, bearer=_UNSET, headers=None, params=None, data=None, json=None, content=None,
         timeout=DEFAULT_TIMEOUT_S) -> httpx.Response
def put(url, *, bearer=_UNSET, headers=None, params=None, data=None, json=None, content=None,
        timeout=DEFAULT_TIMEOUT_S, server_supplied_url=False) -> httpx.Response
```

Private: `_UNSET`, `_normalise(key) -> str` (decode as R2, then `re.sub(r"[^a-z0-9]", "", ...)` on the stripped, lower-cased
text), and the helpers that implement rules 2, 3 and 7 (as shipped: `_secret_message`, `_split_query`, `_url_problem`, `_params_problem`, `_guard`, `_check_types`, `_check_bearer`, `_check_server_supplied`) behind `_prepare(...) -> dict` (validation, guard and keyword assembly). The module imports
`httpx`, `re`, `urllib.parse`, `collections.abc` and `__future__` only: no `api.*`, `engine.*`, `worker.*`, and it never
imports `json` (the `json=` parameter shadows the stdlib name inside `post`/`put`; pinned by PH-G5).

### 4.2 Behavior (each rule is test-pinned)

1. **Forward only what was supplied; URL positional.** `_prepare` builds the keyword set for `httpx` from arguments that
   are not `None`, plus `timeout` (always). It never passes `params=None`, `data=None`, `json=None`, `content=None` or
   `headers=None`; the URL is passed positionally (`test_youtube_publisher.py:69`, `test_metrics_fetcher.py:113` and
   `test_instagram_publisher.py:51` read the URL as `call_args[0][0]`/`.args[0]`). Reason: existing tests assert `"params" not
   in kwargs or not kwargs["params"]` (`test_oauth.py:91,110`), and `test_youtube_token_refresh.py::_Google` records every
   keyword `refresh()` passes and asserts `extra_kwargs == [{}]` (`:102`). Never set: `follow_redirects`, `verify`,
   `trust_env`, `auth`, `cookies` (httpx defaults, as before).
2. **`bearer=`.** Omitted: no Authorization header from the wrapper. Given as a `str` of one or more printable ASCII
   characters with no whitespace (`re.fullmatch(r"[\x21-\x7e]+", ...)`: `fullmatch`, because `$` would accept a trailing
   newline) **and** with an `https` URL: header `Authorization: Bearer <value>`, first
   among the caller-supplied headers (R5). Given as `None`, `""`, whitespace-only, a non-`str`, or a `str` with a control
   character, whitespace or non-ASCII: `ValueError("bearer must be a non-empty str")` before any request, the value never
   echoed (a newline in a token would otherwise reach h11, whose error message quotes the whole header value). A bearer with a
   non-`https` URL: `ValueError("bearer requires an https URL")`. `bearer=` together with `server_supplied_url=True`:
   `ValueError("bearer is not allowed with a server-supplied URL")`. A caller `headers` mapping that already holds an
   `Authorization` key (any case) together with `bearer` is a `ValueError`; without `bearer` such a header skips only the collision check (its value is still validated, rule 7).
3. **Secret guard.** Before any request, `ValueError` if (a) a key of `params` normalises into `SECRET_QUERY_NAMES` — the keys
   are read from `httpx.QueryParams(params)` (the parser httpx itself uses) for a mapping, a `QueryParams` or a sequence of
   pairs, and by the module's own `&`/`;` splitting for a `str`/`bytes` query string (`bytes` decoded as ASCII; a non-ASCII
   byte is `unparseable params`, never a raw `UnicodeDecodeError`; a leading `?` dropped, blank values kept so a bare key
   counts); a `None` or list value counts by its key; (b) a pair of the URL's own query normalises into it (split on both `&`
   and `;`, blank values kept, every pair checked, not the first); or (c) the URL's netloc carries userinfo (`@`). A URL that differs from `url.strip()`, or that starts or ends with any `str.isspace()` character, is `unparseable URL` (httpx would parse it differently from `urlsplit`). `urlsplit`
   failures (a malformed netloc makes it raise a `ValueError` that can quote part of the netloc) and any exception raised
   while computing the guard (`str(key)`, `QueryParams(...)` on hostile params) are caught, a flag is set, and the
   replacement `ValueError("platform_http: unparseable URL")` (or `"platform_http: unparseable params"`) is raised **after**
   the `except` block: `from None` alone would leave the original in `__context__`, so the tests assert
   `exc.__context__ is None and exc.__cause__ is None`. The guard's own message names the **matched secret name taken from
   the constant** (for example `accesstoken`), never the caller's raw key, value or URL, and says to use `bearer=` or the
   request body (the substring `bearer=` is pinned). Not guarded, on purpose: `data=`, `json=`, `content=`, parameter
   *values*, the URL path and fragment (§7); `headers` are validated by rule 7 but never scanned for secrets (secrets belong
   there; sites 1-4, 10 and 12 send theirs in a form body). With `server_supplied_url=True` (put only), (b) is skipped, (c) still runs, any `params` that is not `None` (an empty `{}` included) is a `ValueError` and only `Content-Type` and `Content-Length` headers are accepted, compared after `str.lower()` against exactly those two names (`Content-Type` and `CONTENT-LENGTH` are accepted, `ContentType` is not), with two keys that case-fold to the same name rejected (R4). A subset (`Content-Type` alone) and `headers=None` are accepted at runtime; PH-G2 is intentionally stricter and requires the one real call to carry exactly both keys. The allowlist rejection has its own constant message, `"platform_http: header not allowed with a server-supplied URL"` (and `"platform_http: params not allowed with a server-supplied URL"` for `params`). httpx replaces the URL's own query when `params` is given, so the guard scanning both does
   not imply merging. A stateful mapping that changes between the guard's read and httpx's is out of scope. The module calls `urllib.parse.urlsplit`, `urllib.parse.unquote_plus` and `httpx.QueryParams` **as module attributes** (never through a `from ... import`), so the tests can patch them; PH-G5 enforces this.
4. **`raise_for_status()` always**, straight after the call, before the caller touches the body. The same
   `httpx.HTTPStatusError` class and text as before (the text contains the full URL, including non-secret params), so
   `is_transient_error`, `job.error` and the credentials-router 502 text are unchanged. Non-2xx, including 3xx (redirects
   are not followed), raises. Transport errors propagate unchanged (not wrapped).
5. **Module-attribute calls into `httpx`** (`httpx.get(...)` resolved at call time, never captured at import), so a test
   patching bare `httpx.get` / `httpx.post` / `httpx.put` is seen. No `request()` indirection. The response is returned as
   is (`return resp`, not the result of `raise_for_status()`).
6. **Returns the `Response` untouched.** `.json()`, `.headers["Location"]` (the YouTube resumable session) and `.content`
   stay at the call sites.
7. **Input validation, before any request.** `TypeError` for: a `url` that is not a `str` (not `bytes`, not `httpx.URL`; the
   message never echoes the object, whose `repr` can carry userinfo); `headers` that is not a `Mapping[str, str]`; a
   `params` that is not `None`, a `Mapping`, an `httpx.QueryParams`, a list/tuple of pairs, a `str` or `bytes`; a `timeout`
   that is not a number (R8). `ValueError` for a `timeout` outside R8's range, for a header name that is not an RFC 7230
   token, and for a header value that does not `fullmatch` `[\x21-\x7e]([\x20-\x7e\t]*[\x21-\x7e])?` (so CR, LF, NUL, `\x0b`,
   DEL, non-ASCII, leading/trailing whitespace and the empty string are rejected: h11 would echo the whole value in its error
   text, which reaches `job.error`; a non-ASCII header value is rejected on purpose, no site sends one). **No message raised
   by the module contains caller-supplied text** (only the matched secret name from the constant and type names), pinned
   with one distinct-sentinel test per message; messages raised by third-party code on malformed input that gets past the
   module (an `httpx.InvalidURL` quoting a bad port, a `UnicodeEncodeError` naming a lone surrogate in `data`) are out of
   this claim (§7). No site violates any of these today.

### 4.3 The 14 call sites after the change

| # | Site | Before (kwargs sent to `httpx`) | After |
|---|---|---|---|
| 1 | `YouTubeOAuth.exchange_code` | POST `data`(5 keys), `timeout=30.0` | `platform_http.post(self.token_url, data={...})` |
| 2 | `YouTubeOAuth.refresh` | POST `data`(4), 30.0 | same shape |
| 3 | `InstagramOAuth.exchange_code` | POST `data`(4), 30.0 | same shape |
| 4 | `InstagramOAuth.exchange_long_lived_token` | POST `data`(4), 30.0 | same shape |
| 5 | `discover_account` `/me/accounts` | GET `headers`(Authorization), 30.0 | `platform_http.get(url, bearer=user_access_token)` |
| 6 | `discover_account` page | GET `params`, `headers`, 30.0 | `get(url, params=..., bearer=page_token)` |
| 7 | `youtube._upload_captions` | POST `params`, `headers`(Authorization + Content-Type), `content`, 30.0 | `post(url, params=..., bearer=access_token, headers={"Content-Type": ...}, content=body)` |
| 8 | `YouTubePublisher.publish` init | POST `params`, `headers`(4), `json`, 30.0 | `post(url, params=..., bearer=..., headers={3 non-auth}, json=metadata)` |
| 9 | `YouTubePublisher.publish` PUT | PUT `headers`(Content-Type, Content-Length), `content`, `timeout=300.0`, **no Authorization** | commit 2: `put(upload_url, headers={...}, content=video_bytes, timeout=UPLOAD_TIMEOUT_S)`; commit 3 adds `server_supplied_url=True` |
| 10 | `InstagramPublisher._create_container` | POST `data`(4, incl. `access_token`), 30.0 | `post(url, data={...})` (body secret allowed) |
| 11 | `_wait_until_ready` | GET `params`, `headers`, 30.0 | `get(url, params=..., bearer=access_token)` |
| 12 | `_publish_container` | POST `data`(2, incl. `access_token`), 30.0 | `post(url, data={...})` |
| 13 | `YouTubeMetricsFetcher.fetch` | GET `params`, `headers`, 30.0 | `get(url, params=..., bearer=access_token)` |
| 14 | `InstagramMetricsFetcher.fetch` | GET `params`, `headers`, 30.0 (inside `record_stage`) | `get(url, params=..., bearer=credential.token_blob)` (still inside `record_stage`) |

Site 9 sends no Authorization on purpose (the session URL is the capability); the module must not add one, and the
characterization test pins its absence. Sites 10 and 12 keep the secret in the form body, allowed by rule 3 (Meta also
accepts a bearer header, which would be the safer shape; changing the wire form is not part of this PR, §7). Everything else
in these functions (parsing, `record_stage` composition, the `Location` read, the poll loop, error messages) is untouched.
Imports: `import httpx` is removed from `youtube.py`, `instagram.py` and `metrics.py` (unused afterwards); `api/oauth.py`
keeps it for `httpx.QueryParams` in the two `authorize_redirect_url` methods (URL building, not a request).

### 4.4 Dependency direction

`api/platform_http.py` imports `httpx`, `re`, `urllib.parse`, `collections.abc`, `__future__` and nothing else. Importers:
`api/oauth.py`, `engine/publish/youtube.py`, `engine/publish/instagram.py`, `engine/publish/metrics.py`, each as `from api
import platform_http` and called as `platform_http.get(...)` (module attribute, the C2 "D3" rule: no from-import of
`get/post/put`, no alias, no default argument, no partial).

### 4.5 Guards (all in `tests/test_platform_http.py`, prefixed `PH-` to keep them apart from `tests/test_module_seams.py`'s G1-G6; each with synthetic positive and negative controls)

- **PH-G1 confinement.** An AST walk of `api/`, `engine/`, `worker/`, resolving aliases with `tests/_seam_ast.py`
  (`import_bindings`, `dotted`; covers `import httpx as h`, `from httpx import post as p`). (a) Any `httpx` attribute or
  from-import name other than the **non-request allowlist** `{QueryParams, HTTPStatusError, HTTPError, TransportError,
  TimeoutException, InvalidURL, Response, URL, Headers}` may appear only in `ALLOWED_REQUEST = {api/platform_http.py,
  engine/generation/llm.py, engine/render/safe_fetch.py}`. The rule is an allowlist of harmless names, not a blacklist of
  request names, so an unknown name fails (controls, in attribute and from-import forms: `HTTPTransport`, `Request`, `stream`, `Client`, `AsyncClient`, `request`; M121, M166). (b) `.raise_for_status` attribute access
  only in `ALLOWED_RAISE = ALLOWED_REQUEST | {engine/render/asset_sourcer.py}` (the sourcers own the status check after
  `safe_fetch`, C2). (c) An import of another HTTP client (`requests`, `aiohttp`, `urllib3`, `http.client`,
  `urllib.request`) only in `ALLOWED_REQUEST` (today only `safe_fetch.py` imports `urllib.request`, for proxy detection).
  Non-vacuity on the real tree, outside the three allowed files: the only `httpx` names used are `QueryParams`
  (`api/oauth.py`), `TransportError` and `HTTPStatusError` (`worker/tasks/common.py`); `youtube.py`, `instagram.py` and
  `metrics.py` use none; and the three allowed files do use request-capable names.
- **PH-G2 module-attribute access.** In production code `api.platform_http` is imported only as a module (`from api import
  platform_http`, `import api.platform_http`, with or without `as`), never `from api.platform_http import get/post/put/...`;
  its functions are used only as the callee of an attribute call (a capture such as `x = platform_http.get`, a tuple-unpacking
  or conditional alias is a failure), and `DEFAULT_TIMEOUT_S`/`UPLOAD_TIMEOUT_S` only as reads inside a function (one
  `UPLOAD_TIMEOUT_S` read, at site 9). Any `server_supplied_url=` keyword must be the literal `True`, on a `platform_http.put` call that has no `bearer=` and no `params=` and whose `headers=` is a dict literal with exactly the constant `str` keys `Content-Type` and `Content-Length` (a non-literal, a spread or any other key fails closed), inside the allowlisted (file, function) `engine/publish/youtube.py::YouTubePublisher.publish`. Non-vacuity: the walk sees exactly **14** `platform_http.<fn>` calls
  (oauth 6, youtube 3, instagram 3, metrics 2; this count holds from commit 2, the guard tests arrive in commit 3) and
  exactly one `server_supplied_url=True`. Controls cover `import api.platform_http as ph; ph.get(...)`, `x = platform_http.get`,
  `server_supplied_url=flag`, `=1`, on `get`, with `bearer=`, with `params=`, with `headers={"Authorization": ...}`, `headers={"Cookie": ...}`, `headers={"Accept": ...}` and `headers=hdrs`, a `headers=` literal with an extra benign key, with only `Content-Type`, with a `**` spread, with a non-constant key or with a duplicate key, and each forbidden import form.
- **PH-G3 secret guard, behavior.** The set is pinned as a literal (`SECRET_QUERY_NAMES == frozenset({...37 names...})`) and
  the cases are parametrized over a literal list in the test file, never over the imported constant (so deleting a name
  deletes the guard, not the case). Rejected, for each of `get`/`post`/`put`: every name in params (dict, list of pairs,
  `QueryParams`, a `str` and a `bytes` query string, a `?`-prefixed string, a bare key, a `None` value, a list value) and in
  the URL query (including a bare `?access_token`), in upper/lower/mixed case, with the variants `access-token`,
  `accessToken`, `access_token[]`, `%61ccess_token`, an `access_token` encoded in **9 or more** layers (the fixpoint cap is 8,
  so fewer layers would not kill the fail-open mutant), `access_token ` (trailing space), `;`-separated, the secret key not
  first (`{"part": "x", "access_token": t}`, `?a=1&access_token=`), URL userinfo, and a URL with leading or trailing
  whitespace. **Unparseable inputs, deterministically:** for the URL path, `urllib.parse.urlsplit` is patched (`monkeypatch.setattr(urllib.parse, "urlsplit", ...)`; the module calls it as an attribute) to raise `ValueError` carrying a sentinel (and one real smoke input, `https://h℀/x`, whose Python 3.14 `urlsplit` error quotes the netloc; Python-version dependent, so it is skipped if `urlsplit` does not raise on the running Python); for the params path, `httpx.QueryParams` is patched to raise (`monkeypatch.setattr(httpx, "QueryParams", Raising)` where `Raising` is a `httpx.QueryParams` subclass whose `__init__` raises (so an `isinstance` check against it still works), with a **dict** `params`: a `str`/`bytes` `params` is split by the module itself and never reaches `QueryParams`), with
  `params=[("a",)]` and a key whose `__str__` raises as real smokes. Each asserts `pytest.raises(ValueError)`, the `httpx`
  function not called, the message **equal to** `"platform_http: unparseable URL"` / `"platform_http: unparseable params"`
  (no sentinel in it) and `exc.__context__ is None and exc.__cause__ is None`; these kill M62, M86, M107 and M108.
  Accepted: the real call params (`part`, `id`, `fields`, `metric`, `uploadType`), near-miss names (`keyword`, `monkey`,
  `tokens`, `encoded`, `code_challenge`, `pagetoken`, `nextpagetoken`), a secret-looking name only as a *value*
  (`params={"fields": "access_token"}`), the same names in `data=`, `json=`, `content=`, `headers`, an `@` in the URL path or
  query (not userinfo), and `https://h/a?x=1/media` (a `?` plus a non-secret pair arriving through an id). No `httpx` call is
  made on a rejection (`assert_not_called`); the message contains the matched constant name and `bearer=`, and neither the
  raw key (a key such as `"access_token" + "-" * 200` is rejected with a message of bounded length that does not contain the
  dashes), the value nor the URL (distinct sentinels in `params` and in the URL query are asserted absent).
  `server_supplied_url=True` accepts `https://upload.example.com/s?upload_id=1&key=2` with `Content-Type`/`Content-Length` headers (also as `content-type` and `CONTENT-LENGTH`; `Content-Type` alone and `headers=None` accepted; `params=None` accepted), and rejects (`ValueError`) any non-`None` `params=` (`{}` and a non-secret `{"part": "x"}` included), userinfo, `bearer=`, two keys that case-fold to the same name, `ContentType`, `Content_Type`, `Content%2DType`, and every other header, credential-like (`Authorization`, `Proxy-Authorization`, `Cookie`, `X-Goog-Api-Key`, `X_Goog_Api_Key`, `X-Api-Key`) **and benign** (`Accept`, `X-Upload-Content-Type`, `X-Foo`), so an allowlist cannot be replaced by a denylist (M162); each allowlist rejection is asserted equal to the constant allowlist message above, so a rule-7 rejection cannot mask a missing allowlist check, and `X_Foo` and `X%41` are accepted by rule 7 when sent **without** the flag; `get`/`post` take no such keyword (`TypeError`). A bearer containing CR/LF/NUL, whitespace,
  non-ASCII, DEL or a trailing newline, or paired with an `http://` URL, is rejected without echoing it; a header value with
  CR/LF/NUL, `\x0b`, DEL, non-ASCII, leading/trailing whitespace or empty, and a header name that is not an RFC 7230 token, are
  rejected without echoing; a non-`str` URL, a non-`Mapping` `headers`, an unsupported `params` type and a non-numeric
  `timeout` are `TypeError`s, a `timeout` of `nan`, `inf`, `10**400`, `0`, `-0.0`, a negative number, `3600.001` or `3601` a `ValueError` (exactly `3600` and `0.001` are accepted), a non-ASCII `bytes` `params` `unparseable params`, none echoing a
  caller value (one distinct-sentinel test per message, including a header name, a `params` object and a `timeout`).
  Known-gap controls (accepted today, pinned so a future tightening is deliberate): a secret in the URL path, a secret under an
  innocent key.
- **PH-G4 static call-site check.** The caller-module list is **derived**, not hand-written: every production file that
  imports `api.platform_http` (PH-G2's walk), asserted equal to the pinned four (`api/oauth.py`, `engine/publish/{youtube,
  instagram,metrics}.py`), so a new caller module is checked from its first commit or fails the pin. It reuses
  `tests/_seam_ast.py` (`parse`/`annotate` for `_parent` and `_class_of`, `import_bindings`, `dotted`, `Problem`, `_kw`) for
  alias resolution; that file's module-ownership helper is hard-wired to the C2 modules and `eval_str` needs `FileCtx` and
  `Project`, unions several assignments and does not resolve `self.`, so the plan lists what is **written new**: a
  module-binding multiplicity counter, a class-attribute resolver, a JoinedStr flattener, an authority splitter, the call-shape
  checker, the per-callee allowlist and PH-G2's non-callee walk. PH-G4 compares `dotted(node.func.value, bindings)` with
  `"api.platform_http"` itself, and a plain-name callee bound from `platform_http.get` (`x = platform_http.get; x(...)`) is
  counted too (PH-G2 forbids the capture). Every call that resolves to `platform_http.get/post/put` is checked, failing
  closed:
  - **Call shape.** The URL is the first positional argument or the `url=` keyword, exactly one of them; any second
    positional argument, any `*args` and any `**kwargs` spread fails (a keyword-only `bearer=`, `params=`, `headers=` ... is
    read by name).
  - **`params=`.** Absent, or a dict literal, a list-of-pairs literal or a `dict(k=v, ...)` call with only constant keywords
    (`dict(**x)` and `dict(pairs)` fail), none of whose constant keys normalises (with the module's own `_normalise`, the
    one private name the test imports; the secret *names* stay a literal list in the test) into a secret name; a
    non-constant key, a `**` spread, a name or any other expression fails.
  - **URL text**, resolved to a string, failing closed: a `Constant`; an f-string whose literal parts are text and whose every
    `{...}` hole is either (i) a module-level string `Name` (substituted) or (ii) one of the pinned id expressions, matched by
    the `ast.dump` of the whole expression and keyed per (file, function, expression): `page_id` (`oauth.py`, `InstagramOAuth.discover_account`),
    `ig_user_id` (`instagram.py`, `InstagramPublisher._create_container` and `InstagramPublisher._publish_container`), `creation_id` (`instagram.py`, `InstagramPublisher._wait_until_ready`) and `cut.platform_post_id` (`metrics.py`, `InstagramMetricsFetcher.fetch`), so `{other.platform_post_id}`,
    `{cut.reel_id}`, `{page_id.strip()}`, `{page_id + q}` and a pinned id used in the wrong function all fail. Every hole
    must have `conversion == -1` and `format_spec is None` (a nested hole inside a format spec, `{x!r}` and `{x:?<40}` fail).
    The literal or substituted text **before the first id hole already contains the `/` that ends the authority** (so
    `f"https://graph.facebook.com{page_id}/x"` and a substituted constant without a path both fail: an id could extend the
    host); a module-level string `Name` (`_UPLOAD_URL`, `_CAPTIONS_UPLOAD_URL`, `_GRAPH`, in a URL or in a hole); `self.<attr>`
    resolved to the **enclosing class body only** (`token_url`, `api/oauth.py:72,125`; the base class's annotation-only
    `token_url: str` at `:53` is not consulted and a bare `AnnAssign` is not an assignment). A module-level `Name` is valid only if the file has **exactly one binding** of it, a `Constant` string, counting every binding form (assign, augmented assign, annotated assign with a value, `for`, `with`, import, `def`, `class`, walrus, function parameter, `except ... as`, comprehension target, match capture, `del`, and a write under a `global` or `nonlocal` declaration), and no binding of the same name in the enclosing function scope; a class attribute is valid only with **exactly one value-binding in the enclosing class body** (annotation-only ignored, bases not consulted: the base class's `token_url: str` at `oauth.py:53` and the two subclass values at `:72` and `:125` therefore pass) and no store through `self.`, `cls.`, `ClassName.` or `setattr` anywhere in the file. The resolved text must start with `https://` and contain none of `?`, `;`, `#` (a query is only
    ever added through `params=`; no current site has one) and no `@` in its authority. Any other URL expression fails,
    except the single allowlisted `upload_url` Name, valid only as the URL of a `platform_http.put` call inside
    `youtube.py::YouTubePublisher.publish` (matched by file, function and callee, not by name).
  - **`bearer=`.** A call passing `bearer=` must resolve to a URL starting `https://` and have no `Authorization` key (any
    case) in a `headers=` dict literal.

  Non-vacuity, pinned by kind: of the 14 URLs, 2 are `Constant` (sites 5, 13), 5 are f-strings (6, 10, 11, 12, 14), 4 are
  `self.token_url` resolved to the class constant (1-4; with the base-class annotation present), 2 are module-level `Name`s (7,
  8) and 1 is the allowlisted `upload_url` (9); exactly six calls (sites 6, 7, 8, 11, 13, 14) carry a dict-literal `params=`;
  and exactly seven pass `bearer=` (5, 6, 7, 8, 11, 13, 14). Controls (each must fire): `params={"access_token": t}`,
  `params=[("access_token", t)]`, `params=dict(access_token=t)`, `params=dict(**x)`, `params={**base, ...}`, `params={k: t}`,
  `params=p`; URLs `f"{u}?access_token={t}"`, `f"{b}?{k}={t}"`, `f"{u}?access_{x}=1"`, `f"{base}access_token=1"`,
  `f"https://{h}/x"`, `f"{q}"`, `f"https://graph.facebook.com{page_id}/x"`, `f"{_GRAPH}/{page_id!r}"`,
  `f"{_GRAPH}/{page_id:{q}}"`, `f"{_GRAPH}/{other.platform_post_id}"`, `f"{_GRAPH}/{cut.reel_id}"`, a pinned hole used in the wrong
  function, `"...?accessToken=1"`, `"...;key=1"` (the `;` ban alone), `"...#x"` (the `#` ban alone), `"https://u:p@h/x"`;
  `u = f"{x}?access_token={t}"; platform_http.get(u)`; `platform_http.get(url=u)` with no positional URL; `platform_http.get(a, b)`;
  `platform_http.get(*args)`; `platform_http.get(url, **opts)`; `platform_http.post(upload_url, ...)` (the allowlist is for
  `put` only); a module `Name` assigned twice, bound by `+=`, written under `global`, or shadowed by a local; `self.token_url
  = ...` inside a method, `cls.token_url = ...`, a duplicate class attribute; a class-level `token_url =
  "https://x/y?client_secret=z"`; `bearer=` with an `http://` URL or with `headers={"authorization": ...}`; an aliased call.
  Also: a constant `"http://graph.facebook.com/x"` URL without `bearer=` fires; `ClassName.token_url = ...` and `setattr(Cls, "token_url", ...)` fire; two subclasses each defining `token_url` once pass and one class defining it twice fires; `def f(_GRAPH): platform_http.get(f"{_GRAPH}/{page_id}")` (a parameter shadow) fires; `put(upload_url, ...)` in another function and in another file fires; the multi-binding controls are parametrized over **every binding form the rule lists**, as a **literal list in the test file** (never derived from the implementation's own list), one synthetic snippet per form, for a module `Name` in the URL position and in the hole position and for the class body, whose forms are listed explicitly: assignment, augmented assignment, annotated assignment with a value, `def`, `class`, `for`, `with`, import, `del` and walrus; the `type X = ...` statement counts as a binding form, and a `from m import *` in a caller file fails closed (M163). Must not fire: `data={"access_token": t}`, `params={"fields": "access_token"}`. A secret that arrives through an allowed
  id hole (`https://.../a?access_token=` built from `page_id`) is statically invisible and rejected at runtime by rule 3(b):
  PH-G4 is a tripwire for the shapes above, not a completeness proof.
- **PH-G5 module hygiene.** `api/platform_http.py` imports only `httpx`, `re`, `urllib.parse`, `collections.abc` and
  `__future__`; it has no `import json`; it has no `from urllib.parse import ...` and no `from httpx import ...` (every call
  into those two modules is a module-attribute call, so a test's `monkeypatch.setattr(urllib.parse, ...)` and
  `monkeypatch.setattr(httpx, ...)` reach it; synthetic positive control for each); its public names (top-level defs and
  assignments not starting with `_` and not imports) are exactly `get, post, put, DEFAULT_TIMEOUT_S, UPLOAD_TIMEOUT_S,
  SECRET_QUERY_NAMES`.

## 5. Behavior preservation

- Each site sends `httpx` the same keyword set, the same URL (positional), the same header content and the same body as
  before (rules 1, 2, 5; pinned per site by the characterization test, §6.3). Among the caller-supplied headers the order is
  unchanged (Authorization first where it was first; sites 5, 6, 11, 13, 14 carry only that header).
- The exception types, messages and `.response` are unchanged (same `raise_for_status`), so `is_transient_error`,
  `should_retry`, `publish_cut`'s `max_retries=0`, the reaper and the credentials-router 502 text are unaffected.
- The only intended deltas, each on an input that did not occur in a healthy run, each pinned by a new test (commit 3), none
  in the characterization table: (a) a secret-named query key, a userinfo URL, a bearer+`Authorization` header collision,
  a bearer over a non-`https` URL or an unparseable URL now raises `ValueError` before the request (none exists in the tree;
  PH-G4 pins the secret-key, URL-text, `https`-with-bearer and header-literal shapes statically, the runtime guard covers the
  rest); (b) a **missing or malformed token**: `bearer` given as `None`/empty/non-`str`/with a control character
  now raises `ValueError("bearer must be a non-empty str")` instead of sending `Authorization: Bearer None` (and getting a
  401) or reaching h11. Reachable at the `bearer=` sites with a null input: 14, 8 and 13 (a null `token_blob`, directly or
  through `get_valid_access_token`; site 7 shares site 8's token and runs only after it succeeded; site 11 shares site 10's
  `access_token`, which Meta rejects first, so it is reachable only in theory), 6 (a `null` `access_token` in the remote
  `/me/accounts` JSON) and 5 (`credentials.py:85` assigns `long_lived.get("access_token", access_token)`, so a JSON `null`
  from the long-lived exchange reaches `discover_account(None)`). Both outcomes are non-retried failures and `_pull_one` and
  the credentials router catch the error either way, but the exception class and `job.error` text differ (`ValueError`, not
  `HTTPStatusError`); (c) a wrong-typed `url`, `headers`, `params` or `timeout` raises `TypeError`; a `timeout` outside `0 < t <= 3600`, a header name that is not an RFC 7230 token and a header value with CR/LF/NUL/`\x0b`/DEL/non-ASCII, leading or trailing whitespace or empty raise `ValueError` (no site passes one).
- Not changed: timeouts (30 and 300), redirects (not followed), call order, per-cut isolation in `pull_publish_metrics`, the
  `record_stage` composition in `youtube.py` and `metrics.py`.

## 6. Tests

### 6.1 Proof of a mechanical move

Same method as C2/C3: (1) characterization tests written first on the old code and passing unmodified after the move;
(2) a throwaway normalized-diff script over the six retargeted test files (docstrings and comments ignored, the seven
`<module>.httpx.<fn>` patch-string forms of §6.2 rewritten by that fixed table, `ast.dump` equality; within those six files
the total number of string constants matching `\.httpx\.(get|post|put)\b` goes 65 -> 0 and each maps by the table;
negative controls: an extra edit, a wrong-module string and a wrong-function string must each fail); (3) the mutation
battery of §6.4.

### 6.2 Retargets (65 strings in 6 files; generated from `git grep -nE "(api\.oauth|engine\.publish\.(youtube|instagram|metrics))\.httpx\." -- tests`)

| Old string | New | Count |
|---|---|---|
| `api.oauth.httpx.post` / `.get` | `httpx.post` / `httpx.get` | 21 / 2 |
| `engine.publish.metrics.httpx.get` | `httpx.get` | 13 |
| `engine.publish.youtube.httpx.post` / `.put` | `httpx.post` / `httpx.put` | 10 / 7 |
| `engine.publish.instagram.httpx.post` / `.get` | `httpx.post` / `httpx.get` | 7 / 5 |

That is seven forms. Files: `test_oauth.py` 5, `test_youtube_token_refresh.py` 19, `test_metrics_fetcher.py` 12,
`test_youtube_publisher.py` 11, `test_instagram_publisher.py` 12, `test_tasks_real_db.py` 6. The 14
`engine.generation.llm.httpx.*` strings in `test_llm_provider.py` are not touched. A patch of the global `httpx.post` was
already the effect of every old string (`module.httpx` is the `httpx` module object), so two old strings of the same
function in one test already patched one attribute: no new collision. The edits must land in the same commit as the
`import httpx` removal (the old strings would raise `AttributeError`). `tests/conftest.py::_api_calls_via_httpx` (the
sourcer seam) is unaffected. No existing test passes `None`, `""` or an unset `MagicMock` attribute as a token at the seven
bearer sites, nor a non-`str` URL or header value (checked: non-empty strings and `str(...)` lengths), so commit 3's deltas
break none.

### 6.3 New tests

**Commit 1 (before the move, green on the old code): `tests/test_platform_http_callsites.py`** plus a small shared helper
module `tests/_platform_http_helpers.py` (the recording fake and the network guard, imported by this file and by
`tests/test_platform_http.py`, so both are protected; the autouse fixture is imported the way
`tests/test_wikipedia_image_source.py:14` imports one, `from tests._platform_http_helpers import <fixture>  # noqa: F401`,
because a fixture is autouse only in the module namespace that holds it and CI lints with ruff `--select F,E9`). One
table-driven test per site (14 rows) driving the real caller.
Per-row tokens are distinct non-empty `str` sentinels. The recording fake replaces the bare `httpx.get/post/put`; it (i)
checks each call against the real function's signature (`inspect.signature(real).bind(url, **kw)`, so `get(data=...)` or an
unknown keyword fails), (ii) records `(method, url, kwargs)` in call order with the URL positional, and (iii) answers through
a real `httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)` using the same keywords, so the wire form is
real and the response a real `httpx.Response` with a request attached. An autouse fixture (in the helper, applied by both
files) makes `httpx.request`, `socket.socket.connect`, `socket.socket.connect_ex` and `socket.getaddrinfo` raise
`AssertionError` and appends every blocked call to a list that the fixture asserts empty at teardown, so a path that
bypasses the patched functions fails even where production code swallows the `AssertionError` (`_upload_captions`'s
best-effort `except Exception`, `_pull_one`'s per-cut handler) instead of reaching the network. The new files patch only
literal targets or use `monkeypatch.setattr(httpx, ...)`, because `tests/test_module_seams.py` G1 walks every test file and
fails a `patch(...)` whose target it cannot resolve. The guard is a plain generator `network_guard() -> Iterator[list]` (it installs the patches, yields the recording list and, on resumption, restores them in a `finally` and then asserts the list empty; its patches raise and record to their own list, never chaining to an outer guard, so the control can nest under the autouse one); the autouse fixture only wraps it. A control test drives it by hand: `gen = network_guard(); blocked = next(gen)`, triggers a blocked call and swallows the `AssertionError` as production code would, asserts `blocked` is non-empty, then `with pytest.raises(AssertionError): next(gen)` and asserts, with `is`, that `socket.getaddrinfo`, `socket.socket.connect`, `connect_ex` and `httpx.request` are back to the values they had before the first `next(gen)` (the control calls `gen.close()` in a `finally` if it fails earlier) (so the recording, the teardown assertion and the restoration are killed: M137, M164). The list is cleared in place, never rebound. For each row the **full literal wire request** is asserted from the
handler: method; full URL including the query string (compared through `request.url.params`, so encodings such as
`part=snippet%2Cstatus` do not matter); the caller-supplied headers by name and value (`Authorization`, `Content-Type`,
`X-Upload-Content-Type`, `X-Upload-Content-Length`, `Content-Length`; **no Authorization at all for site 9**), their relative
order checked on the recorded kwargs (httpx adds its own defaults first on the wire, and header-name case is not observable
through `httpx.Headers`); and the body (the form-encoded fields of sites 1-4, 10 and 12 key by key, the JSON of site 8
compared as parsed JSON, since httpx 0.28 changed the encoder's separators and CI floats inside `httpx>=0.27,<0.29`, the
content of sites 7 and 9). Also: the exact keyword-name set sent; `timeout` equal to the pinned float (`type(...) is
float`; `30.0`, `300.0` for site 9); no secret-named key in `params` or the URL, checked against the test file's **own
literal list** (`SECRET_QUERY_NAMES` does not exist until commit 3); and, for each of a 400, a 500 and a 302 answer (the 302
carries an absolute `Location: https://other.example/` and the test asserts the handler ran exactly once) with a body that is
not valid JSON, that `httpx.HTTPStatusError` propagates (so `raise_for_status` ran before the caller read the body, the old
code included).

Drive recipes: sites 1-4 call the provider methods; sites 5/6 `discover_account` with a two-page response; site 7
`_upload_captions` on a temp `.srt`, with `uuid.uuid4` patched (the multipart boundary); sites 8 and 9 `YouTubePublisher.publish` drives with `cut.subtitle_path = None`, a real-list `hashtags` (except in C) and a non-expired credential (no refresh call), run in **three variants** (plus two edge variants added during implementation: C-empty with `hashtags=[]`, and D with a `None` caption and `None` hashtags) so that every builder bound and literal is observable on the wire: **A**, a multi-line caption over 5000 characters whose first line is over 100 characters and whose distinct short second line makes `splitlines()[0]` observable, with at least 16 hashtags (kills the `[:100]` and `[:5000]` mutants and `tags[:15]`; the description's hashtag append is cut off here); **B**, a short multi-line caption and 16 or more short hashtags, asserting the exact description `caption + "\n\n" + "#h1 ... #h15"` and exactly 15 tags (kills the description-side `[:14]`/`[:16]` mutants and the `"\n\n"`, `" "` and `#` literals); **C**, a whitespace-only caption with `hashtags=None` (the `"Reel"` fallback; pins `tags == []` and the `or []`), and a variant with `hashtags=[]`. Title, description and tags are asserted as exact strings; the fake is keyed by method and path (ignoring the query string): the init POST returns an **absolute `https://` `Location`** (MockTransport requires one) and the PUT returns `{"id": ...}`; each failure variant is made to hit
the right request (site 8's at the POST, site 9's at the PUT), asserting POST then PUT and that the PUT URL equals the
`Location` value; sites 10-12 call the three `InstagramPublisher` methods, site 10 with a caption longer than 2200
characters (so the `caption[:2200]` bound is observable); site 13 `YouTubeMetricsFetcher.fetch` with a non-expired
credential; site 14 `InstagramMetricsFetcher.fetch` on an in-memory SQLite session with **real `Reel` and `Cut` rows** (the
shape `tests/test_metrics_fetcher.py`'s drift test builds with its own engine; with a `MagicMock` cut `record_stage` swallows
the failed commit and writes nothing), asserting that a 400 leaves a `StageEvent(ok=False)` (the request ran inside the
`record_stage` block). It patches the bare `httpx.*`, so it needs no edit after the move.

The transitional commit-2 behavior (an explicit `bearer=None` yields `Bearer None`) is **intentionally untested**: pinning it
in commit 1 would force an edit to that test in commit 3. Commit 3's tests pin only the final behavior (M11 and an omitted
`bearer` sending no header); `float(timeout)` (M38) and the type checks are commit 3 too (commit 2 forwards `timeout` as
given).

**Commit 3, `tests/test_platform_http.py`:** module behavior (rules 1-7 of §4.2, the three deltas of §5, defaults per verb
(`get`, `post`, `put` each with the constant), the two constants by value, the never-forwarded kwargs and a `TypeError` for
`auth=`/`cookies=` (checked with a bind-only recorder, not the Client-answering fake: `Client.request` rejects
`verify`/`trust_env`/`proxy` itself), the caller's `headers` dict and `params` object not mutated and `params` forwarded as the
same object, `put`'s `server_supplied_url` semantics including a behavioral test at site 9 that a session URL carrying
`?key=` still publishes, the response returned as is, `HTTPStatusError` text unchanged for 4xx, 5xx and 3xx, transport
errors not wrapped, `httpx.get` not captured at import) and PH-G1..PH-G5 with controls.

### 6.4 Mutation battery (apply, the named test must turn red, revert; the table goes into §10)

*Commit 1, on the old code, scripted and exhaustive:* each of the 14 `timeout` arguments removed (14), each of the 14
`raise_for_status()` calls removed (14), each of the 7 bearer headers removed (7), an Authorization header added to site 9
(1), and, for every literal in every call (each params value, data key, header value, JSON key, body fragment, slice bound)
and every variable operand (swapped with a same-type neighbour: `page_token`/`user_access_token` at sites 5-6, the
`client_id`/`client_secret` values, `ig_user_id`/`creation_id`), one value-changing mutant each. The generator mutates every literal in the enclosing function, in `_build_multipart_related`, and in the module and class constants a call reads (`_GRAPH`, `_UPLOAD_URL`, `_CAPTIONS_UPLOAD_URL`, `_METRICS`, `token_url`, the site-8 metadata such as `privacyStatus`); the **swap table** (`client_id`/`client_secret`/`redirect_uri`/`code` at sites 1-4, `page_token`/`user_access_token`/`page_id` at sites 5-6, `ig_user_id`/`creation_id` at sites 10-12, `access_token`/`video_id`) is enumerated in the plan, and the fixtures give those operands distinct values (otherwise a swap is an equivalent mutant); site 10's caption is at least 2201 characters. The count of the value mutants is recorded in §10. Every mutant must turn at least its own
row red; a mutant that is genuinely equivalent is named as such in §10 with the reason.

*Commit 3, on the module and its guards (M1-M171, M59 retired so 170 are active; the plan carries the map from each M-id to its test and assertion; M61a-h, M92a-c, M95a-b, M119a-c and M120a-c are variants):*

| Ids | Family and mutants |
|---|---|
| M1-M7 | status: drop `raise_for_status` in `get` / `post` / `put` (M1-M3); raise only on status >= 500 (M4); swallow 3xx (M5); return `resp.raise_for_status()` instead of `resp` (M6); swallow `HTTPStatusError` (M7) |
| M8-M15 | bearer: wrong format; ignored; truthiness (empty accepted); `None` accepted as `Bearer None`; non-`str` accepted; whitespace-only accepted; validated only after the request; the `ValueError` echoes the value |
| M16-M23 | headers: dropped when `bearer` is set; caller `Authorization` overrides `bearer`; collision check case-sensitive; collision check applied without `bearer`; caller dict mutated; Authorization not first; non-`Mapping` accepted; non-`str` key or value accepted |
| M24-M33 | forwarding: `params=None` always forwarded (M24); the normalised copy forwarded instead of the original (M25); caller `params` mutated (M26); `data` dropped in `post` (M27); `content` dropped in `put` (M28); `json` dropped in `post` (M29); `data`/`headers`/`json`/`content=None` always forwarded (M30); URL passed as `url=` (M31); `follow_redirects=True` added (M32); `verify`/`trust_env`/`auth`/`cookies` forwarded (M33) |
| M34-M41 | timeouts: wrong default in only `get` / `post` / `put` (M34-M36); `put` ignores a given `timeout` (M37); `int` forwarded as `int` (M38); `timeout` not forwarded (M39); `None` accepted (M40); a constant changed in both places (M41) |
| M42-M63 | guard: removed for `params` (M42) / for the URL query (M43); case-sensitive in only params (M44) / only the URL query (M45); first key only (M46); non-alphanumeric stripping removed (M47); `;` splitting removed (M48); userinfo check removed (M49); any `@` in the URL treated as userinfo (M50); a name dropped from the constant (M51, caught by the literal pin); substring instead of exact match (M52); values matched instead of keys (M53); `data=` also blocked (M54); guard runs after the request (M55); error text includes the value (M56) / the URL (M57) / omits the matched name (M58); M59 retired (the raw-key truncation path no longer exists; superseded by M156); `unquote_plus` removed (M60); one input form unscanned (M61a-h: `QueryParams`, list of pairs, `str`, `bytes`, `?`-prefixed `str`, bare key, `None` value, list value); the `urlsplit` error text leaks (M62); a non-`str` URL accepted (M63) |
| M64-M69 | `server_supplied_url`: ignored (M64); the `params=` ban removed under the flag, so a non-secret `params={"part": "x"}` is accepted (M65); also skips userinfo (M66); forwarded to `httpx` (M67); default `True` (M68); accepted by `get`/`post` (M69) |
| M70 | `post` calls `httpx.request` |
| M71-M80 | guards: a site reverted to a raw `httpx.get` (PH-G1(a), M71); a from-import of `get` (PH-G2, M72); a secret-named literal key at a site (PH-G4, M73); a `?access_token=` URL constant (PH-G4, M74); a non-literal `params=` (PH-G4, M75); `u = f"...?access_token="; get(u)` (PH-G4, M76); `server_supplied_url=flag` (PH-G2, M77); `import json` in the module (PH-G5, M78); an extra import in the module (PH-G5, M79); a second, non-allowlisted file with a raw `httpx` request call (PH-G1(a), M80) |
| M81-M91 | r4 additions, validation: `server_supplied_url=True` dropped at site 9 (M81, the `?key=` session-URL test); a bearer with CR/LF/NUL accepted (M82); a bearer accepted over a non-`https` URL (M83); a header value with CR/LF/NUL accepted (M84); the non-`str` URL `TypeError` echoes the object (M85); a guard error raised inside the `except` block, so `__context__` carries the original (M86); keys read via `Mapping.keys()` instead of `httpx.QueryParams` (M87); an unsupported `params` type accepted (M88); `_normalise` decodes once (M89); a `bool` timeout accepted (M90); an `httpx.Timeout` accepted (M91) |
| M92-M98 | r4 additions, forwarding: `params` dropped in one verb (M92a-c); `data` dropped in `put` (M93); `content` dropped in `post` (M94); `get` / `put` call `httpx.request` (M95a-b); caller headers forwarded as a copy without `bearer`, or their key case lost (M96); transport errors wrapped (M97); `httpx.post` captured at import time (M98) |
| M99-M106 | r4 additions, guards: `.raise_for_status` in a caller (PH-G1(b), M99); `import requests` in a caller (PH-G1(c), M100); a new public name in `platform_http` (PH-G5, M101); a call-level `**opts` (PH-G4, M102); an aliased call left out of PH-G4's count (PH-G4, M103); `post(upload_url)` accepted by the allowlist (PH-G4, M104); a class-level `token_url` with a `?` (PH-G4, M105); a URL f-string with a hole after `?` (PH-G4, M106) |
| M107-M120 | r5 additions, validation and forwarding: an unparseable URL swallowed, the request proceeds (M107); unparseable `params` swallowed (M108); `server_supplied_url=True` with `bearer=` accepted (M109); a header value with leading/trailing whitespace or `\x0b` accepted (M110); an illegal header name accepted (M111); the percent-decode capped below the fixed point and not rejecting at the cap, fail open (M112); `re.match` instead of `re.fullmatch` for the bearer, so a trailing newline passes (M113); a raised message echoes a caller-supplied header or `params` value (M114); an omitted `bearer` sends an Authorization header (M115); the guard message lacks the `bearer=` hint (M116); a bare key in the URL query not scanned (M117); `json` dropped in `put` (M118); `headers` dropped in one verb (M119a-c); `timeout` dropped in one verb (M120a-c) |
| M121-M136 | r5 additions, guards: the PH-G1 allowlist inverted to a blacklist, so an unknown `httpx` name passes (PH-G1(a), M121); the `import httpx as h` form unresolved (PH-G1(a), M122); the `from httpx import post as p` form unresolved (PH-G1(a), M123); `server_supplied_url=True` at a second site or function (PH-G2, M124); a constant read at module level (PH-G2, M125); a non-allowlisted f-string hole accepted (PH-G4, M126); a hole inside the authority accepted (PH-G4, M127); a URL passed as `url=` skipped (PH-G4, M128); an extra positional or `*args` accepted (PH-G4, M129); a module `Name` assigned twice accepted (PH-G4, M130); a `self.token_url = ...` assignment or a duplicate class attribute accepted (PH-G4, M131); a constant URL with userinfo accepted (PH-G4, M132); `bearer=` with an `http://` URL accepted (PH-G4, M133); an `Authorization` key in a `headers=` literal with `bearer=` accepted (PH-G4, M134); the caller-module list hand-written, a new caller unchecked (PH-G4, M135); the PH-G1 walk finding nothing in the allowed files (PH-G1 non-vacuity, M136) |
| M137-M156 | r6 additions: the network-guard teardown assertion or recording dropped (M137); the bearer regex widened to admit non-ASCII, DEL or interior whitespace (M138); an empty header value accepted (M139); a message echoes a header name or a `timeout` value (M140); a `platform_http` function captured by an alias `x = platform_http.get` (PH-G2, M141); `put` with both `server_supplied_url=True` and `bearer=` not flagged (PH-G2, M142); the `upload_url` allowlist matched by name only (PH-G4, M143); the `;` ban (PH-G4, M144) and the `#` ban (PH-G4, M145) individually removed; the PH-G4 resolver returning nothing, caught by the per-kind counts (PH-G4, M146); `import requests as r` and `from http import client` unresolved (PH-G1(c), M147); `server_supplied_url=True` with any header other than `Content-Type`/`Content-Length`, or with `params=`, accepted (rule 3, PH-G3, M148); an id hole directly after the authority without a `/` accepted (PH-G4, M149); a nested `format_spec` or a `conversion` hole accepted (PH-G4, M150); a pinned id matched by attribute name only, so `other.platform_post_id` passes (PH-G4, M151); a hole `Name` with two bindings, or bound by `+=`, `global` or a local shadow, accepted (PH-G4, M152); `cls.token_url = ...` or `setattr` on the class accepted (PH-G4, M153); a `timeout` of `nan`, `inf` or `10**400` accepted, or `float()` applied before the range check, so a raw `OverflowError` escapes (M154); a URL with leading or trailing whitespace accepted (M155); the guard message printing the raw key instead of the constant name (M156) |
| M157-M161 | r7 additions: `<= 3600` weakened to `< 3600` (M157); `0 <` weakened to `0 <=` (M158); a non-ASCII `bytes` `params` accepted (M159); PH-G2's `headers=`-literal check removed, so `put(..., server_supplied_url=True, headers={"Cookie": ...})` is accepted (PH-G2, M160); a `from urllib.parse import urlsplit` or `from httpx import QueryParams` in the module, which the fail-open tests could not patch (PH-G5, M161) |
| M162-M167 | r8 additions: the `server_supplied_url` header allowlist replaced by a credential-name denylist (M162); one listed binding form ignored by the PH-G4 resolver (PH-G4, M163); the network guard never restoring its patches (§6.3, M164); a constant `http://` URL accepted by PH-G4 without `bearer=` (PH-G4, M165); PH-G1's non-request allowlist widened with `Client`, `AsyncClient` or `request` (PH-G1(a), M166); a class attribute counted per file instead of per enclosing class body, which fails sites 1-4 on the real tree (PH-G4, M167) |
| M168-M171 | r9 additions, the `server_supplied_url` allowlist: `if params:` instead of `params is not None`, so `{}` is accepted (M168); the case-fold duplicate-key rejection removed (M169); a case-sensitive header-name comparison, rejecting `content-type` and `CONTENT-LENGTH` (M170); a `_normalise`-based header-name comparison, accepting `ContentType` and `Content_Type` (M171) |

Equivalent mutants are named as such in §10 with the reason, not silently dropped.

## 7. Risks and known gaps

- **Not size-capped, deadline-bounded or redirect-checked.** Platform calls keep httpx's per-read timeouts only, as before. A
  hostile or broken host could trickle a response. Almost every host is a fixed constant and the caller is a trusted worker,
  so this is recorded, not addressed (Q3). **Exception: the YouTube upload URL (site 9) is server-chosen** (the `Location`
  header of the init response). The video bytes, and no credential (no Authorization is sent), would go to whatever host
  Google's response names, with a 300 s timeout and no redirect following. No host allowlist is added here: the live host
  shape is not verified (tests use `https://upload.example.com/session123`) and a wrong allowlist would break publishing;
  a follow-up could pin `https` and `*.googleapis.com` after one real call.
- **A non-https `Location`.** The session URL is accepted for the video PUT whatever its scheme (no credential is sent, but the video body would travel over cleartext to a server-chosen host); a scheme check is part of the unverified host allowlist above.
- **A hand-written `Authorization` header without `bearer=`** is accepted by the runtime guard (rule 2 only checks a collision when `bearer=` is also given); the static check PH-G4 flags it only at literal call sites in the four caller modules.
- **`upload_url` in error text** (Q10): `HTTPStatusError.__str__` includes it, hence `job.error`. It is an upload capability,
  not a stored credential, and expires. Not redacted. Site 9's absence of `Authorization` is pinned.
- **The guard is name-based and query-only.** It does not see a secret under an innocent key (`?x=<token>`), a name outside
  the 37 (a new vendor spelling), a secret in the URL path (`/v1/<token>/media`) or a fragment, headers, or error response
  bodies; the path and innocent-key cases have "accepted today" controls (PH-G3). It over-splits (`&` and `;`) and
  over-decodes, which is the safe direction. Value-matching was rejected (false positives on short test tokens, no help for
  non-bearer secrets). Exact match on normalised names was kept over a suffix rule, which would reject legitimate
  `pageToken`/`monkey`.
- **Remote-derived ids in URLs.** Sites 6, 11 and 14 interpolate ids from remote JSON (`page_id`, `creation_id`,
  `platform_post_id`), and sites 10 and 12 the stored `ig_user_id` (originally from remote JSON), into the URL path;
  `HTTPStatusError.__str__` includes that path (not secret). An id containing `?` followed by a secret-named pair (for
  example `?access_token=`) makes the guard raise `ValueError` (fail loud, a new exception class at those sites); `?x=1` or
  an `@` in the path is accepted (PH-G3), an id at the very end of the URL (sites 6 and 11) with trailing whitespace makes the guard raise `unparseable URL` before the request (interior whitespace, at sites 10, 12 and 14, is accepted), and an id cannot change the host because the host prefix is a constant.
- **PH-G4 and the caller list.** A fifth caller module of `platform_http` is picked up automatically (PH-G4's list is derived from PH-G2's walk) and fails the "equals the pinned four" assertion until it is deliberately added; a module that bypasses `platform_http` is blocked by PH-G1 from using raw `httpx`. PH-G1/G2/G4 are AST tripwires, not proofs: they do not see `getattr(httpx, "post")`, `importlib`,
  `__import__` or a dynamically built module name. Same limit as C3's G-C.
- **Third-party error text.** The "no message raised by the module contains caller text" claim covers only messages the module raises. What httpx, h11 or the stdlib raise on input that passes the module's validation is outside it: an `httpx.InvalidURL` quoting a bad port, a `UnicodeEncodeError` naming a lone surrogate in `data`, `params` values or the URL path. None carries a stored credential at today's sites, and the guard rejects the header and bearer shapes h11 would echo.
- **Non-ASCII header values** (for example `filename="café.mp4"`) are rejected on purpose; no site sends one.
- **Sites 10 and 12 send `access_token` in a form body**, which Meta also accepts as a bearer header. A body is not a URL,
  so it is allowed (§4.2 rule 3), but the header is the safer shape; changing the wire form is a behavior change for another PR.
- **`SECRET_QUERY_NAMES` includes `code`, `key`, `token`, `sig`, `auth`.** Correct for every current site (OAuth `code` is a
  POST body field; Google `key=` is unused). A future legitimate query parameter with one of those names must be sent
  another way or the list reviewed; the guard fails loudly rather than leaking silently, which is the intent. An
  escape-hatch parameter (`allow_query=`) was considered and rejected: no site needs it, and it would be the first thing
  anyone reaches for to silence the guard.
- **`asset_sourcer.py` still carries `timeout=30.0` literals** and its own `raise_for_status` calls: they are the sourcers'
  request to `safe_fetch` (C2) and are allowlisted in PH-G1(b).
- **Rolling deploy.** Pure code change: no schema, no config. Workers do not hot reload; restart them. Rollback is a code
  revert.

## 8. Documentation to update (every file below is edited in commit 4; grep the touched names and fix every hit)

- `CLAUDE.md`: Module layout (an `api/platform_http.py` entry; the `oauth.py`, `youtube.py`, `instagram.py`, `metrics.py`
  entries where they mention raw `httpx`, `timeout=30.0` or Bearer headers); Key conventions ("Token-in-URL is a
  logging/DB leak" now names the enforcing module and guards; a "Platform calls go through `platform_http`" bullet with the
  deltas of §5 and the known gaps of §7); the `tests/` list (`test_platform_http.py`, `test_platform_http_callsites.py`, the
  helper module, and the patch-target and count entries of the six retargeted files, if any entry names an old
  `<module>.httpx.*` string); the Build-phase paragraph (Phase 8i); the Commands test-count comment; the "Dependency caps" bullet (the `httpx<0.29` cap now also protects `platform_http`'s reliance on the module-level `httpx.get/post/put` signatures, and is why the site-8 test compares parsed JSON rather than raw bytes).
- `CONTEXT.md`: a "Platform HTTP" section opening "Surfaced via the C4 architecture-review grilling session (Phase 8i,
  `docs/specs/2026-11-platform-http-module-design.md`)", with capitalised entries **Platform call**, **Secret query guard**,
  **Bearer**, **Upload timeout**, **Server-supplied URL**.
- `README.md:347` ("3160 tests") and any other count; `docs/architecture.md`: the `api/` module table (the `oauth.py` row
  and a new `platform_http.py` row), the `engine/publish/` table, the "79 test files ... 3160 tests" line (~:199: 79 ->
  81 test files, the "two helper modules" parenthetical -> three, and the same file count at `CLAUDE.md:51`), the per-file test
  table (~:201-242, two new files), the Notable-coverage prose (~:244, a clause for the new files) and the publishing prose
  (~:576-578, a one-line pointer to `platform_http`); `docs/prd.md:226`: the bullet "A credential token is never placed in a URL query string — always an
  `Authorization: Bearer` header or a POST body" is extended to name `api/platform_http.py` as its enforcement (the wording is
  already true); `docs/design.md`, `docs/api.md`, `docs/roadmap.md`: grep `httpx|raise_for_status|timeout=30|Bearer|api/oauth`
  and fix hits (a grep at spec time found none that need edits).
- `tests/_seam_oracle.py` is unaffected (these names never lived on `asset_sourcer`). Counts come from
  `pytest --collect-only`, not from this spec.

## 9. Open questions

None at the design level. Implementation-time choices left to the plan: the `ValueError`/`TypeError` wording beyond what rules
2, 3 and 7 fix (tests assert the matched constant name's presence, the `bearer=` substring and the raw key's, value's and URL's absence; the unparseable messages are asserted equal to their full text), and the helper layout inside `tests/test_platform_http_callsites.py`.

## 10. Commit plan and implementation record

Plan: `docs/specs/2026-11-platform-http-implementation-plan.md` (written once this spec's review is clean).

1. `test: pin the platform call sites before moving them`: `tests/test_platform_http_callsites.py` and
   `tests/_platform_http_helpers.py`, green on the old code; the scripted old-code mutants of §6.4, each must turn a row red.
2. `refactor: route platform API calls through api/platform_http.py`: the module per R7 (forward-only-supplied kwargs, the
   `_UNSET` bearer sentinel with `Bearer {value}` unvalidated, the two timeout constants, positional URL,
   `raise_for_status`; no guard, no validation, no `server_supplied_url`), the 14 call sites (site 9 without
   `server_supplied_url`), import removals, the 65 retargets, normalized-diff output, the unmodified commit-1 test as proof.
   No new tests in this commit.
3. `test: pin platform_http's rules and confine raw httpx`: the guard, the bearer, header and type validation and
   `server_supplied_url` added to the module (and `server_supplied_url=True` to the site-9 call) together with
   `tests/test_platform_http.py` (module behavior, PH-G1..PH-G5) and the mutation battery M1-M171.
4. `docs: document platform_http and record the evidence`: §8 and this section's record.

Implementation record (filled in at implementation; every number below was re-run by the controller, not taken from
an implementer's report):

- **Baseline** at `1b49607`: 3160 selected (3157 passed, 3 skipped, 1 deselected), 79 test files. Final (after the code-review fix commit): 4327 selected, 4328
  total with the golden test, 81 test files (`tests/test_platform_http.py` 1090 tests, 1084 at the commit-3 snapshot the
  mutation tables below were run against, `tests/test_platform_http_callsites.py` 77 tests, plus the helper
  `tests/_platform_http_helpers.py`); full suite 4324 passed, 3 skipped, 1 deselected; ruff
  `--select F,E9` clean.
- **Commit 1** (`6923313`): the characterization test, 77 tests, green on the old code. Scripted old-code mutation: 301
  mutants, 301 killed, 0 survived (families: auth-added 1, auth-removed 7, literal 188, raise_for_status 14, swap 71, swap-extra 6, timeout 14),
  every kill a pytest failure; no equivalent mutants. The first pass left three survivors in `YouTubePublisher.publish`
  (the `""` after `caption or` in the title and description lines, and the log message literal), closed by drive variant D (a
  `None` caption with `None` hashtags) and an exact-equality check on the log record.
- **Commit 2** (`c531b64`): the move. T1's tests passed unmodified; the normalized-AST diff passed on all six files (65 of 65
  strings rewritten, 0 old matches left; self-test: a correct rewrite passes, and an extra edit, a wrong module, a wrong function
  and an unrewritten string are each detected); the test node-id set is the baseline plus exactly the 77 new tests (0 removed).
- **Commit 3** (`856fa54`): module mutation battery: 143 mutants, 140 killed, 3 equivalent survivors
  (M61e2, M155b, M155c: a leading `?` not dropped is equivalent because `_normalise` strips non-alphanumerics, and the two
  whitespace-URL conditions of rule 3 are redundant with each other). Guard battery: 120 mutants, 119 red,
  1 green (M164b, an extra that restores with `setattr` only: equivalent, no `is` assertion can tell the
  difference). M59 is retired. The full per-mutant results follow (first failing test per mutant; the plan's map is the intent,
  this is the observed result).

<details><summary>Module mutants (M-ids as in section 6.4; variants lettered)</summary>

| Mutant | Result | First failing test (tests/test_platform_http.py) |
|---|---|---|
| M1 | killed | test_raise_for_status_per_verb[get] |
| M2 | killed | test_raise_for_status_per_verb[post] |
| M3 | killed | test_raise_for_status_per_verb[put] |
| M4 | killed | test_status_variants[400-get] |
| M5 | killed | test_status_variants[301-get] |
| M6 | killed | test_returns_resp_not_raise_result[get] |
| M7 | killed | test_raise_for_status_per_verb[get] |
| M8 | killed | test_bearer_format_and_first_header[get] |
| M9 | killed | test_bearer_format_and_first_header[get] |
| M10 | killed | test_bearer_rejected_before_any_request[''-get] |
| M11 | killed | test_bearer_rejected_before_any_request[None-get] |
| M12 | killed | test_bearer_rejected_before_any_request[None-get] |
| M13 | killed | test_bearer_rejected_before_any_request[''-get] |
| M14 | killed | test_bearer_rejected_before_any_request[None-get] |
| M15 | killed | test_bearer_rejected_before_any_request[None-get] |
| M16 | killed | test_bearer_format_and_first_header[get] |
| M17 | killed | test_bearer_header_collision_any_case[get] |
| M18 | killed | test_bearer_header_collision_any_case[get] |
| M19 | killed | test_headers_authorization_without_bearer_is_accepted_but_still_validated[get] |
| M20 | killed | test_bearer_format_and_first_header[get] |
| M21 | killed | test_bearer_format_and_first_header[get] |
| M22 | killed | test_headers_must_be_a_mapping_of_str[get] |
| M23 | killed | test_type_checks_no_echo[get] |
| M24 | killed | test_forwards_only_supplied[get] |
| M25 | killed | test_forwards_only_supplied[get] |
| M26 | killed | test_forwards_a_falsy_value_that_is_not_none[get] |
| M27 | killed | test_forwards_only_supplied[post] |
| M28 | killed | test_forwards_only_supplied[put] |
| M29 | killed | test_forwards_only_supplied[post] |
| M30 | killed | test_raise_for_status_per_verb[get] |
| M31 | killed | test_raise_for_status_per_verb[get] |
| M32 | killed | test_status_variants[301-get] |
| M33 | killed | test_raise_for_status_per_verb[get] |
| M34 | killed | test_timeout_default_per_verb[get] |
| M35 | killed | test_timeout_default_per_verb[post] |
| M36 | killed | test_timeout_default_per_verb[put] |
| M37 | killed | test_timeout_override_is_forwarded_as_a_float[put] |
| M38 | killed | test_timeout_override_is_forwarded_as_a_float[get] |
| M39 | killed | test_forwards_only_supplied[get] |
| M40 | killed | test_timeout_type_rejected[None-get] |
| M41 | killed | test_public_surface_constants |
| M41b | killed | test_public_surface_constants |
| M42 | killed | test_guard_params_forms[get] |
| M43 | killed | test_guard_url_query_forms[get] |
| M44 | killed | test_guard_params_spellings[get] |
| M45 | killed | test_guard_url_query_spellings[get] |
| M46 | killed | test_guard_params_forms[get] |
| M47 | killed | test_secret_names_literal_pin |
| M48 | killed | test_guard_params_forms[get] |
| M49 | killed | test_guard_userinfo[get] |
| M50 | killed | test_guard_accepts_the_real_call_params_and_near_misses[get] |
| M51 | killed | test_secret_names_literal_pin |
| M52 | killed | test_guard_params_forms[get] |
| M53 | killed | test_guard_params_forms[get] |
| M54 | killed | test_guard_does_not_inspect_bodies_or_headers[post] |
| M55 | killed | test_guard_params_forms[get] |
| M56 | killed | test_guard_whitespace_around_the_url_is_unparseable[ |
| M57 | killed | test_guard_userinfo[get] |
| M58 | killed | test_guard_params_forms[get] |
| M60 | killed | test_guard_params_spellings[get] |
| M61a | killed | test_guard_params_forms[get] |
| M61b | killed | test_guard_params_forms[get] |
| M61c | killed | test_guard_params_forms[get] |
| M61d | killed | test_guard_params_forms[get] |
| M61e | killed | test_guard_params_forms[get] |
| M61e2 | SURVIVED |  |
| M61f | killed | test_guard_params_forms[get] |
| M61g | killed | test_guard_params_forms[get] |
| M61h | killed | test_guard_params_forms[get] |
| M62 | killed | test_unparseable_url[get] |
| M63 | killed | test_type_checks_no_echo[get] |
| M64 | killed | test_server_supplied_url_skips_only_the_url_query_scan |
| M65 | killed | test_server_supplied_url_rejects_any_params[params0] |
| M66 | killed | test_server_supplied_url_still_rejects_userinfo |
| M67 | killed | test_raise_for_status_per_verb[put] |
| M68 | killed | test_bearer_format_and_first_header[put] |
| M69a | killed | test_get_takes_no_body_keywords |
| M69b | killed | test_get_takes_no_body_keywords |
| M70 | killed | test_raise_for_status_per_verb[post] |
| M81 | killed | test_server_supplied_url_site9_publishes_with_a_secret_looking_session_query[https://upload.example.com/s/abc?upload_id=U1&key=K2&upload_protocol=resumable] |
| M82 | killed | test_bearer_rejected_before_any_request['\\n'-get] |
| M83 | killed | test_bearer_requires_https[http://api.example/x-get] |
| M84 | killed | test_headers_authorization_without_bearer_is_accepted_but_still_validated[get] |
| M85 | killed | test_type_checks_no_echo[get] |
| M86 | killed | test_unparseable_url[get] |
| M87 | killed | test_guard_accepts_the_real_call_params_and_near_misses[get] |
| M88 | killed | test_type_checks_no_echo[get] |
| M89 | killed | test_guard_params_spellings[get] |
| M90 | killed | test_timeout_type_rejected[True-get] |
| M91 | killed | test_timeout_type_rejected[bad7-get] |
| M92a | killed | test_forwards_only_supplied[get] |
| M92b | killed | test_forwards_only_supplied[post] |
| M92c | killed | test_forwards_only_supplied[put] |
| M93 | killed | test_forwards_only_supplied[put] |
| M94 | killed | test_forwards_only_supplied[post] |
| M95a | killed | test_raise_for_status_per_verb[get] |
| M95b | killed | test_raise_for_status_per_verb[put] |
| M96a | killed | test_bearer_omitted_sends_no_authorization[get] |
| M96b | killed | test_bearer_omitted_sends_no_authorization[get] |
| M96c | killed | test_headers_with_bearer_are_a_new_dict_and_the_caller_dict_is_untouched[get] |
| M97 | killed | test_transport_errors_not_wrapped[get] |
| M97b | killed | test_transport_errors_not_wrapped[get] |
| M98 | killed | test_raise_for_status_per_verb[post] |
| M98b | killed | test_raise_for_status_per_verb[get] |
| M98c | killed | test_raise_for_status_per_verb[put] |
| M107 | killed | test_unparseable_url[get] |
| M108 | killed | test_unparseable_params[get] |
| M109 | killed | test_server_supplied_url_rejects_a_bearer |
| M110 | killed | test_headers_value_must_fullmatch_printable_ascii[ |
| M111 | killed | test_headers_name_must_be_an_rfc7230_token[ |
| M112 | killed | test_guard_params_spellings[get] |
| M112b | killed | test_normalise_fixpoint |
| M112c | killed | test_guard_variants[get] |
| M113 | killed | test_bearer_rejected_before_any_request['a |
| M114a | killed | test_headers_value_must_fullmatch_printable_ascii[-get] |
| M114b | killed | test_type_checks_no_echo[get] |
| M115 | killed | test_bearer_omitted_sends_no_authorization[get] |
| M116 | killed | test_guard_params_forms[get] |
| M117 | killed | test_guard_url_query_forms[get] |
| M118 | killed | test_forwards_only_supplied[put] |
| M119a | killed | test_bearer_format_and_first_header[get] |
| M119b | killed | test_bearer_format_and_first_header[post] |
| M119c | killed | test_bearer_format_and_first_header[put] |
| M120a | killed | test_forwards_only_supplied[get] |
| M120b | killed | test_forwards_only_supplied[post] |
| M120c | killed | test_forwards_only_supplied[put] |
| M148a | killed | test_server_supplied_url_rejects_any_params[params0] |
| M148b | killed | test_server_supplied_url_rejects_a_bearer |
| M154a | killed | test_timeout_range_rejected_before_any_float_conversion[10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000-get] |
| M154b | killed | test_timeout_range_rejected_before_any_float_conversion[nan-get] |
| M154c | killed | test_timeout_range_rejected_before_any_float_conversion[inf-get] |
| M155 | killed | test_guard_whitespace_around_the_url_is_unparseable[ |
| M155b | SURVIVED |  |
| M155c | SURVIVED |  |
| M156 | killed | test_guard_params_forms[get] |
| M157 | killed | test_timeout_override_is_forwarded_as_a_float[get] |
| M158 | killed | test_timeout_range_rejected_before_any_float_conversion[0-get] |
| M159 | killed | test_unparseable_params_real_smokes[get] |
| M162 | killed | test_server_supplied_url_header_allowlist[X_Goog_Api_Key] |
| M168 | killed | test_server_supplied_url_rejects_any_params[params0] |
| M169 | killed | test_server_supplied_url_duplicate_names_after_case_folding |
| M170 | killed | test_put_flag_is_never_forwarded |
| M171 | killed | test_server_supplied_url_header_allowlist[ContentType] |

</details>

<details><summary>Guard mutants</summary>

| Mutant | Result | First failing test (tests/test_platform_http.py) |
|---|---|---|
| M71 | red | test_ph_g1_real_tree_is_confined |
| M71c | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[Client] |
| M72 | red | test_ph_g2_real_tree_is_clean |
| M72c | red | test_ph_g2_control_every_forbidden_import_form_fires[from |
| M73 | red | test_ph_g4_real_tree_is_clean |
| M74 | red | test_ph_g4_real_tree_is_clean |
| M75 | red | test_ph_g4_real_tree_is_clean |
| M76 | red | test_ph_g4_real_tree_is_clean |
| M77 | red | test_ph_g2_real_tree_is_clean |
| M77c1 | red | test_ph_g2_control_every_bad_server_supplied_url_shape_fires[platform_http.put(u, |
| M77c2 | red | test_ph_g2_control_every_bad_server_supplied_url_shape_fires[platform_http.put(u, |
| M78 | red | test_ph_g5_the_real_module_is_clean |
| M78c | red | test_ph_g5_control_imports_fire[import |
| M79 | red | test_ph_g5_the_real_module_is_clean |
| M79c | red | test_ph_g5_control_imports_fire[import |
| M80 | red | test_ph_g1_real_tree_is_confined |
| M99 | red | test_ph_g1_real_tree_is_confined |
| M99c | red | test_ph_g1_control_raise_for_status_only_in_the_request_modules |
| M100 | red | test_ph_g1_real_tree_is_confined |
| M100c | red | test_ph_g1_control_other_http_clients_fire_in_every_form[import |
| M101 | red | test_ph_g5_the_real_module_is_clean |
| M101c | red | test_ph_g5_control_a_new_public_name_fires[def |
| M102 | red | test_ph_g4_real_tree_is_clean |
| M102c | red | test_ph_g4_control_call_shape_fires[platform_http.get(_GRAPH, |
| M103c | red | test_ph_g4_control_aliased_callees_are_counted_and_checked |
| M104c | red | test_ph_g4_control_upload_url_is_allowlisted_only_for_put_in_publish |
| M104p | red | test_ph_g2_real_tree_is_clean |
| M105 | red | test_ph_g4_real_tree_is_clean |
| M106 | red | test_ph_g4_real_tree_is_clean |
| M121 | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[HTTPTransport] |
| M122 | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[Client] |
| M123 | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[Client] |
| M124c | red | test_ph_g2_control_a_valid_shape_in_the_wrong_place_fires |
| M124p | red | test_ph_g2_real_tree_is_clean |
| M125c | red | test_ph_g2_control_the_constants_are_reads_inside_a_function_only[from |
| M125p | red | test_ph_g2_real_tree_is_clean |
| M126a | red | test_ph_g4_control_bad_urls_fire[f"{_GRAPH}/{other.platform_post_id}"-None] |
| M126b | red | test_ph_g4_real_tree_is_clean |
| M127 | red | test_ph_g4_control_bad_urls_fire[f"https://graph.facebook.com{ig_user_id}/x"-authority] |
| M128 | red | test_ph_g4_control_clean_baselines_pass |
| M129a | red | test_ph_g4_control_call_shape_fires[platform_http.get(u, |
| M129b | red | test_ph_g4_control_call_shape_fires[platform_http.get(*args)-*args] |
| M130 | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[assignment-url] |
| M131a | red | test_ph_g4_control_stores_through_self_cls_classname_or_setattr_fire[self.token_url |
| M131b | red | test_ph_g4_control_a_second_binding_in_the_class_body_fires[assignment] |
| M132 | red | test_ph_g4_control_bad_urls_fire["https://u:p@graph.facebook.com/x"-userinfo] |
| M133 | red | test_ph_g4_control_upload_url_is_allowlisted_only_for_put_in_publish |
| M134 | red | test_ph_g4_control_bearer_needs_an_https_url_and_no_authorization_header |
| M135 | red | test_ph_g4_caller_list_is_derived_and_pinned |
| M136 | red | test_ph_g1_real_tree_non_vacuity |
| M137a | red | test_network_guard_control |
| M137b | red | test_network_guard_control |
| M138a | red | test_bearer_rejected_before_any_request['tok\\x7f'-get] |
| M138b | red | test_bearer_rejected_before_any_request['tok\\x7f'-get] |
| M138c | red | test_bearer_rejected_before_any_request[' |
| M139 | red | test_headers_value_must_fullmatch_printable_ascii[-get] |
| M140a | red | test_headers_name_must_be_an_rfc7230_token[-get] |
| M140b | red | test_headers_value_must_fullmatch_printable_ascii[-get] |
| M140c | red | test_timeout_messages_do_not_echo |
| M141c | red | test_ph_g2_control_captures_and_non_callee_uses_fire[x |
| M141p | red | test_ph_g2_real_tree_is_clean |
| M142c | red | test_ph_g2_control_every_bad_server_supplied_url_shape_fires[platform_http.put(u, |
| M142p | red | test_ph_g2_real_tree_is_clean |
| M143 | red | test_ph_g4_control_upload_url_is_allowlisted_only_for_put_in_publish |
| M144 | red | test_ph_g4_control_bad_urls_fire["https://graph.facebook.com/x;key=1"-';'] |
| M145 | red | test_ph_g4_control_bad_urls_fire["https://graph.facebook.com/x#x"-'#'] |
| M146a | red | test_ph_g4_caller_list_is_derived_and_pinned |
| M146b | red | test_ph_g4_real_tree_non_vacuity_by_kind |
| M147a | red | test_ph_g1_control_other_http_clients_fire_in_every_form[import |
| M147b | red | test_ph_g1_control_other_http_clients_fire_in_every_form[from |
| M149 | red | test_ph_g4_control_bad_urls_fire[f"https://graph.facebook.com{ig_user_id}/x"-authority] |
| M150a | red | test_ph_g4_control_bad_urls_fire[f"{_GRAPH}/{ig_user_id:{q}}"-format |
| M150b | red | test_ph_g4_control_bad_urls_fire[f"{_GRAPH}/{ig_user_id!r}"-conversion] |
| M151a | red | test_ph_g4_control_bad_urls_fire[f"{_GRAPH}/{other.platform_post_id}"-None] |
| M151b | red | test_ph_g4_control_bad_urls_fire[f"{_GRAPH}/{page_id}"-None] |
| M151c | red | test_ph_g4_control_bad_urls_fire[f"{_GRAPH}/{cut.platform_post_id}"-None] |
| M152 | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[assignment-url] |
| M153a | red | test_ph_g4_control_stores_through_self_cls_classname_or_setattr_fire[self.token_url |
| M153b | red | test_ph_g4_control_stores_through_self_cls_classname_or_setattr_fire[setattr(self, |
| M160c1 | red | test_ph_g2_control_every_bad_server_supplied_url_shape_fires[platform_http.put(u, |
| M160c2 | red | test_ph_g2_control_every_bad_server_supplied_url_shape_fires[platform_http.put(u, |
| M160c3 | red | test_ph_g2_control_every_bad_server_supplied_url_shape_fires[platform_http.put(u, |
| M160p | red | test_ph_g2_real_tree_is_clean |
| M161a | red | test_ph_g5_the_real_module_is_clean |
| M161b | red | test_ph_g5_the_real_module_is_clean |
| M161c | red | test_ph_g5_control_imports_fire[from |
| M161d | red | test_ph_g5_control_imports_fire[from |
| M163-annassign | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[annotated |
| M163-annotation only counted | red | test_ph_g4_control_module_name_baselines_pass |
| M163-assign | red | test_ph_g4_real_tree_is_clean |
| M163-async def | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[async |
| M163-augassign | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[augmented |
| M163-class | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[class-url] |
| M163-comprehension | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[comprehension |
| M163-def | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[def-url] |
| M163-del | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[del-url] |
| M163-except | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[except |
| M163-for | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[for-url] |
| M163-import | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[import |
| M163-import as | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[import |
| M163-match as | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[match |
| M163-match mapping | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[match |
| M163-match star | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[match |
| M163-param | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[function |
| M163-star import | red | test_ph_g4_control_a_star_import_fails_closed |
| M163-star report | red | test_ph_g4_control_a_star_import_fails_closed |
| M163-starred | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[star |
| M163-tuple | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[tuple |
| M163-type alias | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[type |
| M163-type param | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[type |
| M163-walrus | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[walrus-url] |
| M163-with | red | test_ph_g4_control_a_second_binding_of_a_module_name_fires[with-url] |
| M164 | red | test_network_guard_control |
| M164b | GREEN (equivalent) |  |
| M165 | red | test_ph_g4_control_bad_urls_fire["http://graph.facebook.com/x"-https://] |
| M166a | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[Client] |
| M166b | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[AsyncClient] |
| M166c | red | test_ph_g1_control_unknown_httpx_name_fires_in_every_form[request] |
| M167 | red | test_ph_g4_real_tree_is_clean |
| X-inbody | red | test_ph_g2_control_the_constants_are_reads_inside_a_function_only[from |

</details>

- **Deviations from the spec text (all stricter, none weaker)**: the bearer check order is value, then `server_supplied_url`, then
  https, then the `Authorization` collision; a non-bool `server_supplied_url` is a `TypeError`; the decode cap returns an
  internal sentinel the guard rejects with a fixed message instead of raising from `_normalise`; PH-G2 also fails the module used
  as a bare value and any `platform_http.<attr>` outside the six public names; PH-G4 treats any `setattr`/`delattr` with a
  non-constant attribute name as a store that blocks `self.<attr>` resolution and reports a star-import as a problem in
  itself; the network-guard control checks all four blocked routes and the recorded order, and asserts the guard starts with an
  empty list.
- **Commit 4**: this documentation commit.

## 11. Review history

- **Round 1 (r1 -> r2).** Security/Red-Team: not clean (`bearer=None`, guard name variants, userinfo, the server-chosen
  upload URL and the guard on it, header normalisation, G4 scope, known gaps, error-text hygiene). Correctness/Edge-Case: no
  blocker (`bearer=None` ambiguity, the guard on a server-supplied URL, "nine" vs seven, key variants and `;`, forward the
  original `params`, header order, `_FakePost` is `_Google`). Test-Quality: not clean (guard test could vanish with the
  constant, a `**kwargs` fake hides invalid calls, G4 too narrow, many unlisted mutants, `bearer=None`).
  Documentation-Consistency: not clean (nine/seven, `bearer=None`, §8 omissions, six/seven forms, "five times", Q5, plan
  file, mutant count).
- **Round 2 (r2 -> r3).** Security: no blocker or major; `urlsplit` error text can echo the netloc, `%61` inconsistency,
  name-set siblings, `headers`/`url` types, known-gap additions. Documentation: clean, with minors (the `%61` contradiction,
  mutant-table id ranges) and nits. Correctness: no blocker or major; commit 2 vs site 9's `server_supplied_url` and the
  bearer semantics (R7), `timeout` types (R8), `%61` scope, site 6/11 reachability, MockTransport's absolute-URL requirement,
  stale line citations, `prd.md:226` wording. Test-Quality: not clean (commit 2 cannot be a green pure move; PH-G4 inspected
  about half the URL arguments; the characterization table did not pin non-secret wire values; many §4.2 behaviors had no
  mutant; PH-G5 allowlist contradicted the `Mapping` check; the 302 row lacked a `Location`; unpatched paths could reach the
  network; PH-G2's count was literal-only).
- **Round 3 (r3 -> r4).** Security: no blocker or major (verdict clean): `from None` leaves `__context__` set (raise outside
  the `except`), a bearer with a control character reaches h11's error text (validate the character set), the guard read
  `params` keys differently from httpx (use `QueryParams`, type-check `params`), plus nits (single-pass decode, name siblings,
  `https` with a bearer, non-echoing `TypeError`). Correctness: no blocker or major (verdict clean): PH-G4 holes after `?`,
  class-level `token_url` never scanned, an equivalent value mutant (`caption[:2200]`), site 7 reachability, "within the six
  files". Test-Quality: not clean: the `__context__` claim, PH-G4 satisfiable by holes, call-level spreads, a per-function
  rather than per-callee allowlist, unscanned class constants, alias matching, mutants for PH-G1(b)/(c) and PH-G5 and other
  rules, no network guard in the commit-3 file, an imported constant in the commit-1 test, the row-14 fixture, rows 7-9 drive
  details. Documentation: not clean: the §7 ids sentence contradicted PH-G3, mutants for PH-G1(b)/(c)/PH-G5 and M80's label,
  site 5 reachability, R7's sentinel, partial R8 mutants, wording nits. All addressed in r4.
- **Round 4 (r4 -> r5).** Security: clean verdict, no blocker or major: `server_supplied_url` with `bearer=` (now a
  `ValueError`), header values and names validated like the bearer, PH-G4's `url=` and missing-argument cases, a decode cap
  that was fail-open (now 8, rejecting at the cap), `re.fullmatch` for the bearer, no caller value in any message.
  Correctness: clean verdict: site-8 slice bounds need a killing drive input, site 11 reachability, the PH-G4 overclaim in
  §5(a) (now pinned via the `bearer=`/`https`/`Authorization` rules), the `fullmatch` nit. Documentation: clean verdict: the
  architecture.md helper parenthetical and file counts, the PH-G3 `?x=1` accepted case, the `tests/test_module_seams.py` G1
  literal-target constraint, mutant-table headings and PH-G labels, site 7 wording. Test-Quality: not clean (four majors):
  unresolved f-string holes skipped (PH-G4 now allows only substituted module constants and the four pinned id expressions,
  with a literal scheme and authority), the URL by keyword or in extra positions, no mutants for the fail-open unparseable
  paths, a network guard that could be swallowed or not registered (now a recording list asserted at teardown, imported with
  `# noqa: F401`); plus minors: mutants for several §4.2 and PH clauses (M107-M136), a derived caller-module list,
  variable-operand value mutants, the transitional commit-2 `Bearer None` behavior stated as intentionally untested, parsed
  JSON for site 8. All addressed in r5.
- **Round 5 (r5 -> r6).** Security: not clean (one major): `server_supplied_url=True` was blocked only for the `bearer=`
  spelling, so `headers={"Authorization": ...}` still reached a server-chosen host (now a `ValueError` for
  `authorization`, `proxy-authorization`, `cookie`, `x-goog-api-key`, asserted statically too); minors: an id hole directly
  after the authority (the literal text before the first id hole must already contain the `/`), substituted `Name` holes and
  format-spec holes, `timeout` accepting `nan`/`inf`/non-positive (now `0 < t <= 3600`), the guard message echoing the raw key
  (now the matched constant name) and `bytes` params decoding (ASCII), rule 2's "unchecked" wording, leading-space URLs.
  Correctness: not clean (one major): one site-8 drive cannot make the `[:5000]` and the description-side `[:15]` bounds
  observable (now drives A, B and C), plus the stale §7 bullet, no control for the network guard, the class-attribute rule's
  scope (enclosing class body only, annotation-only ignored), `timeout` and format-spec holes. Documentation: not clean (one
  major): the stale §7 caller-list bullet; minors: a site-7 recipe detail, "rounds" wording, §5(c) wording, markdown and
  labels. Test-Quality: not clean (four majors): the site-8 drive again, PH-G4 hole matching (now `ast.dump` per file,
  function and expression, no format spec or conversion, binding multiplicity over every binding form), the killers for the
  fail-open unparseable paths (now deterministic patched raises plus real smokes with exact-message assertions), and the
  `Name` binding resolver; minors: mutants M137-M156, variable-operand swap table, what must be written new for PH-G4. All
  addressed in r6.
- **Round 6 (r6 -> r7).** Security: clean verdict, no blocker or major: the `server_supplied_url` header denylist was an
  exact-match list that missed equivalents (now an allowlist: only `Content-Type` and `Content-Length`, no `params`, no
  `bearer`), `math.isfinite` was dead weight and a raw `OverflowError` for `10**400` (range check before any `float()`, `math`
  import dropped), PH-G2's static header check failed open for a non-literal `headers=` and R4 over-claimed PH-G4. Correctness:
  not clean (one major): the binding rule counted `token_url` per file and would fail sites 1-4 (now: a module `Name` has
  exactly one binding in the file, a class attribute exactly one value-binding in the enclosing class body); minors: drives A/B
  multi-line and C `hashtags=None`, R4, M59 not applicable (retired), qualified `Class.method` names for the pinned id holes.
  Documentation: clean verdict: duplicated drive sentence, §9 wording, R4, the Dependency-caps clause. Test-Quality: not clean
  (two majors): the fail-open killers patch `urllib.parse.urlsplit` and `httpx.QueryParams`, so the module must call them as
  attributes (PH-G5 now forbids `from ... import`), and the network-guard control killed only half of M137 (now a
  `network_guard()` generator the control drives by hand); minors: more binding forms, M143/M152 controls, a constant
  `http://` URL control, `hashtags=None`, multi-line captions. All addressed in r7.
- **Round 7 (r7 -> r8).** Security, Correctness and Documentation: clean verdicts, no blocker or major. Minors: the
  `server_supplied_url` header names were compared after `_normalise` (now exact lower-cased `content-type` and
  `content-length`, two keys that case-fold to one name rejected), R4's first sentence still said `params` was checked (now: not
  allowed at all, `params is not None` rejected), M65 overlapped the `params=` ban (re-described), "real-list hashtags" except in
  drive C. Test-Quality: not clean (one major): the allowlist could not be told from a denylist by the listed tests (now benign
  rejected names `Accept`, `X-Upload-Content-Type`, `X-Foo`, PH-G2 controls for an extra key, a subset, a spread, a
  non-constant and a duplicate key, M162); minors: the unparseable-params patch is a `QueryParams` subclass, a control per
  listed binding form (M163), network-guard restoration in a `finally` with a no-chaining, asserted-restored contract (M164),
  the remote-id whitespace claim scoped to sites 6 and 11, mutants M165-M167. All addressed in r8.
- **Round 8 (r8 -> r9).** All four lenses returned clean verdicts (no blocker or major). Nits and minors applied: the header
  comparison worded as "after `str.lower()`", PH-G1 controls for `Client`/`AsyncClient`/`request`, PH-G4 binding forms as a literal
  list in the test with the class-body forms enumerated, star-imports and `type` statements, a distinct allowlist message so a
  rule-7 rejection cannot mask it, the runtime subset/`headers=None` behavior pinned (PH-G2 intentionally stricter), the guard
  control's `is` comparator and `gen.close()`, mutants M168-M171, and the round-7 timeout edge cases (`-0.0`, `3600.001`, `3601`).
- **Code review round 1 (implementation, `main..HEAD`).** Four personas; verdicts: Security no findings (the reviewer could not
  bypass the guard, leak caller text into any message or exception chain, or send a credential outside a bearer header or POST
  body; real token and `Location` shapes still pass), Correctness no findings (all 14 sites diffed against `main`: same kwargs,
  header order, positional URL; site 9 still has no Authorization), Test-Quality clean (about 290 reviewer-designed mutants; five
  minor survivors), Documentation clean (four minors and nits). Fixed in one follow-up commit: five test strengthenings, each
  verified to kill the named mutant by applying it to the module (falsy non-mapping `headers`, a case-sensitive `https` check,
  `startswith("auth")` for the collision check, the exact "encoded too many times" message, a type-name echo in a type error), the
  §4.1 private-helper list, the five publish drive variants, the Phase 8i deltas wording, the `300.0` upload timeout note, and
  the counts (6 new parametrized cases: 4327 selected). Two security nits became §7 sentences (a non-https `Location` is
  accepted for the video PUT; a hand-written `Authorization` header without `bearer=` is only caught statically).
