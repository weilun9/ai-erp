from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the API process is up."""
    return {"status": "ok"}


@router.get("/healthz/db")
def healthz_db(db: Annotated[Session, Depends(get_db)]) -> JSONResponse:
    """Readiness: the database answers and pgvector is installed."""
    try:
        db.execute(text("SELECT 1"))
        has_vector = db.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
        ).scalar_one()
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "error", "database": "down"})
    return JSONResponse(
        content={"status": "ok", "database": "up", "pgvector": bool(has_vector)}
    )
