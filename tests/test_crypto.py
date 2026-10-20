"""Tests for api/crypto.py — Fernet encryption of OAuth tokens at rest.

This module had no tests at all. It guards `Credential.token_blob`/`refresh_token_blob`,
i.e. every connected YouTube/Instagram account, so the properties worth pinning are the
security-shaped ones: ciphertext really is ciphertext, a wrong/rotated/missing key fails
loudly instead of handing garbage to a publisher, and no log line or error message ever
carries a secret.

The settings key is always pinned explicitly (`Settings` reads `.env`, so a developer's
own CREDENTIALS_KEY must not leak into these tests).
"""
import base64
import logging

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from api import crypto, models

SECRET = "ya29.a0-super-secret-access-token"


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode()


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


@pytest.fixture
def set_key(monkeypatch):
    def _set(value):
        monkeypatch.setattr(crypto.settings, "credentials_key", value)
    return _set


# ── seal / open_ with a key ──────────────────────────────────────────────────

def test_round_trip(set_key, key):
    set_key(key)
    assert crypto.open_(crypto.seal(SECRET)) == SECRET


@pytest.mark.parametrize("value", ["", "é✓🎉 unicode", "x" * 5000, " leading and trailing "])
def test_round_trip_edge_values(set_key, key, value):
    set_key(key)
    assert crypto.open_(crypto.seal(value)) == value


def test_ciphertext_is_not_the_plaintext_and_is_a_fernet_token(set_key, key):
    set_key(key)
    sealed = crypto.seal(SECRET)
    assert SECRET not in sealed
    assert sealed.startswith("gAAAAA")           # Fernet version byte 0x80, base64url
    assert Fernet(key.encode()).decrypt(sealed.encode()).decode() == SECRET


def test_sealing_is_randomized(set_key, key):
    """A fresh IV per call: equal plaintexts must not give equal ciphertexts, or two
    accounts sharing a token would be linkable from the database alone."""
    set_key(key)
    assert crypto.seal(SECRET) != crypto.seal(SECRET)


def test_key_is_read_on_every_call_not_cached(set_key, key):
    k2 = Fernet.generate_key().decode()
    set_key(key)
    sealed_with_first = crypto.seal(SECRET)
    set_key(k2)
    assert Fernet(k2.encode()).decrypt(crypto.seal(SECRET).encode()).decode() == SECRET
    with pytest.raises(ValueError):
        crypto.open_(sealed_with_first)


def test_a_key_with_surrounding_whitespace_is_tolerated(set_key, key):
    """Characterization: an env var / docker secret with a trailing newline still works."""
    set_key(f" {key}\n")
    assert crypto.open_(crypto.seal(SECRET)) == SECRET


def test_bytes_key_is_accepted(monkeypatch, key):
    monkeypatch.setattr(crypto.settings, "credentials_key", key.encode())
    assert crypto.open_(crypto.seal(SECRET)) == SECRET


# ── failure modes: wrong / rotated key, tampering, plaintext rows ────────────

def test_wrong_key_raises_value_error_without_leaking(set_key, key, caplog):
    set_key(key)
    sealed = crypto.seal(SECRET)
    set_key(Fernet.generate_key().decode())
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as ei:
        crypto.open_(sealed)
    msg = str(ei.value)
    assert "CREDENTIALS_KEY" in msg and "rotated" in msg
    assert sealed not in msg and SECRET not in msg
    assert sealed not in caplog.text and SECRET not in caplog.text


def test_wrong_key_error_is_chained_to_the_cause(set_key, key):
    from cryptography.fernet import InvalidToken
    set_key(key)
    sealed = crypto.seal(SECRET)
    set_key(Fernet.generate_key().decode())
    with pytest.raises(ValueError) as ei:
        crypto.open_(sealed)
    assert isinstance(ei.value.__cause__, InvalidToken)


def test_tampered_ciphertext_is_rejected(set_key, key):
    """Fernet authenticates (HMAC): flipping any byte must fail, not decrypt to garbage."""
    set_key(key)
    sealed = crypto.seal(SECRET)
    mid = len(sealed) // 2
    tampered = sealed[:mid] + ("A" if sealed[mid] != "A" else "B") + sealed[mid + 1:]
    with pytest.raises(ValueError):
        crypto.open_(tampered)


