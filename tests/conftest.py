"""Shared pytest fixtures."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models


@pytest.fixture()
def db_session():
    """An isolated in-memory SQLite session with the full schema created.

    Each test gets its own engine/connection (StaticPool keeps the single
    :memory: connection alive for the session's lifetime), so tests never
    see each other's rows.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _permissive_media_sniffing(request, monkeypatch):
    """Make the sourcers' downloaded-bytes check accept anything, except in tests that opt in.

    `engine.render.asset_sourcer._sniff_ok` rejects a download whose first bytes are not a real
    JPEG/PNG/GIF/WebP/TIFF image or MP4/WebM/GIF video. The hundreds of sourcer fakes elsewhere in
    the suite serve placeholder bytes (b"orig", b"video", ...), so they would all fail it for a
    reason that has nothing to do with what they test. Tests that exercise the check itself carry
    `@pytest.mark.real_media_sniffing` (see tests/test_sourcer_sniffing.py) and get the real one.
    """
    if request.node.get_closest_marker("real_media_sniffing"):
        return
    monkeypatch.setattr("engine.render.asset_sourcer._sniff_ok", lambda kind, head: True, raising=False)


@pytest.fixture(autouse=True)
def _no_real_dns_lookups(request, monkeypatch):
    """Make the sourcers' bounded DNS pre-resolution a no-op that always answers "in time".

    `engine.render.asset_sourcer._dns_in_time` resolves the host of every download hop. Real lookups
    in the suite would need the network (and fail offline) for no reason: the fakes never connect.
    Tests of the gate itself carry `@pytest.mark.real_dns` (tests/test_sourcer_dns.py) and get the
    real one, with `socket.getaddrinfo` stubbed.
    """
    if request.node.get_closest_marker("real_dns"):
        return
    monkeypatch.setattr("engine.render.asset_sourcer._dns_in_time", lambda host, port, timeout: True, raising=False)


@pytest.fixture(autouse=True)
def _api_calls_via_httpx(request, monkeypatch):
    """Wire the sourcers' streamed API calls back to `httpx.get` / `httpx.post`, except in `real_api` tests.

    `engine.render.asset_sourcer._api_get` / `_api_post` stream and size-cap the Pexels / Wikipedia /
    HuggingFace API calls through the `_http_stream` seam. They keep `httpx.get`'s / `httpx.post`'s
    call signature, and ~130 older tests fake those two functions (a fake returns a response-like
    object with `.json()`, which the streaming path could not build a body from). This hands every
    such call to whatever `httpx.get` / `httpx.post` currently is (looked up at call time, so the old
    patches keep working untouched), dropping the one extra keyword, `limit`. Tests of the streaming
    path itself carry `@pytest.mark.real_api` (tests/test_sourcer_api_streaming.py).
    """
    if request.node.get_closest_marker("real_api"):
        return
    import httpx

    monkeypatch.setattr("engine.render.asset_sourcer._api_get",
                        lambda url, *, limit=None, **kw: httpx.get(url, **kw), raising=False)
    monkeypatch.setattr("engine.render.asset_sourcer._api_post",
                        lambda url, *, limit=None, **kw: httpx.post(url, **kw), raising=False)


@pytest.fixture(autouse=True)
def _permissive_image_check(request, monkeypatch):
    """Make the sourcers' "Pillow can read it and it is not enormous" check accept anything.

    `engine.render.asset_sourcer._image_ok` opens an image's header with Pillow; the suite's fakes
    serve placeholder bytes (even the "valid-looking" JPEG/PNG heads of test_sourcer_sniffing.py are not
    decodable images). Tests of the check itself carry `@pytest.mark.real_image_check`
    (tests/test_sourcer_image_check.py) and use real images.
    """
    if request.node.get_closest_marker("real_image_check"):
        return
    monkeypatch.setattr("engine.render.asset_sourcer._image_ok", lambda source: True, raising=False)

