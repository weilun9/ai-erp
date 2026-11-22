"""Excel templates the AI butler hands out, and reading them back.

When the butler cannot draft a document (items not in the system yet, quantities or prices
unknown), it gives the user a template to fill: a Document sheet (who, which invoice, date,
store) and a Lines sheet (item code, and for an item that does not exist yet its name, unit
and selling price; quantity; price). What it already knows is filled in.

The filled file comes back through the chat. parse() checks every row and turns it into one
proposal - "create these new items, then this draft document" - which the user agrees to on
the usual card; nothing is written before that.
"""

import base64
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.models import Item, Uom
from app.services import butler_tools as tools
from app.services.auth import Principal

KINDS = {
    "grn": ("Goods received (supplier invoice)", "supplier", "Supplier invoice no"),
    "sales-invoice": ("Sales invoice", "customer", "Customer ref / PO"),
    "quotation": ("Quotation", "customer", "Customer ref"),
    "delivery-order": ("Delivery order", "customer", "Customer ref / PO"),
}
LINE_HEAD = [
    "Item code *",
    "Item name (new items)",
    "Unit",
    "Qty *",
    "Unit price",
    "Selling price (new items)",
    "Barcode (new items)",
]
HEAD = Font(bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="1F6FEB")
KNOWN = PatternFill("solid", fgColor="EAF2FF")
MAX_ROWS = 300


