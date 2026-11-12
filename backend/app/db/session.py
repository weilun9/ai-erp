import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

if get_settings().database_url_env:
    # Hosted database behind a pooler (PgBouncer, transaction mode): server-side prepared
    # statements do not survive it, so they are off. On Vercel each function instance is
    # short-lived, so it opens a connection per request and leaves pooling to the pooler.
    engine = create_engine(
        get_settings().database_url,
        poolclass=NullPool if os.environ.get("VERCEL") else None,
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )
else:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
