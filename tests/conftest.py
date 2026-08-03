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
