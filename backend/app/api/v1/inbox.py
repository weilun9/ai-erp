"""/inbox: scan intake — upload supplier papers, recognise, review, confirm into drafts."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import DB, CurrentUser
from app.core.errors import Unauthenticated
from app.services import intake as svc
from app.services import scan_tokens

router = APIRouter(prefix="/inbox", tags=["inbox"])

DocKind = Literal["INVOICE", "CN", "PAYMENT", "BANK", "RETURN_NOTE", "OTHER"]
Source = Literal["UPLOAD", "SCANNER", "PHONE", "WHATSAPP", "EMAIL", "DOC2EXCEL"]


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
    queue: bool = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page:
    """`queue=true`: what is still to be handled, oldest first."""
    rows, total = svc.list_files(
        db, p, status=status, doc_id=doc_id, queue=queue, page=page, page_size=page_size
    )
    return Page(items=[FileOut(**_cols(FileOut, f)) for f in rows], total=total)


# --- scanning apps (Doc2Excel) push papers in with a scan token ------------------------------


class ScanTokenOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime | None


class ScanTokenNew(ScanTokenOut):
    token: str  # shown once
    upload_url: str  # relative to the ERP address: POST here with the file


@router.post("/tokens", status_code=201)
def new_scan_token(p: CurrentUser, db: DB) -> ScanTokenNew:
    """A token for a scanning app on this computer (Doc2Excel): it may only upload papers into
    this scan queue, as you. Shown once; revoke it here or by signing the computer out."""
    sess, token = scan_tokens.issue(db, p)
    return ScanTokenNew(
        id=sess.id,
        created_at=sess.created_at,
        last_used_at=sess.last_used_at,
        token=token,
        upload_url="/api/v1/inbox/remote",
    )


@router.get("/tokens")
def list_scan_tokens(p: CurrentUser, db: DB) -> list[ScanTokenOut]:
    return [ScanTokenOut.model_validate(t, from_attributes=True) for t in scan_tokens.active(db, p)]


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_scan_token(token_id: uuid.UUID, p: CurrentUser, db: DB) -> None:
    scan_tokens.revoke(db, p, token_id)


def _scan_principal(db, authorization: str | None):
    token = (authorization or "").removeprefix("Bearer ").strip()
    p = scan_tokens.principal(db, token) if token else None
    if p is None:
        raise Unauthenticated(
            "Scan token missing, revoked or not allowed; make a new one in the ERP"
        )
    return p


class RemotePing(BaseModel):
    ok: bool
    user: str
    company_id: uuid.UUID
    queue: int


@router.get("/remote/ping")
def remote_ping(db: DB, authorization: Annotated[str | None, Header()] = None) -> RemotePing:
    """For the scanning app's "Test connection" button."""
    p = _scan_principal(db, authorization)
    _, waiting = svc.list_files(db, p, queue=True, page_size=1)
    return RemotePing(ok=True, user=p.display_name, company_id=p.company_id, queue=waiting)


@router.post("/remote", status_code=201)
async def remote_upload(
    db: DB,
    file: Annotated[UploadFile, File()],
    authorization: Annotated[str | None, Header()] = None,
    location_id: Annotated[uuid.UUID | None, Form()] = None,
    hint: Annotated[DocKind | None, Form()] = None,
):
    """A scanning app sends one paper into the scan queue (it is read and checked on the
    scan screen, like any upload). The same file twice points at the first one."""
    p = _scan_principal(db, authorization)
    content = await file.read()
    f, already = svc.upload(
        db,
        p,
        filename=file.filename or "scan",
        content=content,
        source="DOC2EXCEL",
        location_id=location_id,
        hint=hint,
    )
    body = FileOut(**_cols(FileOut, f)).model_dump(mode="json")
    return JSONResponse({**body, "already_uploaded": already}, status_code=200 if already else 201)


class EngineOut(BaseModel):
    ready: bool
    model: str | None


@router.get("/engine")
def engine(p: CurrentUser) -> EngineOut:
    """Is a recognition (vision) model set up? Without one, documents are keyed in by hand."""
    p.require("pur.doc.read")
    ex = svc.vision.get_extractor()
    return EngineOut(ready=ex is not None, model=getattr(ex, "model", None) if ex else None)


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