def test_truncated_ciphertext_is_rejected(set_key, key):
    set_key(key)
    with pytest.raises(ValueError):
        crypto.open_(crypto.seal(SECRET)[:-10])


def test_plaintext_row_read_after_a_key_is_set_raises(set_key, key):
    """A credential saved in dev (no key) and then read once a key exists is refused, not
    silently accepted: accepting plaintext would let anyone who can write a DB row inject a
    token. (The error message wording is pinned separately.)"""
    set_key(key)
    with pytest.raises(ValueError):
        crypto.open_("plain-token")


def test_empty_stored_value_with_a_key_raises(set_key, key):
    set_key(key)
    with pytest.raises(ValueError):
        crypto.open_("")


# ── no key configured: documented plaintext fallback ─────────────────────────

def test_without_a_key_seal_returns_the_plaintext_and_warns(set_key, caplog):
    set_key("")
    with caplog.at_level(logging.WARNING, logger="api.crypto"):
        assert crypto.seal(SECRET) == SECRET
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "CREDENTIALS_KEY" in warnings[0].getMessage()
    assert SECRET not in caplog.text                  # the warning never carries the secret


def test_without_a_key_open_returns_the_value_unchanged(set_key):
    set_key("")
    assert crypto.open_(SECRET) == SECRET
    assert crypto.open_("") == ""


def test_no_warning_when_a_key_is_set(set_key, key, caplog):
    set_key(key)
    with caplog.at_level(logging.WARNING, logger="api.crypto"):
        crypto.seal(SECRET)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── invalid configuration ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["notakey", "short", "A" * 43])
def test_malformed_key_raises_value_error(set_key, bad):
    set_key(bad)
    with pytest.raises(ValueError):
        crypto.seal(SECRET)
    with pytest.raises(ValueError):
        crypto.open_("anything")


def test_malformed_key_error_does_not_echo_the_key(set_key):
    bad = "this-is-not-a-fernet-key-but-looks-secret"
    set_key(bad)
    with pytest.raises(ValueError) as ei:
        crypto.seal(SECRET)
    assert bad not in str(ei.value)


@pytest.mark.parametrize("bad_value", [123, b"bytes", None])
def test_seal_rejects_non_strings(set_key, key, bad_value):
    """Characterization: seal() is str-only; the TypeDecorator handles None itself."""
    set_key(key)
    with pytest.raises(AttributeError):
        crypto.seal(bad_value)


# ── the SQLAlchemy TypeDecorator, through a real ORM round trip ──────────────

def _raw(db, cred_id, column):
    return db.execute(text(f"SELECT {column} FROM credentials WHERE id = :i"), {"i": cred_id}).scalar()


def test_encrypted_column_stores_ciphertext_and_reads_plaintext(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob=SECRET, refresh_token_blob="refresh-" + SECRET)
    db_session.add(cred)
    db_session.commit()
    cred_id = cred.id

    assert SECRET not in _raw(db_session, cred_id, "token_blob")
    assert SECRET not in _raw(db_session, cred_id, "refresh_token_blob")
    assert _raw(db_session, cred_id, "token_blob").startswith("gAAAAA")

    db_session.expire_all()                            # force a real re-read
    fresh = db_session.get(models.Credential, cred_id)
    assert fresh.token_blob == SECRET
    assert fresh.refresh_token_blob == "refresh-" + SECRET


def test_encrypted_column_without_a_key_stores_plaintext(db_session, set_key):
    set_key("")
    cred = models.Credential(provider="youtube", token_blob=SECRET)
    db_session.add(cred)
    db_session.commit()
    assert _raw(db_session, cred.id, "token_blob") == SECRET


def test_null_columns_stay_null_and_are_never_encrypted(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="instagram", token_blob=SECRET)   # no refresh token
    db_session.add(cred)
    db_session.commit()
    assert _raw(db_session, cred.id, "refresh_token_blob") is None
    db_session.expire_all()
    assert db_session.get(models.Credential, cred.id).refresh_token_blob is None


