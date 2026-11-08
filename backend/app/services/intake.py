"""Scan intake: upload -> recognise -> check -> a person reviews -> confirm into a draft.

1. upload   the file is fingerprinted (SHA-256); the same file twice is the same entry.
2. extract  the vision model reads it into one JSON shape (vision.py); off = type by hand.
3. review   automatic checks on whatever the data is now (model output or the user's
            edits): supplier and items matched, numbers add up, number format, dates,
            already-recorded documents. Every field is TRUSTED / CHECK / MISSING.
4. confirm  the reviewed data becomes a DRAFT document (GRN, supplier CN, supplier
            payment) or a bank record; the file stays linked to it as evidence.
Nothing is posted here: drafts are saved and posted by a person as usual.
"""

import hashlib
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import BadRequest, BusinessRule, DocState, NotFound
from app.models import (
    BankTxn,
    Grn,
    InboxExtraction,
    InboxFile,
    Item,
    Location,
    Supplier,
    SupplierCn,
    SupplierPayment,
)
from app.services import audit, finance, masterdata, purchasing, vision
from app.services.auth import Principal
from app.services.doclib import today
from app.services.vision import day, num, text

TRUSTED, CHECK, MISSING = "TRUSTED", "CHECK", "MISSING"
TOLERANCE = Decimal("0.05")
KINDS = ("INVOICE", "CN", "PAYMENT", "BANK", "RETURN_NOTE", "OTHER")
NEEDS_SUPPLIER = ("INVOICE", "CN", "PAYMENT", "RETURN_NOTE")
BLOCKING = {"DUPLICATE_DOC"}

# Accepted files, recognised by their first bytes (not by the name the user gave).
MAGIC = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
)


def _sniff(content: bytes) -> tuple[str, str]:
    for magic, mime, ext in MAGIC:
        if content.startswith(magic):
            return mime, ext
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise BadRequest("Only PDF, JPEG, PNG and WEBP files can be uploaded")


def _storage(key: str) -> Path:
    return get_settings().inbox_path / key


# --- files --------------------------------------------------------------------------------


