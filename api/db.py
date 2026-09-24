from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from api.config import settings

# pool_pre_ping: a connection killed server-side (failover, idle timeout) is replaced
# before use instead of failing the first query of a task with OperationalError.
# hide_parameters: SQLAlchemy errors otherwise echo bound values (e.g. an OAuth token_blob)
# into exception text, which ends up in logs and in job.error shown in the UI.
engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