def test_updating_a_token_re_encrypts(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob="old-token-value-1")
    db_session.add(cred)
    db_session.commit()
    before = _raw(db_session, cred.id, "token_blob")
    cred.token_blob = "new-token-value-2"
    db_session.commit()
    after = _raw(db_session, cred.id, "token_blob")
    assert after != before and "new-token-value-2" not in after
    db_session.expire_all()
    assert db_session.get(models.Credential, cred.id).token_blob == "new-token-value-2"


def test_rotated_key_row_loads_but_reading_the_token_raises_a_clear_error(db_session, set_key, key):
    """The columns are deferred: the row (provider, label, ...) stays usable under a stale
    key, and the ValueError fires where a token is actually read — see
    tests/test_credentials_router.py for the pages that depend on this."""
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob=SECRET, refresh_token_blob="r-" + SECRET,
                             account_label="My Channel")
    db_session.add(cred)
    db_session.commit()
    cred_id = cred.id
    set_key(Fernet.generate_key().decode())
    db_session.expire_all()
    loaded = db_session.get(models.Credential, cred_id)
    assert loaded.account_label == "My Channel"
    with pytest.raises(ValueError, match="CREDENTIALS_KEY"):
        loaded.token_blob
    with pytest.raises(ValueError, match="CREDENTIALS_KEY"):
        loaded.refresh_token_blob


def test_token_columns_are_not_loaded_until_read(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob=SECRET)
    db_session.add(cred)
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.get(models.Credential, cred.id)
    from sqlalchemy import inspect
    assert {"token_blob", "refresh_token_blob"} <= inspect(loaded).unloaded
    assert loaded.token_blob == SECRET


def test_type_decorator_passes_none_through_in_both_directions():
    enc = crypto.Encrypted()
    assert enc.process_bind_param(None, None) is None
    assert enc.process_result_value(None, None) is None


def test_type_decorator_is_cache_safe_text_impl():
    from sqlalchemy import Text
    assert crypto.Encrypted.cache_ok is True
    assert crypto.Encrypted.impl is Text


# ── fail-loud on a removed key, and precise error messages ───────────────────

def test_removed_key_does_not_hand_back_the_ciphertext(set_key, key):
    """The bug this guards: with the key unset, open_() used to return a stored Fernet
    token unchanged, so a publisher would send 'gAAAAA...' as the bearer token."""
    set_key(key)
    sealed = crypto.seal(SECRET)
    set_key("")
    with pytest.raises(ValueError) as ei:
        crypto.open_(sealed)
    msg = str(ei.value)
    assert "encrypted" in msg and "CREDENTIALS_KEY is not set" in msg
    assert sealed not in msg and SECRET not in msg


def test_removed_key_row_loads_but_reading_the_token_raises_a_clear_error(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob=SECRET)
    db_session.add(cred)
    db_session.commit()
    cred_id = cred.id
    set_key("")
    db_session.expire_all()
    loaded = db_session.get(models.Credential, cred_id)
    with pytest.raises(ValueError, match="CREDENTIALS_KEY is not set"):
        loaded.token_blob


@pytest.mark.parametrize("token", [
    "ya29.a0AfH6SMBx-1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789_abcdefghij",
    "EAAGm0PX4ZCpsBAKd8l5ZCqJ3TZBlvO1YvXq3sLZB9kjhgf7Qw0bX2mN4cV6bN8mZAsdfghjklPOIUYTREWQ1234567890zxcvbnm",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "gAAAAA-looks-like-a-prefix-but-is-not-a-token",
    "gAAAAA" + "A" * 94,             # right prefix and a plausible length, wrong structure
    "plain-short-token",
    "",
])
def test_real_plaintext_tokens_are_never_mistaken_for_ciphertext(set_key, token):
    """With no key configured (the documented dev mode) a plaintext credential must come
    back exactly as stored — the new fail-loud check must not fire on real token shapes."""
    set_key("")
    assert crypto.open_(token) == token