def upload(
    db: Session,
    p: Principal,
    *,
    filename: str,
    content: bytes,
    source: str = "UPLOAD",
    location_id: uuid.UUID | None = None,
    hint: str | None = None,
) -> tuple[InboxFile, bool]:
    """Returns (file, already_uploaded)."""
    p.require("pur.doc.create")
    limit = get_settings().inbox_max_mb * 1024 * 1024
    if not content:
        raise BadRequest("The file is empty")
    if len(content) > limit:
        raise BadRequest("File is too large", {"max_mb": get_settings().inbox_max_mb})
    mime, ext = _sniff(content)
    if location_id:
        loc = db.get(Location, location_id)
        if loc is None or loc.company_id != p.company_id or not p.can_see_location(loc.id):
            raise BadRequest("Location not found")
    sha = hashlib.sha256(content).hexdigest()
    existing = db.scalar(
        select(InboxFile).where(InboxFile.company_id == p.company_id, InboxFile.sha256 == sha)
    )
    if existing is not None:
        return existing, True

    key = f"{p.company_id}/{sha[:2]}/{sha}{ext}"
    path = _storage(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
    f = InboxFile(
        company_id=p.company_id,
        location_id=location_id,
        sha256=sha,
        filename=Path(filename or "upload").name[:255],
        content_type=mime,
        size_bytes=len(content),
        storage_key=key,
        source=source,
        doc_kind=hint if hint in KINDS else None,
        uploaded_by=p.user_id,
    )
    db.add(f)
    db.flush()
    audit.record(db, p, "upload", "pur.inbox_file", f.id, after={"filename": f.filename})
    db.commit()
    return f, False


def get_file(db: Session, p: Principal, file_id: uuid.UUID, *, lock: bool = False) -> InboxFile:
    p.require("pur.doc.read")
    stmt = select(InboxFile).where(InboxFile.id == file_id, InboxFile.company_id == p.company_id)
    if lock:
        stmt = stmt.with_for_update()
    f = db.scalar(stmt.execution_options(populate_existing=True))
    if f is None:
        raise NotFound("File not found")
    return f


def list_files(
    db: Session,
    p: Principal,
    *,
    status: str | None = None,
    doc_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
):
    p.require("pur.doc.read")
    stmt = select(InboxFile).where(InboxFile.company_id == p.company_id)
    if status:
        stmt = stmt.where(InboxFile.status == status)
    if doc_id:
        stmt = stmt.where(InboxFile.linked_doc_id == doc_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(
        stmt.order_by(InboxFile.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return list(rows), total


def file_path(db: Session, p: Principal, file_id: uuid.UUID) -> tuple[InboxFile, Path]:
    f = get_file(db, p, file_id)
    path = _storage(f.storage_key)
    if not path.is_file():
        raise NotFound("The stored file is missing")
    return f, path


def latest_extraction(db: Session, file_id: uuid.UUID) -> InboxExtraction | None:
    return db.scalar(
        select(InboxExtraction)
        .where(InboxExtraction.file_id == file_id)
        .order_by(InboxExtraction.created_at.desc())
        .limit(1)
    )


# --- recognition --------------------------------------------------------------------------


def extract(db: Session, p: Principal, file_id: uuid.UUID, hint: str | None = None):
    p.require("pur.doc.create", "ai.vision.use")
    f = get_file(db, p, file_id)
    if f.status in ("CONFIRMED", "REJECTED"):
        raise DocState(f"This file is already {f.status.lower()}")
    extractor = vision.get_extractor()
    if extractor is None:
        raise BusinessRule(
            "Document recognition is not set up; enter the document by hand",
            {"setting": "VISION_BASE_URL / VISION_MODEL"},
        )
    content = _storage(f.storage_key).read_bytes()
    row = InboxExtraction(file_id=f.id, provider=extractor.name, created_by=p.user_id)
    try:
        got = extractor.extract(vision.to_pages(content, f.content_type), hint or f.doc_kind)
    except vision.ExtractionError as exc:
        row.status, row.error = "FAILED", str(exc)[:500]
        db.add(row)
        db.commit()
        raise BusinessRule(
            "Recognition failed; try again or enter by hand", {"error": row.error}
        ) from exc

    checked = review(db, p, hint or got.data.get("kind"), got.data, got.uncertain, f)
    row.status = "DONE"
    row.model = got.model
    row.result = checked["data"]
    row.confidence = checked["fields"]
    row.input_tokens, row.output_tokens = got.input_tokens, got.output_tokens
    row.seconds = Decimal(str(round(got.seconds, 2)))
    db.add(row)
    _remember(f, checked["data"])
    f.status = "EXTRACTED"
    db.commit()
    return f, checked


def _remember(f: InboxFile, data: dict[str, Any]) -> None:
    f.doc_kind = data.get("kind") or f.doc_kind
    f.supplier_id = uuid.UUID(data["supplier_id"]) if data.get("supplier_id") else None
    f.doc_no = data.get("doc_no")
    f.total = num(data.get("total"))


# --- checks -------------------------------------------------------------------------------


def _norm_no(no: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (no or "").upper())


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _match_supplier(db: Session, p: Principal, d: dict[str, Any]):
    if _uuid(d.get("supplier_id")):
        s = db.get(Supplier, _uuid(d["supplier_id"]))
        if s is not None and s.company_id == p.company_id:
            return s, TRUSTED, []
    suppliers = db.scalars(
        select(Supplier).where(Supplier.company_id == p.company_id, Supplier.is_active.is_(True))
    ).all()
    tin = (d.get("supplier_tin") or "").upper()
    if tin:
        hit = [s for s in suppliers if (s.tin or "").upper() == tin]
        if len(hit) == 1:
            return hit[0], TRUSTED, []
    name = re.sub(r"\s+", " ", (d.get("supplier_name") or "").upper()).strip()
    if not name:
        return None, MISSING, []
    exact = [s for s in suppliers if s.name.upper() == name or s.code.upper() == name]
    if len(exact) == 1:
        return exact[0], TRUSTED, []
    near = [s for s in suppliers if s.name.upper() in name or name in s.name.upper()]
    if len(near) == 1:
        return near[0], CHECK, []
    return None, MISSING, near


def _match_item(db: Session, p: Principal, ln: dict[str, Any]):
    """(item, uom from barcode, status, candidates)"""
    if _uuid(ln.get("item_id")):
        item = db.scalar(
            select(Item)
            .where(Item.id == _uuid(ln["item_id"]), Item.company_id == p.company_id)
            .options(selectinload(Item.uoms))
        )
        if item is not None:
            return item, None, TRUSTED, []
    if ln.get("barcode"):
        hits = masterdata.lookup_barcode(db, p, ln["barcode"])
        if len(hits) == 1:
            return hits[0][0], hits[0][1].uom, TRUSTED, []
        if len(hits) > 1:
            return None, None, CHECK, [h[0] for h in hits]
    desc = (ln.get("description") or "").strip()
    if len(desc) >= 3:
        found = db.scalars(
            select(Item)
            .where(
                Item.company_id == p.company_id,
                Item.is_active.is_(True),
                (Item.name.ilike(f"%{desc}%")) | (Item.short_name.ilike(f"%{desc}%")),
            )
            .options(selectinload(Item.uoms))
            .limit(5)
        ).all()
        if len(found) == 1:
            return found[0], None, CHECK, []
        return None, None, MISSING, list(found)
    return None, None, MISSING, []


def review(
    db: Session,
    p: Principal,
    kind: str | None,
    raw: dict[str, Any],
    uncertain: list[str] | tuple = (),
    f: InboxFile | None = None,
) -> dict[str, Any]:
    """Clean the data, match it to master data, and mark every field."""
    p.require("pur.doc.read")
    kind = (kind or raw.get("kind") or "OTHER").upper()
    if kind not in KINDS:
        raise BadRequest("Unknown document kind", {"allowed": list(KINDS)})
    fields: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    d: dict[str, Any] = {
        "kind": kind,
        "supplier_id": raw.get("supplier_id"),
        "supplier_name": text(raw.get("supplier_name")),
        "supplier_tin": text(raw.get("supplier_tin")),
        "doc_no": text(raw.get("doc_no")),
        "doc_date": day(raw.get("doc_date")),
        "ref_invoice_no": text(raw.get("ref_invoice_no")),
        "currency": text(raw.get("currency")) or "MYR",
        "subtotal": num(raw.get("subtotal")),
        "tax_total": num(raw.get("tax_total")),
        "total": num(raw.get("total")),
    }

    # supplier
    supplier = None
    if kind in NEEDS_SUPPLIER:
        supplier, st, near = _match_supplier(db, p, d)
        fields["supplier"] = st
        d["supplier_id"] = str(supplier.id) if supplier else None
        if supplier:
            d["supplier_name"] = supplier.name
        if near:
            findings.append(
                {
                    "code": "SUPPLIER_CANDIDATES",
                    "message": "Several suppliers could match; choose one",
                    "candidates": [{"id": str(s.id), "name": s.name} for s in near],
                }
            )

    # number and date
    if kind in ("INVOICE", "CN", "PAYMENT"):
        fields["doc_no"] = TRUSTED if d["doc_no"] else (MISSING if kind == "INVOICE" else CHECK)
        _check_number(db, p, kind, d, supplier, fields, findings, f)
    if kind != "OTHER":
        _check_date(db, p, d, fields, findings, "doc_date")

    if kind in ("INVOICE", "CN", "RETURN_NOTE"):
        _check_lines(db, p, kind, raw.get("lines") or [], d, fields, findings)
    if kind == "PAYMENT":
        _check_payment_lines(raw.get("payment_lines") or [], d, fields, findings)
    if kind == "BANK":
        b = raw.get("bank") or {}
        d["bank"] = {
            "date": day(b.get("date")) or d["doc_date"],
            "amount": num(b.get("amount")) or d["total"],
            "direction": (text(b.get("direction")) or "OUT").upper(),
            "counterparty": text(b.get("counterparty")),
            "reference": text(b.get("reference")),
        }
        fields["bank.date"] = TRUSTED if d["bank"]["date"] else MISSING
        fields["bank.amount"] = TRUSTED if d["bank"]["amount"] else MISSING
        fields["bank.direction"] = TRUSTED if b.get("direction") else CHECK
        if d["bank"]["direction"] not in ("IN", "OUT"):
            d["bank"]["direction"], fields["bank.direction"] = "OUT", CHECK

    # what the model itself was unsure about
    for path in uncertain:
        for key in list(fields):
            if key == path or key.startswith(path + "."):
                if fields[key] == TRUSTED:
                    fields[key] = CHECK
        if path not in fields:
            fields[path] = CHECK

    ready = (
        kind != "OTHER"
        and MISSING not in {v for k, v in fields.items() if _required(kind, k)}
        and not any(x["code"] in BLOCKING for x in findings)
    )
    return {
        "kind": kind,
        "data": audit.jsonable(d),
        "fields": fields,
        "findings": audit.jsonable(findings),
        "ready": ready,
    }


def _required(kind: str, path: str) -> bool:
    """Fields that must be filled before confirming (the rest are informational)."""
    if path in ("supplier", "doc_date"):
        return kind in NEEDS_SUPPLIER
    if path == "doc_no":
        return kind == "INVOICE"
    if path.startswith("lines."):
        part = path.rsplit(".", 1)[1]
        if kind == "RETURN_NOTE":
            return False  # a vague return note may still be recorded
        return part in ("item", "uom", "qty") or (kind == "INVOICE" and part == "unit_price")
    if path.startswith("payment_lines."):
        return path.rsplit(".", 1)[1] in ("line_type", "ref_no", "amount")
    if path.startswith("bank."):
        return path in ("bank.date", "bank.amount")
    return False


def _check_number(db, p, kind, d, supplier, fields, findings, f) -> None:
    no = d["doc_no"]
    if not no or supplier is None:
        return
    pattern = (supplier.profile or {}).get(
        {"INVOICE": "invoice_no_pattern", "CN": "cn_no_pattern"}.get(kind, "")
    )
    if pattern and not re.match(pattern, no):
        fields["doc_no"] = CHECK
        findings.append(
            {
                "code": "DOC_NO_FORMAT",
                "message": "Number does not follow this supplier's usual format",
                "expected_pattern": pattern,
            }
        )
    model = {"INVOICE": Grn, "CN": SupplierCn, "PAYMENT": SupplierPayment}[kind]
    col = model.supplier_ref_no
    existing = db.scalar(
        select(model.doc_no).where(
            model.company_id == p.company_id,
            model.supplier_id == supplier.id,
            model.status.in_(("SAVED", "POSTED")),
            func.upper(func.regexp_replace(col, "[^0-9A-Za-z]", "", "g")) == _norm_no(no),
        )
    )
    if existing:
        findings.append(
            {
                "code": "DUPLICATE_DOC",
                "message": "This document is already recorded",
                "existing_doc_no": existing,
            }
        )
    same = select(InboxFile.filename).where(
        InboxFile.company_id == p.company_id,
        InboxFile.supplier_id == supplier.id,
        func.upper(func.regexp_replace(InboxFile.doc_no, "[^0-9A-Za-z]", "", "g")) == _norm_no(no),
        InboxFile.status.in_(("EXTRACTED", "CONFIRMED")),
    )
    if f is not None:
        same = same.where(InboxFile.id != f.id)
    other = db.scalar(same.limit(1))
    if other:
        findings.append(
            {
                "code": "DUPLICATE_FILE",
                "message": "Another uploaded file has the same supplier and number",
                "filename": other,
            }
        )


def _check_date(db, p, d, fields, findings, key) -> None:
    v = d[key]
    if v is None:
        fields[key] = MISSING
        return
    fields[key] = TRUSTED
    now = today(db, p)
    if v > now:
        fields[key] = CHECK
        findings.append({"code": "DATE_FUTURE", "message": "Date is in the future"})
    elif v < now - timedelta(days=180):
        fields[key] = CHECK
        findings.append({"code": "DATE_OLD", "message": "Date is more than six months ago"})


def _check_lines(db, p, kind, raw_lines, d, fields, findings) -> None:
    lines = []
    computed_sum = Decimal(0)
    all_priced = True
    for n, rl in enumerate(raw_lines):
        key = f"lines.{n}"
        item, bc_uom, st, cands = _match_item(db, p, rl)
        qty = num(rl.get("qty"))
        price = num(rl.get("unit_price"))
        disc = num(rl.get("discount_pct")) or Decimal(0)
        amount = num(rl.get("amount"))
        fields[f"{key}.item"] = st
        if cands:
            findings.append(
                {
                    "code": "ITEM_CANDIDATES",
                    "line": n + 1,
                    "message": "Several items could match; choose one",
                    "candidates": [
                        {"id": str(i.id), "code": i.code, "name": i.name} for i in cands
                    ],
                }
            )
        # unit
        uom = None
        printed = (text(rl.get("uom")) or "").upper()
        if item is not None:
            units = {u.uom.upper(): u.uom for u in item.uoms}
            if printed in units:
                uom, fields[f"{key}.uom"] = units[printed], TRUSTED
            elif bc_uom:
                uom, fields[f"{key}.uom"] = bc_uom, TRUSTED if not printed else CHECK
            elif len(units) == 1:
                uom, fields[f"{key}.uom"] = item.base_uom, CHECK
            else:
                fields[f"{key}.uom"] = MISSING
        else:
            fields[f"{key}.uom"] = MISSING if not printed else CHECK
        # quantity, price, amount
        fields[f"{key}.qty"] = TRUSTED if qty and qty > 0 else MISSING
        if price is None and amount is not None and qty:
            price = (amount / qty / ((100 - disc) / 100)).quantize(Decimal("0.0001"))
            fields[f"{key}.unit_price"] = CHECK
        else:
            fields[f"{key}.unit_price"] = TRUSTED if price is not None else MISSING
        if price is None:
            all_priced = False
        if qty and price is not None:
            line_value = (qty * price * (100 - disc) / 100).quantize(Decimal("0.01"))
            if amount is not None and abs(line_value - amount) > TOLERANCE:
                fields[f"{key}.amount"] = CHECK
                findings.append(
                    {
                        "code": "LINE_AMOUNT",
                        "line": n + 1,
                        "message": "Quantity x price does not give the printed amount",
                        "computed": str(line_value),
                        "printed": str(amount),
                    }
                )
            computed_sum += amount if amount is not None else line_value
        lines.append(
            {
                "item_id": str(item.id) if item else None,
                "item_code": item.code if item else None,
                "barcode": text(rl.get("barcode")),
                "description": text(rl.get("description")),
                "uom": uom or (printed or None),
                "qty": qty,
                "unit_price": price,
                "discount_pct": disc,
                "amount": amount,
            }
        )
    d["lines"] = lines
    if kind == "INVOICE" and not lines:
        fields["lines"] = MISSING
    expected = d["subtotal"]
    if expected is None and d["total"] is not None:
        expected = d["total"] - (d["tax_total"] or 0)
    if lines and all_priced and expected is not None and abs(computed_sum - expected) > TOLERANCE:
        fields["total"] = CHECK
        findings.append(
            {
                "code": "TOTAL_MISMATCH",
                "message": "Lines do not add up to the document total",
                "lines_sum": str(computed_sum),
                "document": str(expected),
            }
        )
    elif d["total"] is not None:
        fields["total"] = TRUSTED


def _check_payment_lines(raw_lines, d, fields, findings) -> None:
    sign = {"INVOICE": 1, "CN": -1, "FEE": -1, "OTHER": 1}
    out, net = [], Decimal(0)
    for n, rl in enumerate(raw_lines):
        key = f"payment_lines.{n}"
        t = (text(rl.get("line_type")) or "").upper()
        amt = num(rl.get("amount"))
        fields[f"{key}.line_type"] = TRUSTED if t in sign else MISSING
        fields[f"{key}.amount"] = TRUSTED if amt and amt != 0 else MISSING
        ref = text(rl.get("ref_no"))
        if t in ("INVOICE", "CN"):
            fields[f"{key}.ref_no"] = TRUSTED if ref else MISSING
        fee_type = (text(rl.get("fee_type")) or "").upper() or None
        if t == "FEE" and fee_type not in (
            "FREIGHT",
            "LISTING",
            "PROMOTION",
            "DISPLAY",
            "REBATE",
            "OTHER",
        ):
            fee_type, fields[f"{key}.fee_type"] = "OTHER", CHECK
        if t in sign and amt:
            amt = abs(amt) if t != "OTHER" else amt
            net += sign[t] * amt
        out.append(
            {
                "line_type": t or None,
                "ref_no": ref,
                "amount": amt,
                "fee_type": fee_type if t == "FEE" else None,
                "remark": text(rl.get("description")),
            }
        )
    d["payment_lines"] = out
    if not out:
        fields["payment_lines"] = MISSING
    if d["total"] is not None and out and abs(net - d["total"]) > TOLERANCE:
        fields["total"] = CHECK
        findings.append(
            {
                "code": "TOTAL_MISMATCH",
                "message": "Paid minus deducted does not give the payment total",
                "lines_net": str(net),
                "document": str(d["total"]),
            }
        )


# --- confirm / reject ---------------------------------------------------------------------


def confirm(
    db: Session,
    p: Principal,
    file_id: uuid.UUID,
    *,
    kind: str,
    data: dict[str, Any],
    location_id: uuid.UUID | None = None,
    completeness: str | None = None,
):
    """Turn the reviewed data into a DRAFT document linked to this file."""
    f = get_file(db, p, file_id)
    checked = review(db, p, kind, data, (), f)
    if not checked["ready"]:
        missing = sorted(
            k for k, v in checked["fields"].items() if v == MISSING and _required(kind, k)
        )
        raise BusinessRule(
            "Fill in the missing fields before confirming",
            {
                "missing": missing,
                "blocking": [x for x in checked["findings"] if x["code"] in BLOCKING],
            },
        )
    d = checked["data"]
    loc = location_id or f.location_id
    if checked["kind"] != "BANK" and loc is None:
        raise BadRequest("Choose the store / warehouse for this document")

    # Claim the file first so two people cannot confirm it twice.
    claimed = db.execute(
        update(InboxFile)
        .where(InboxFile.id == f.id, InboxFile.status.in_(("NEW", "EXTRACTED")))
        .values(status="CONFIRMED", confirmed_at=datetime.now(UTC), confirmed_by=p.user_id)
    ).rowcount
    if not claimed:
        db.rollback()
        raise DocState("This file is already confirmed or rejected")
    db.commit()
    try:
        doc_type, doc_id = _create(db, p, checked["kind"], d, loc, completeness, f)
    except Exception:
        db.rollback()
        db.execute(
            update(InboxFile)
            .where(InboxFile.id == f.id)
            .values(status="EXTRACTED" if latest_extraction(db, f.id) else "NEW", confirmed_at=None)
        )
        db.commit()
        raise
    f = get_file(db, p, file_id, lock=True)
    _remember(f, d)
    f.linked_doc_type, f.linked_doc_id = doc_type, doc_id
    db.add(
        InboxExtraction(
            file_id=f.id,
            provider="user",
            status="DONE",
            result=d,
            confidence=checked["fields"],
            created_by=p.user_id,
        )
    )
    audit.record(
        db, p, "confirm", "pur.inbox_file", f.id, after={"doc_type": doc_type, "doc_id": doc_id}
    )
    db.commit()
    return f, doc_type, doc_id


def _create(db, p, kind, d, loc, completeness, f) -> tuple[str, uuid.UUID]:
    header = {
        "doc_date": date.fromisoformat(d["doc_date"]) if d.get("doc_date") else today(db, p),
        "location_id": loc,
    }
    if kind in ("INVOICE", "CN", "RETURN_NOTE"):
        lines = [
            {
                "item_id": uuid.UUID(ln["item_id"]),
                "uom": ln["uom"],
                "qty": Decimal(ln["qty"]),
                "unit_price": Decimal(ln["unit_price"]) if ln.get("unit_price") else None,
                "discount_pct": Decimal(ln.get("discount_pct") or 0),
                "remark": (ln.get("description") or "")[:200] or None,
            }
            for ln in d["lines"]
            if ln.get("item_id") and ln.get("uom") and ln.get("qty")
        ]
        unread = [ln.get("description") for ln in d["lines"] if not ln.get("item_id")]
        remark = f"From scan {f.filename}"
        if unread:
            remark += "; not matched: " + ", ".join(x for x in unread if x)[:300]
        v = {
            **header,
            "supplier_id": uuid.UUID(d["supplier_id"]),
            "supplier_ref_no": d.get("doc_no"),
            "remark": remark[:500],
        }
        if kind == "INVOICE":
            doc = purchasing.create_doc(db, p, "grn", lines=lines, **v)
            return "GRN", doc.id
        if kind == "RETURN_NOTE":
            state = completeness or ("PROVISIONAL" if lines and not unread else "RECORDED")
        else:
            state = completeness or "COMPLETE"
        doc = purchasing.create_doc(
            db,
            p,
            "supplier-cn",
            lines=lines,
            completeness=state,
            ref_invoice_no=d.get("ref_invoice_no"),
            **v,
        )
        return "SCN", doc.id
    if kind == "PAYMENT":
        doc = finance.create_doc(
            db,
            p,
            "supplier-payment",
            lines=[
                {k: ln[k] for k in ("line_type", "ref_no", "amount", "fee_type", "remark")}
                for ln in d["payment_lines"]
            ],
            supplier_id=uuid.UUID(d["supplier_id"]),
            supplier_ref_no=d.get("doc_no"),
            remark=f"From scan {f.filename}",
            **header,
        )
        return "PV", doc.id
    if kind == "BANK":
        p.require("fin.doc.create")
        b = d["bank"]
        txn = BankTxn(
            company_id=p.company_id,
            txn_date=date.fromisoformat(b["date"]),
            direction=b["direction"],
            amount=Decimal(b["amount"]),
            counterparty=b.get("counterparty"),
            reference=b.get("reference"),
            source_file_id=f.id,
            created_by=p.user_id,
        )
        db.add(txn)
        db.commit()
        return "BANK", txn.id
    raise BadRequest("Documents of kind OTHER cannot be confirmed; reject the file instead")


def reject(db: Session, p: Principal, file_id: uuid.UUID, reason: str) -> InboxFile:
    p.require("pur.doc.create")
    f = get_file(db, p, file_id, lock=True)
    if f.status not in ("NEW", "EXTRACTED"):
        raise DocState(f"This file is already {f.status.lower()}")
    f.status, f.note = "REJECTED", reason
    audit.record(db, p, "reject", "pur.inbox_file", f.id, after={"reason": reason})
    db.commit()
    return f
