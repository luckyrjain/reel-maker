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