def test_plaintext_row_with_a_key_set_says_it_is_unencrypted_not_rotated(set_key, key):
    set_key(key)
    with pytest.raises(ValueError) as ei:
        crypto.open_("ya29.a-plaintext-token-saved-before-the-key-existed")
    msg = str(ei.value)
    assert "not encrypted" in msg and "CREDENTIALS_KEY" in msg
    assert "rotated" not in msg


def test_rotated_key_message_still_says_rotated(set_key, key):
    set_key(key)
    sealed = crypto.seal(SECRET)
    set_key(Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="rotated"):
        crypto.open_(sealed)


def test_tampered_token_still_reads_as_a_decryption_failure_not_plaintext(set_key, key):
    """A tampered token is still structurally a Fernet token, so it must get the
    'failed to decrypt' message, not the 'was never encrypted' one."""
    set_key(key)
    sealed = crypto.seal(SECRET)
    mid = len(sealed) // 2
    tampered = sealed[:mid] + ("A" if sealed[mid] != "A" else "B") + sealed[mid + 1:]
    with pytest.raises(ValueError, match="Failed to decrypt"):
        crypto.open_(tampered)


def test_malformed_key_error_names_the_setting_and_how_to_fix_it(set_key):
    set_key("notakey")
    with pytest.raises(ValueError) as ei:
        crypto.seal(SECRET)
    msg = str(ei.value)
    assert "CREDENTIALS_KEY" in msg and "Fernet" in msg and "32" in msg
    assert "Fernet.generate_key" in msg
    assert "notakey" not in msg
    assert isinstance(ei.value.__cause__, ValueError)       # the library error stays chained


# ── _looks_like_fernet_token ─────────────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 31, 32, 100, 1000])
def test_every_real_token_length_is_recognized(set_key, key, n):
    set_key(key)
    assert crypto._looks_like_fernet_token(crypto.seal("x" * n))


def test_looks_like_rejects_structural_mismatches(set_key, key):
    set_key(key)
    sealed = crypto.seal(SECRET)
    raw = _b64d(sealed)
    assert crypto._looks_like_fernet_token(sealed)
    assert not crypto._looks_like_fernet_token(sealed + "!")                 # outside the alphabet
    assert not crypto._looks_like_fernet_token(sealed + "+")                 # standard-b64 char, not urlsafe
    assert not crypto._looks_like_fernet_token(_b64e(b"\x81" + raw[1:]))    # wrong version byte
    assert not crypto._looks_like_fernet_token(_b64e(raw[:60]))             # below the minimum
    assert not crypto._looks_like_fernet_token(_b64e(raw + b"\x00"))        # not block-aligned
    assert not crypto._looks_like_fernet_token(_b64e(raw[:-1]))             # not block-aligned
    assert not crypto._looks_like_fernet_token("")
    assert crypto._looks_like_fernet_token(" " + sealed + "\n")                     # Fernet tolerates surrounding whitespace


def test_minimum_token_boundary(set_key, key):
    """73 bytes (one cipher block) is the smallest real token; 72 and 57 are not."""
    set_key(key)
    raw = _b64d(crypto.seal(""))
    assert len(raw) == 73
    assert crypto._looks_like_fernet_token(_b64e(raw))
    assert not crypto._looks_like_fernet_token(_b64e(raw[:72]))
    assert not crypto._looks_like_fernet_token(_b64e(raw[:57]))


def test_looks_like_requires_16_byte_block_alignment_not_just_8(set_key, key):
    """73 + 8 = 81 bytes is 8-aligned but not a whole number of AES blocks."""
    set_key(key)
    raw = _b64d(crypto.seal(""))
    assert not crypto._looks_like_fernet_token(_b64e(raw + b"\x00" * 8))
    assert crypto._looks_like_fernet_token(_b64e(raw + b"\x00" * 16))       # one more real block


