"""/kb: the knowledge base the AI butler searches (rules, SOPs, supplier terms, manuals)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel

from app.api.deps import DB, CurrentUser
from app.services import kb

router = APIRouter(prefix="/kb", tags=["knowledge base"])


class DocumentOut(BaseModel):
    id: uuid.UUID
    doc_key: str
    version: int
    title: str
    category: str | None
    filename: str
    status: str
    chunks: int
    embed_model: str | None
    visible_tags: list[str] | None
    visible_location_ids: list[uuid.UUID] | None
    error: str | None
    created_at: datetime
    retired_at: datetime | None


class UploadOut(DocumentOut):
    warnings: list[str]


class ChunkOut(BaseModel):
    seq: int
    section: str | None
    page: int | None
    content: str
    has_vector: bool


class HitOut(BaseModel):
    title: str
    version: int
    section: str | None
    page: int | None
    content: str
    score: float
    keyword_rank: int | None
    vector_rank: int | None


def _doc(d) -> DocumentOut:
    return DocumentOut.model_validate(d, from_attributes=True)


def _split(value: str | None) -> list[str] | None:
    items = [x.strip() for x in (value or "").split(",") if x.strip()]
    return items or None


@router.post("/documents", status_code=201)
async def upload(
    p: CurrentUser,
    db: DB,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    doc_key: Annotated[str | None, Form(max_length=80)] = None,
    category: Annotated[str | None, Form()] = None,
    visible_tags: Annotated[str | None, Form(description="tag codes, comma separated")] = None,
    visible_location_ids: Annotated[str | None, Form(description="comma separated")] = None,
) -> UploadOut:
    """Same doc_key (default: the title) as a live document = a new version of it."""
    content = await file.read()
    locs = _split(visible_location_ids)
    doc, warnings = kb.upload(
        db,
        p,
        content=content,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        title=title,
        doc_key=doc_key,
        category=category,
        visible_tags=_split(visible_tags),
        visible_location_ids=[uuid.UUID(x) for x in locs] if locs else None,
    )
    return UploadOut(**_doc(doc).model_dump(), warnings=warnings)


@router.get("/documents")
def documents(p: CurrentUser, db: DB, include_retired: bool = False) -> list[DocumentOut]:
    return [_doc(d) for d in kb.documents(db, p, include_retired=include_retired)]


@router.get("/documents/{document_id}/chunks")
def chunks(document_id: uuid.UUID, p: CurrentUser, db: DB) -> list[ChunkOut]:
    """Preview how a document was cut up."""
    return [
        ChunkOut(
            seq=c.seq,
            section=c.section,
            page=c.page,
            content=c.content,
            has_vector=c.embedding is not None,
        )
        for c in kb.chunks(db, p, document_id)
    ]


@router.post("/documents/{document_id}/retire")
def retire(document_id: uuid.UUID, p: CurrentUser, db: DB) -> DocumentOut:
    return _doc(kb.retire(db, p, document_id))


@router.get("/search")
def search(
    p: CurrentUser,
    db: DB,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=20)] = 6,
) -> list[HitOut]:
    """Try a question: what the AI butler would be given (only what you may see)."""
    return [
        HitOut(**{k: getattr(h, k) for k in HitOut.model_fields})
        for h in kb.search(db, p, q, limit=limit)
    ]
