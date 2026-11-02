# YouTube access-token refresh: tests for the path that never ran (Phase 8d, candidate 7)

## Problem

`YouTubeOAuth.refresh()` (`api/oauth.py`) exchanges the stored refresh token for a new access token. It is
called from exactly one place, `engine/publish/youtube.py::get_valid_access_token()`, which both the YouTube
publisher and the YouTube metrics fetcher use. Google access tokens last about an hour and
`pull_publish_metrics` runs every 6 hours, so an expired token is the normal case for every scheduled pull,
and for any publish that follows a pause.

Every existing test replaced the provider: `patch("engine.publish.youtube.get_oauth_provider",
return_value=fake_oauth)` with `fake_oauth.refresh.return_value = {...}` (`test_youtube_publisher.py`,
`test_metrics_fetcher.py`). So `refresh()` itself, the request it builds, its error handling and the real
call chain were never executed by the suite. A change to the grant type, the body fields, `raise_for_status`
or the token URL would have passed everything.

## Change

Tests only, in the new `tests/test_youtube_token_refresh.py` (24 tests). No production code changed.

- **`refresh()`**: `httpx.post` is the only thing faked, and it answers through a real `httpx.Client` with a
  `MockTransport`, so the request is built as httpx would send it, and EVERY argument `refresh()` passes
  reaches the transport (a first version of the fake silently dropped extras, which hid a leaked `params=`
  from every test until a review caught it). Pinned: method, Google's token URL, the
  `application/x-www-form-urlencoded` content type, the exact four-field body, secrets only in the body (no query string, no
  extra arguments, no secret in the headers), a 30 s timeout, the JSON body returned unchanged, and `HTTPStatusError` for
  400/401/403/429/500/503 with neither the refresh token nor the client secret in the error text, no retry
  of a rejected grant, and no secret in any log record at any level.
- **`get_valid_access_token`** through the real `get_oauth_provider("youtube")` (settings patched to known
  ids): an expired credential is refreshed and persisted (new token, expiry about now + `expires_in`, one
  commit, refresh token kept because Google does not send a new one); a valid or expiry-less credential
  causes no request and no commit; a rejected refresh or a response without `access_token` leaves the
  credential untouched with no commit (the assignment order matters: `tokens["access_token"]` is read before
  anything is written); no stored refresh token (None or an empty string) fails before any request; `expires_in` as a string works.
- **The chain a scheduled pull takes**: expired credential, real refresh, then the metrics API call carries
  the new bearer token.

## Characterization, not endorsement

A response without `expires_in` stores `expires_at = None`, and a credential with no expiry is treated as
never expiring, so that token would not be refreshed again. Google always sends `expires_in`, so this is not
reachable today; it is pinned so changing it is a deliberate decision. Likewise a `refresh_token` in the
refresh response is ignored and the stored one kept (Google normally sends none).

## Verification

A 35-mutant battery over `refresh()` and `get_valid_access_token()` is all killed: the grant type, each of the
three other body fields and the refresh-token key, `data=` changed to `params=`, `raise_for_status` removed,
returning the response instead of its JSON, two timeouts, the token URL, the commit removed, the expiry
comparison inverted or dropped, the token not stored, seconds changed to minutes, `int()` removed, the old
expiry kept, the wrong token sent to Google, the wrong provider, the no-refresh-token guard removed, the
expiry-less guard removed, and the old token returned, plus (added after review) `params=`, a secret in
`headers=`, `auth=` and `follow_redirects=` passed to the post, the guard changed to `is None`, a retry of
a failed refresh, the refresh token, client secret or either access token (fresh or stale) written to a log, the response JSON
parsed before the status is checked (a non-JSON 503 body), and `commit()` called with arguments.

## Left alone

- The ORM side: `Credential.token_blob` is an `Encrypted` column and these tests use plain objects, so the
  encrypt-on-write path is covered by `test_crypto.py` and `test_credentials_router.py`, not here.
- A real SQLite round trip would hand back a naive `expires_at`, which cannot be compared with an aware
  `now()`; PostgreSQL (the supported database) returns aware values. Not exercised here.
- Instagram has no refresh flow (`InstagramOAuth` exchanges for a long-lived token once, at connect time).