def test_looks_like_rejects_standard_base64_characters(set_key, key):
    """'+' and '/' are standard-base64; a Fernet token is urlsafe ('-' and '_'). Find a real
    token whose bytes would need them and check the standard encoding is not recognized."""
    set_key(key)
    for _ in range(200):
        raw = _b64d(crypto.seal(SECRET))
        std = base64.b64encode(raw).decode()
        if "+" in std or "/" in std:
            assert not crypto._looks_like_fernet_token(std)
            assert crypto._looks_like_fernet_token(_b64e(raw))               # the urlsafe form is fine
            return
    pytest.fail("never produced a token needing '+' or '/' in standard base64")


def test_looks_like_rejects_excess_padding(set_key, key):
    set_key(key)
    unpadded = crypto.seal(SECRET).rstrip("=")
    assert crypto._looks_like_fernet_token(unpadded)
    assert not crypto._looks_like_fernet_token(unpadded + "===")


def test_plaintext_error_is_chained_to_the_cause(set_key, key):
    from cryptography.fernet import InvalidToken
    set_key(key)
    with pytest.raises(ValueError) as ei:
        crypto.open_("ya29.a-plaintext-token")
    assert isinstance(ei.value.__cause__, InvalidToken)


# ── second review round: leakage at every level, TTL, dev-mode exactness, padding ────

# -- no secret (plaintext token, ciphertext, or the key itself) reaches an error message or a log
def test_plaintext_row_error_does_not_echo_the_stored_plaintext_token(set_key, key):
    set_key(key)
    tok = "ya29.a-plaintext-token-saved-before-the-key-existed"
    with pytest.raises(ValueError) as ei:
        crypto.open_(tok)
    assert tok not in str(ei.value)


def test_no_function_logs_a_secret_key_or_ciphertext_at_any_level(set_key, key, caplog):
    other = Fernet.generate_key().decode()
    plain = "ya29.plain-row-token-xxxxxxxxxxxxxxxxxxxxxxxx"
    set_key(key)
    sealed = crypto.seal(SECRET)
    with caplog.at_level(logging.DEBUG):
        crypto.open_(sealed)                                   # happy path under the key
        for bad in (plain, sealed[:-6] + "AAAAAA"):            # unencrypted row / tampered
            with pytest.raises(ValueError):
                crypto.open_(bad)
        set_key(other)
        with pytest.raises(ValueError):
            crypto.open_(sealed)                               # rotated
        set_key("")
        with pytest.raises(ValueError):
            crypto.open_(sealed)                               # removed key
        crypto.open_(plain)                                    # dev-mode plaintext read
        crypto.seal(SECRET)                                    # dev-mode plaintext write
        set_key("bad-key-bad-key-bad-key")
        with pytest.raises(ValueError):
            crypto.seal(SECRET)
    for needle in (key, other, "bad-key-bad-key-bad-key", sealed, plain, SECRET):
        assert needle not in caplog.text


def test_dev_mode_read_is_quiet_and_warning_comes_from_the_module_logger(set_key, caplog):
    set_key("")
    with caplog.at_level(logging.DEBUG):
        crypto.open_(SECRET)
    assert caplog.records == []                                # reads never warn/log
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        crypto.seal(SECRET)
    assert [r.name for r in caplog.records] == ["api.crypto"]


@pytest.mark.parametrize("mode", ["rotated", "removed", "plaintext"])
def test_every_unreadable_message_tells_the_operator_to_reauthenticate(set_key, key, mode):
    set_key(key)
    sealed = crypto.seal(SECRET)
    if mode == "rotated":
        set_key(Fernet.generate_key().decode())
        bad = sealed
    elif mode == "removed":
        set_key("")
        bad = sealed
    else:
        bad = "ya29.a-plaintext-token-saved-before-the-key-existed"
    with pytest.raises(ValueError) as ei:
        crypto.open_(bad)
    assert "re-authenticate" in str(ei.value).lower()


