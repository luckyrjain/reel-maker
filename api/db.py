from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from api.config import settings

# pool_pre_ping: a connection killed server-side (failover, idle timeout) is replaced
# before use instead of failing the first query of a task with OperationalError.
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
