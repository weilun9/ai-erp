"""/inbox: scan intake — upload supplier papers, recognise, review, confirm into drafts."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.services import intake as svc

router = APIRouter(prefix="/inbox", tags=["inbox"])

DocKind = Literal["INVOICE", "CN", "PAYMENT", "BANK", "RETURN_NOTE", "OTHER"]
Source = Literal["UPLOAD", "SCANNER", "PHONE", "WHATSAPP", "EMAIL"]


class FileOut(BaseModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    source: str
    status: str
    doc_kind: str | None
    supplier_id: uuid.UUID | None
    doc_no: str | None
    total: Decimal | None
    location_id: uuid.UUID | None
    duplicate_of: uuid.UUID | None
    linked_doc_type: str | None
    linked_doc_id: uuid.UUID | None
    note: str | None
    created_at: datetime
    confirmed_at: datetime | None


class ExtractionOut(BaseModel):
    id: uuid.UUID
    provider: str
    model: str | None
    status: str
    result: dict[str, Any]
    confidence: dict[str, Any]
    input_tokens: int
    output_tokens: int
    seconds: Decimal | None
    error: str | None
    created_at: datetime


class Review(BaseModel):
    kind: str
    data: dict[str, Any]
    fields: dict[str, str]  # path -> TRUSTED / CHECK / MISSING
    findings: list[dict[str, Any]]
    ready: bool


class FileDetail(FileOut):
    extraction: ExtractionOut | None = None
    review: Review | None = None


class Page(BaseModel):
    items: list[FileOut]
    total: int


def _cols(model: type[BaseModel], obj) -> dict[str, Any]:
    return {c: getattr(obj, c) for c in model.model_fields if hasattr(obj, c)}


def _detail(db, p, f, review: dict[str, Any] | None = None) -> FileDetail:
    out = FileDetail(**_cols(FileOut, f))
    ex = svc.latest_extraction(db, f.id)
    if ex is not None:
        out.extraction = ExtractionOut(**_cols(ExtractionOut, ex))
        if review is None and ex.status == "DONE" and f.status == "EXTRACTED":
            review = svc.review(db, p, ex.result.get("kind"), ex.result, (), f)
    if review is not None:
        out.review = Review(**review)
    return out


@router.post("", status_code=201)
async def upload(
    p: CurrentUser,
    db: DB,
    file: Annotated[UploadFile, File()],
    source: Annotated[Source, Form()] = "UPLOAD",
    location_id: Annotated[uuid.UUID | None, Form()] = None,
    hint: Annotated[DocKind | None, Form()] = None,
):
    content = await file.read()
    f, already = svc.upload(
        db,
        p,
        filename=file.filename or "upload",
        content=content,
        source=source,
        location_id=location_id,
        hint=hint,
    )
    body = FileOut(**_cols(FileOut, f)).model_dump(mode="json")
    if already:  # the same file again: point at the existing entry
        return JSONResponse({**body, "already_uploaded": True}, status_code=200)
    return {**body, "already_uploaded": False}


@router.get("")
def list_files(
    p: CurrentUser,
    db: DB,
    status: Literal["NEW", "EXTRACTED", "CONFIRMED", "REJECTED"] | None = None,
    doc_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page:
    rows, total = svc.list_files(
        db, p, status=status, doc_id=doc_id, page=page, page_size=page_size
    )
    return Page(items=[FileOut(**_cols(FileOut, f)) for f in rows], total=total)


@router.get("/{file_id}")
def get_file(file_id: uuid.UUID, p: CurrentUser, db: DB) -> FileDetail:
    return _detail(db, p, svc.get_file(db, p, file_id))


@router.get("/{file_id}/content")
def content(file_id: uuid.UUID, p: CurrentUser, db: DB) -> FileResponse:
    f, path = svc.file_path(db, p, file_id)
    return FileResponse(
        path,
        media_type=f.content_type,
        filename=f.filename,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


class ExtractIn(BaseModel):
    hint: DocKind | None = None


@router.post("/{file_id}/extract")
def extract(file_id: uuid.UUID, body: ExtractIn, p: CurrentUser, db: DB) -> FileDetail:
    f, review = svc.extract(db, p, file_id, body.hint)
    return _detail(db, p, f, review)


class ReviewIn(BaseModel):
    kind: DocKind
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/{file_id}/review")
def review(file_id: uuid.UUID, body: ReviewIn, p: CurrentUser, db: DB) -> Review:
    """Run the checks on the data as the user has edited it (nothing is saved)."""
    f = svc.get_file(db, p, file_id)
    return Review(**svc.review(db, p, body.kind, body.data, (), f))


class ConfirmIn(ReviewIn):
    location_id: uuid.UUID | None = None
    completeness: Literal["RECORDED", "PROVISIONAL", "COMPLETE"] | None = None  # CN only


class ConfirmOut(BaseModel):
    file: FileOut
    doc_type: str
    doc_id: uuid.UUID


@router.post("/{file_id}/confirm")
def confirm(file_id: uuid.UUID, body: ConfirmIn, p: CurrentUser, db: DB) -> ConfirmOut:
    f, doc_type, doc_id = svc.confirm(
        db,
        p,
        file_id,
        kind=body.kind,
        data=body.data,
        location_id=body.location_id,
        completeness=body.completeness,
    )
    return ConfirmOut(file=FileOut(**_cols(FileOut, f)), doc_type=doc_type, doc_id=doc_id)


class RejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/{file_id}/reject")
def reject(file_id: uuid.UUID, body: RejectIn, p: CurrentUser, db: DB) -> FileOut:
    return FileOut(**_cols(FileOut, svc.reject(db, p, file_id, body.reason)))