def encode_prefill(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_prefill(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BadRequest("The template link is damaged; ask the butler again") from exc
    return data if isinstance(data, dict) else {}


def make_template(db: Session, p: Principal, kind: str, prefill: dict[str, Any]) -> bytes:
    if kind not in KINDS:
        raise BadRequest("Unknown template", {"kinds": list(KINDS)})
    p.require("pur.doc.create" if kind == "grn" else "sal.doc.create")
    title, party, ref_label = KINDS[kind]
    units = [u for u in db.scalars(select(Uom.code).where(Uom.company_id == p.company_id))]

    wb = Workbook()
    doc = wb.active
    doc.title = "Document"
    rows = [
        ("Type", kind, "do not change"),
        (f"{party.title()} code *", prefill.get("party") or "", f"the {party}'s code in the ERP"),
        (ref_label, prefill.get("ref_no") or "", ""),
        ("Date", prefill.get("doc_date") or date.today().isoformat(), "YYYY-MM-DD"),
        ("Store code", prefill.get("store") or "", "empty = your only store"),
        ("Remark", prefill.get("remark") or "", ""),
    ]
    doc.append(["Field", "Value", "Note"])
    for r in rows:
        doc.append(list(r))
    for c in doc[1]:
        c.font, c.fill = HEAD, FILL
    for row in doc.iter_rows(min_row=2, max_row=len(rows) + 1, min_col=2, max_col=2):
        for c in row:
            if c.value:
                c.fill = KNOWN
    doc.column_dimensions["A"].width = 24
    doc.column_dimensions["B"].width = 34
    doc.column_dimensions["C"].width = 30

    lines = wb.create_sheet("Lines")
    lines.append(LINE_HEAD)
    for c in lines[1]:
        c.font, c.fill = HEAD, FILL
    for ln in (prefill.get("lines") or [])[:MAX_ROWS]:
        lines.append(
            [
                ln.get("code") or "",
                ln.get("name") or "",
                ln.get("unit") or "",
                ln.get("qty") or "",
                ln.get("unit_price") or "",
                ln.get("selling_price") or "",
                ln.get("barcode") or "",
            ]
        )
        for c in lines[lines.max_row]:
            if c.value not in (None, ""):
                c.fill = KNOWN
    widths = [16, 34, 9, 9, 12, 22, 18]
    for i, w in enumerate(widths):
        lines.column_dimensions[chr(65 + i)].width = w
    lines.freeze_panes = "A2"
    if units:
        dv = DataValidation(type="list", formula1=f'"{",".join(units[:40])}"', allow_blank=True)
        dv.add(f"C2:C{MAX_ROWS + 1}")
        lines.add_data_validation(dv)

    howto = wb.create_sheet("How to fill")
    for line in (
        f"{title} template from the AI butler.",
        "",
        "Document sheet: fill the code of the "
        f"{party} and the other values. Blue cells were filled in for you - check them.",
        "Lines sheet: one row per item.",
        "  - Item code: an item that exists in the ERP, or a new code for a new item.",
        "  - New items also need Item name and Unit (PCS, CTN...); Selling price and Barcode "
        "are optional.",
        "  - Qty is required."
        + (
            " Unit price is required (the supplier's price)."
            if kind == "grn"
            else " Unit price: empty = list price."
        ),
        "",
        "Then attach the file in the AI butler chat (the paper-clip button). The butler checks "
        "it and shows one card: the new items to create and the draft document. Nothing is "
        "saved until you press Agree.",
    ):
        howto.append([line])
    howto.column_dimensions["A"].width = 110

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, datetime):
        return v.date().isoformat()
    return str(v).strip()


def _num(v: Any) -> Decimal | None:
    t = _text(v).replace(",", "")
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return Decimal("NaN")


def parse(db: Session, p: Principal, content: bytes) -> dict[str, Any]:
    """-> {"errors": [...]} or {"summary": card, "payload": what Agree runs}."""
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # not an xlsx, damaged...
        raise BadRequest("That file is not an Excel workbook (.xlsx)") from exc
    if "Document" not in wb.sheetnames or "Lines" not in wb.sheetnames:
        raise BadRequest("Use the butler's template: it needs the Document and Lines sheets")
    fields = {
        _text(r[0]).rstrip(" *").lower(): r[1]
        for r in wb["Document"].iter_rows(min_row=2, values_only=True)
        if r and r[0]
    }
    kind = _text(fields.get("type"))
    if kind not in KINDS:
        raise BadRequest("The Type on the Document sheet was changed; use a fresh template")
    p.require("pur.doc.create" if kind == "grn" else "sal.doc.create")
    _, party_type, ref_label = KINDS[kind]
    errors: list[str] = []

    party = None
    party_code = _text(fields.get(f"{party_type} code"))
    try:
        party = tools.resolve_party(db, p, party_type, party_code) if party_code else None
    except tools.ToolError as exc:
        errors.append(f"Document: {exc}")
    if party is None and not errors:
        errors.append(f"Document: fill in the {party_type} code")
    store_code = _text(fields.get("store code")) or None
    try:
        loc = tools.resolve_location(db, p, store_code)
        if loc is None:
            stores = tools.dump(tools.locations.list_locations(p=p, db=db).items)
            if len(stores) == 1:
                loc = stores[0]["id"]
            else:
                errors.append("Document: fill in the store code")
    except tools.ToolError as exc:
        errors.append(f"Document: {exc}")
        loc = None
    raw_date = fields.get("date")
    try:
        doc_date = (
            raw_date.date()
            if isinstance(raw_date, datetime)
            else (date.fromisoformat(_text(raw_date)) if _text(raw_date) else date.today())
        )
    except ValueError:
        errors.append("Document: the date must be YYYY-MM-DD")
        doc_date = date.today()

    units = set(db.scalars(select(Uom.code).where(Uom.company_id == p.company_id)))
    lines, shown, new_items = [], [], {}
    for n, row in enumerate(wb["Lines"].iter_rows(min_row=2, values_only=True), start=2):
        if n > MAX_ROWS + 1:
            errors.append(f"Lines: at most {MAX_ROWS} rows")
            break
        row = (list(row) + [None] * 7)[:7]
        code, name, unit = _text(row[0]), _text(row[1]), _text(row[2]).upper()
        qty, price, sell, barcode = _num(row[3]), _num(row[4]), _num(row[5]), _text(row[6])
        if not any((code, name, qty, price)):
            continue  # empty row
        where = f"Lines row {n}"
        if not code:
            errors.append(f"{where}: the item code is missing")
            continue
        if qty is None or qty.is_nan() or qty <= 0:
            errors.append(f"{where} ({code}): Qty must be a number above 0")
            continue
        if price is not None and (price.is_nan() or price < 0):
            errors.append(f"{where} ({code}): Unit price is not a number")
            continue
        if kind == "grn" and price is None:
            errors.append(f"{where} ({code}): a goods received note needs the unit price")
            continue
        item = db.scalar(select(Item).where(Item.company_id == p.company_id, Item.code == code))
        if item is None and code.upper() in new_items:
            unit = unit or new_items[code.upper()]["base_uom"]
        elif item is None:
            if not name:
                errors.append(f"{where}: {code} is not in the ERP; give its Item name to create it")
                continue
            unit = unit or "PCS"
            if unit not in units:
                errors.append(f"{where} ({code}): unit {unit} does not exist")
                continue
            new_items[code.upper()] = {
                "code": code,
                "name": name[:200],
                "base_uom": unit,
                "uoms": [
                    {
                        "uom": unit,
                        "factor": "1",
                        "selling_price": str(sell)
                        if sell is not None and not sell.is_nan()
                        else None,
                        "purchase_price": str(price)
                        if kind == "grn" and price is not None
                        else None,
                    }
                ],
                "barcodes": [{"barcode": barcode, "uom": unit}] if barcode else [],
            }
        else:
            unit = unit or item.base_uom
        line = {"item_code": code, "uom": unit, "qty": str(qty)}
        if price is not None:
            line["unit_price"] = str(price)
        lines.append(line)
        shown.append(
            {
                "item": f"{code} {name or (item.name if item else '')}".strip()
                + (" (new)" if item is None else ""),
                "uom": unit,
                "qty": str(qty),
                "unit_price": str(price) if price is not None else "list",
            }
        )
    if not lines and not errors:
        errors.append("Lines: no rows filled in")
    if errors:
        return {"errors": errors[:30]}

    est = sum(
        (Decimal(x["qty"]) * Decimal(x["unit_price"]) for x in lines if "unit_price" in x),
        Decimal(0),
    )
    ref_field = "supplier_ref_no" if kind == "grn" else "customer_ref_no"
    body = {
        "doc_date": doc_date.isoformat(),
        f"{party_type}_id": party["id"],
        "location_id": loc,
        ref_field: _text(fields.get(ref_label.lower())) or None,
        "remark": _text(fields.get("remark"))[:500] or None,
        "lines": lines,
    }
    checks = []
    if new_items:
        checks.append(
            "New items will be created: "
            + ", ".join(f"{x['code']} {x['name']} ({x['base_uom']})" for x in new_items.values())
        )
    return {
        "summary": {
            "action": f"From your Excel: create a DRAFT {kind}",
            party_type: f"{party['code']} {party['name']}",
            "date": doc_date.isoformat(),
            "lines": shown,
            "estimated_subtotal": str(est.quantize(Decimal("0.01"))) if est else None,
            "checks": checks,
        },
        "payload": {"doc_type": kind, "body": body, "new_items": list(new_items.values())},
    }