def test_the_four_messages_are_distinct_and_each_names_its_own_cause(set_key, key):
    msgs = {}
    set_key("notakey")
    with pytest.raises(ValueError) as ei:
        crypto.seal("x")
    msgs["badkey"] = str(ei.value)
    set_key(key)
    sealed = crypto.seal("x")
    with pytest.raises(ValueError) as ei:
        crypto.open_("plain-row")
    msgs["plain"] = str(ei.value)
    set_key(Fernet.generate_key().decode())
    with pytest.raises(ValueError) as ei:
        crypto.open_(sealed)
    msgs["rotated"] = str(ei.value)
    set_key("")
    with pytest.raises(ValueError) as ei:
        crypto.open_(sealed)
    msgs["removed"] = str(ei.value)
    assert len(set(msgs.values())) == 4
    assert "not a valid Fernet key" in msgs["badkey"]
    assert "not encrypted" in msgs["plain"] and "rotated" not in msgs["plain"]
    assert "rotated" in msgs["rotated"] and "not encrypted" not in msgs["rotated"]
    assert "is not set" in msgs["removed"] and "rotated" not in msgs["removed"]


# -- falsy-but-not-None values through the real TypeDecorator
def test_empty_string_token_is_encrypted_at_rest_and_round_trips(db_session, set_key, key):
    from sqlalchemy import text
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob="", refresh_token_blob="")
    db_session.add(cred)
    db_session.commit()
    raw = db_session.execute(text("SELECT token_blob FROM credentials WHERE id=:i"), {"i": cred.id}).scalar()
    assert raw.startswith("gAAAAA")
    db_session.expire_all()
    got = db_session.get(models.Credential, cred.id)
    assert got.token_blob == "" and got.refresh_token_blob == ""



# stored credentials are long-lived (Instagram ~60d): a Fernet TTL must never apply
def test_a_credential_sealed_long_ago_still_opens(set_key, key):
    set_key(key)
    old = Fernet(key.encode()).encrypt_at_time(SECRET.encode(), 0).decode()      # timestamp = 1970
    assert crypto.open_(old) == SECRET


# dev mode returns a stored value byte-for-byte (no stripping)
@pytest.mark.parametrize("v", [" padded ", "tok\n", "\ttok"])
def test_dev_mode_open_returns_value_exactly_as_stored(set_key, v):
    set_key("")
    assert crypto.open_(v) == v


# a Credential saved without tokens reads back NULL, not "" and not an error
def test_token_columns_default_to_null(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="youtube")
    db_session.add(cred)
    db_session.commit()
    db_session.expire_all()
    got = db_session.get(models.Credential, cred.id)
    assert got.token_blob is None and got.refresh_token_blob is None


# the unpadded-input test in test_crypto.py is vacuous for SECRET (its token needs no '=');
# use a payload whose token is padded so the padding-restoration branch is really exercised
def test_looks_like_accepts_a_real_token_whose_padding_was_stripped(set_key, key):
    set_key(key)
    sealed = crypto.seal("")                 # 73 bytes -> base64 ends in '=='
    assert sealed.endswith("==")
    assert crypto._looks_like_fernet_token(sealed.rstrip("="))
    assert crypto._looks_like_fernet_token(sealed.rstrip("=") + "=")      # partially padded
    s89 = crypto.seal("x" * 16)              # 89 bytes -> single '='
    assert s89.endswith("=") and not s89.endswith("==")
    assert crypto._looks_like_fernet_token(s89.rstrip("="))


def test_whitespace_padded_fernet_token_is_still_recognized_without_a_key(set_key, key):
    """Fernet tolerates surrounding whitespace when a key is set, so the keyless check must
    too — otherwise a stored token with a trailing newline would be handed back as the
    credential."""
    set_key(key)
    sealed = crypto.seal(SECRET)
    set_key("")
    for padded in (sealed + "\n", " " + sealed, "\t" + sealed + " \r\n"):
        with pytest.raises(ValueError, match="CREDENTIALS_KEY is not set"):
            crypto.open_(padded)


def test_assigning_none_to_an_unloaded_refresh_token_persists_null(db_session, set_key, key):
    """The OAuth callback assigns refresh_token_blob without ever loading the old value."""
    from sqlalchemy import text
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob="t", refresh_token_blob="r")
    db_session.add(cred)
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.get(models.Credential, cred.id)
    loaded.refresh_token_blob = None
    db_session.commit()
    assert db_session.execute(text("SELECT refresh_token_blob FROM credentials WHERE id=:i"),
                              {"i": cred.id}).scalar() is None

