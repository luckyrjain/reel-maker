"""Tests for api/routers/credentials.py — the OAuth connect/disconnect routes."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app
from api.oauth import InstagramOAuth, YouTubeOAuth


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, follow_redirects=False) as c:
        c._session_factory = TestingSessionLocal
        yield c
    app.dependency_overrides.clear()


def test_list_credentials_shows_not_connected_by_default(client):
    resp = client.get("/api/credentials")
    assert resp.status_code == 200
    assert "not connected" in resp.text


def test_authorize_rejects_unknown_provider(client):
    resp = client.get("/api/credentials/myspace/authorize")
    assert resp.status_code == 404


def test_authorize_422_when_not_configured(client):
    fake = YouTubeOAuth("", "", "https://example.com/cb")  # no client_id configured
    with patch("api.routers.credentials.get_oauth_provider", return_value=fake):
        resp = client.get("/api/credentials/youtube/authorize")
    assert resp.status_code == 422


def test_authorize_redirects_to_provider_when_configured(client):
    fake = YouTubeOAuth("client-id", "secret", "https://example.com/cb")
    with patch("api.routers.credentials.get_oauth_provider", return_value=fake):
        resp = client.get("/api/credentials/youtube/authorize")
    assert resp.status_code in (302, 307)
    assert "accounts.google.com" in resp.headers["location"]


def test_callback_rejects_invalid_state(client):
    resp = client.get("/api/credentials/youtube/callback", params={"code": "abc", "state": "bogus"})
    assert resp.status_code == 400


def test_callback_surfaces_provider_denial(client):
    resp = client.get("/api/credentials/youtube/callback", params={"error": "access_denied"})
    assert resp.status_code == 400


def test_callback_stores_credential_for_youtube(client):
    from api.oauth import new_state

    state = new_state("youtube")
    fake = YouTubeOAuth("client-id", "secret", "https://example.com/cb")
    fake.exchange_code = MagicMock(return_value={
        "access_token": "tok", "refresh_token": "rtok", "expires_in": 3600, "scope": "a b",
    })

    with patch("api.routers.credentials.get_oauth_provider", return_value=fake):
        resp = client.get(
            "/api/credentials/youtube/callback", params={"code": "abc", "state": state}
        )
    assert resp.status_code == 303

    db = client._session_factory()
    cred = db.query(models.Credential).filter(models.Credential.provider == "youtube").first()
    assert cred is not None
    assert cred.token_blob == "tok"
    assert cred.refresh_token_blob == "rtok"
    assert cred.scopes == ["a", "b"]
    db.close()


def test_callback_uses_page_token_for_instagram(client):
    """Instagram publishing rides on the Page token, not the user token."""
    from api.oauth import new_state

    fake = InstagramOAuth("app-id", "secret", "https://example.com/cb")
    fake.exchange_code = MagicMock(return_value={"access_token": "short-lived-user-tok"})
    fake.exchange_long_lived_token = MagicMock(
        return_value={"access_token": "long-lived-user-tok", "expires_in": 5_183_944}
    )
    fake.discover_account = MagicMock(return_value={
        "page_id": "page1", "page_access_token": "page-tok", "ig_user_id": "ig123",
    })

    state = new_state("instagram")
    with patch("api.routers.credentials.get_oauth_provider", return_value=fake):
        resp = client.get("/api/credentials/instagram/callback", params={"code": "abc", "state": state})

    assert resp.status_code == 303
    fake.exchange_long_lived_token.assert_called_once_with("short-lived-user-tok")
    fake.discover_account.assert_called_once_with("long-lived-user-tok")
    db = client._session_factory()
    cred = db.query(models.Credential).filter(models.Credential.provider == "instagram").first()
    assert cred.token_blob == "page-tok"
    assert cred.provider_account_id == "ig123"
    assert cred.expires_at is not None
    db.close()


def test_disconnect_removes_credential(client):
    db = client._session_factory()
    db.add(models.Credential(provider="youtube", token_blob="tok"))
    db.commit()
    db.close()

    resp = client.post("/api/credentials/youtube/disconnect")
    assert resp.status_code == 303

    db = client._session_factory()
    assert db.query(models.Credential).filter(models.Credential.provider == "youtube").first() is None
    db.close()
