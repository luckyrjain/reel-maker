# Credential encryption: tests, fail-loud on a removed key, no lockout under a stale key (Phase 8a)

Candidate 6 of the full-codebase `improve-codebase-architecture` review: `api/crypto.py` had no
tests. It encrypts `Credential.token_blob` / `refresh_token_blob`, i.e. every connected YouTube and
Instagram account.

## What testing it found

1. **A removed key returned ciphertext as if it were the credential.** With `CREDENTIALS_KEY` unset,
   `open_()` returned its argument unchanged. For a row written while a key existed, that argument is
   a Fernet token, so a publisher would have sent `gAAAAA...` as the bearer token. The documented
   contract ("raises ValueError on decryption failure rather than silently returning the raw
   ciphertext") only held for a *wrong* key.
2. **A rotated or removed key locked the operator out of the recovery path.** SQLAlchemy decrypts
   every mapped column when it loads a row, so with a stale key *any* query that loaded a `Credential`
   raised `ValueError`: the connected-accounts page, `disconnect`, and the OAuth callback. Those are
   exactly the pages the error message ("re-authenticate") sends the operator to. Three router-level
   tests (list, disconnect, re-auth callback) fail on the original code under a rotated key; with fix 1
   applied but the columns not yet deferred, all five cases (those three, plus list and disconnect
   under a removed key) fail.
3. **Misleading messages.** A plaintext row read after a key was set was reported as "key may have been
   rotated"; a malformed key raised the library's bare "Fernet key must be 32 url-safe base64-encoded
   bytes" without naming the setting.

Not defects (pinned as characterization): surrounding whitespace in the key is tolerated by Fernet;
`seal()` accepts only `str`; an empty stored value under a key raises.

## Decisions

- **Fail loud, but only for things that are certainly ciphertext.** `_looks_like_fernet_token()` is
  deliberately strict: strict urlsafe-base64 alphabet, version byte `0x80`, at least 73 bytes
  (version + timestamp + IV + one block + HMAC) and a whole number of 16-byte blocks. It only chooses
  which error to raise, so a false positive would break plaintext dev mode; real OAuth tokens
  (`ya29.`, `EAA`, JWTs, a bare `gAAAAA` prefix) are tested not to match.
- **Plaintext rows are still refused once a key exists.** Silently accepting them would let anyone who
  can write a DB row inject a token. The error now says the row was never encrypted.
- **Defer the two encrypted columns** (`deferred()` on `Credential`) rather than catching the error in
  the TypeDecorator. Returning a sentinel/`None` on failure would hide the cause and risk a publisher
  treating an unreadable credential as "not connected". Deferral keeps the error exactly where a token
  is read (`engine/publish/youtube.py`, `instagram.py`, `metrics.py` — all in-session) and lets every
  page that doesn't read a token work. Assigning a new value never loads or decrypts the old one, so
  the re-auth callback overwrites cleanly. The cost is one lazy SELECT per column read (two when both tokens are
  read, as the YouTube refresh path does) — which is why the token-using call sites undefer them
  up front (see "Found by review").
- Fix 1 and the deferral ship in one commit: fix 1 alone would have extended the lockout to the
  removed-key case, which used to degrade quietly.

## Found by review (round 1)

- **Deferral re-opened an idle transaction during uploads.** `publish_cut` commits right before the
  upload "to end the read transaction: it would sit idle for the whole upload". With the token columns
  deferred, a publisher's first read of `credential.token_blob` lazy-loaded it and autobegan a new
  transaction that stayed open until the upload finished (minutes, on a pooled Postgres connection).
  The mocked-publisher test could not see it because nothing read a token. Fixed by
  `models.credential_token_loaders()` (`undefer` both columns) on the credential query in `publish_cut`
  and `pull_publish_metrics`' `_pull_one`; two new tests read the tokens the way the real publishers do,
  and removing the `undefer` at either site fails them. Loading up front also moves a stale-key failure
  to before any network call.
- **One stale credential no longer aborts the metrics batch.** `_pull_one` now catches the `ValueError`
  from loading an unreadable credential, logs it and skips that cut.
- **A whitespace-padded stored token slipped past the keyless check** (Fernet tolerates surrounding
  whitespace when a key is set, so the check must too); the helper now strips before classifying.
- Test hardening from a 191-mutant review: no secret, key or ciphertext in any message or log at any
  level; no Fernet TTL (credentials are long-lived); `""` is encrypted; dev-mode reads return the value
  exactly as stored and never log; every unreadable-token message tells the operator to re-authenticate;
  a vacuous padding test was replaced by one that exercises real padding; and one flaky assertion
  (`"new" not in ciphertext`) was found and fixed.

## Tests

`tests/test_crypto.py` (78): round trips (unicode, empty, 5k chars, bytes key), randomized Fernet
ciphertext, per-call key read, wrong/rotated key, tampered and truncated tokens, no secret or token in
any message or log, the plaintext fallback and its single warning, malformed keys, the structural
token check including its boundaries, and the TypeDecorator through a real ORM round trip (raw column
is ciphertext, NULL stays NULL, updates re-encrypt, columns are unloaded until read).
`tests/test_credentials_router.py` gains 7 tests: list / disconnect under a rotated and a removed key,
re-authentication replacing an unreadable token (with and without a refresh token in the response —
Google omits it on re-consent, so the callback must not read the stale one), and no token logged by the
callback. `tests/test_r3_proposed.py` and `tests/test_metrics_task.py` gain the worker-side tests below.

A 43-mutant battery over `api/crypto.py` (thresholds, version byte, alignment, alphabet, padding,
each branch and message, exception chaining, None handling, `cache_ok`, the warning and its content)
leaves 3 survivors, all equivalent: a 72-byte minimum is already excluded by block alignment, `*` vs
`+` in the alphabet regex differs only for an all-`=` string (which decodes empty), and the decoder
tolerates excess padding.

## Left alone (follow-ups)

- A malformed `CREDENTIALS_KEY` is only noticed on the first credential read/write. A startup check in
  the lifespan hook (like `validate_configured_models()`) would surface it earlier.
- No dual-key (`MultiFernet`) rotation: rotating the key still means re-authenticating each account.
- The connected-accounts page does not flag an unreadable credential or offer "Reconnect": it shows the
  account as connected with only a Disconnect button, so recovery is Disconnect, then Connect. The
  callback's overwrite-in-place path is covered by a test but is only reachable by visiting
  `/authorize` directly.
