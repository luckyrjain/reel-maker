"""Tests for api/crypto.py — Fernet encryption of OAuth tokens at rest.

This module had no tests at all. It guards `Credential.token_blob`/`refresh_token_blob`,
i.e. every connected YouTube/Instagram account, so the properties worth pinning are the
security-shaped ones: ciphertext really is ciphertext, a wrong/rotated/missing key fails
loudly instead of handing garbage to a publisher, and no log line or error message ever
carries a secret.

The settings key is always pinned explicitly (`Settings` reads `.env`, so a developer's
own CREDENTIALS_KEY must not leak into these tests).
"""
import logging

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from api import crypto, models

SECRET = "ya29.a0-super-secret-access-token"


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
    cred = models.Credential(provider="youtube", token_blob="old")
    db_session.add(cred)
    db_session.commit()
    before = _raw(db_session, cred.id, "token_blob")
    cred.token_blob = "new"
    db_session.commit()
    after = _raw(db_session, cred.id, "token_blob")
    assert after != before and "new" not in after
    db_session.expire_all()
    assert db_session.get(models.Credential, cred.id).token_blob == "new"


def test_rotated_key_makes_the_row_unreadable_with_a_clear_error(db_session, set_key, key):
    set_key(key)
    cred = models.Credential(provider="youtube", token_blob=SECRET)
    db_session.add(cred)
    db_session.commit()
    cred_id = cred.id
    set_key(Fernet.generate_key().decode())
    db_session.expire_all()
    with pytest.raises(ValueError, match="CREDENTIALS_KEY"):
        db_session.get(models.Credential, cred_id)


def test_type_decorator_passes_none_through_in_both_directions():
    enc = crypto.Encrypted()
    assert enc.process_bind_param(None, None) is None
    assert enc.process_result_value(None, None) is None


def test_type_decorator_is_cache_safe_text_impl():
    from sqlalchemy import Text
    assert crypto.Encrypted.cache_ok is True
    assert crypto.Encrypted.impl is Text
