"""SST return (in the shape of the SST-02 form) for a taxable period.

Malaysian SST is charged once, on the sale: sales tax on taxable goods (5% / 10%) and
service tax on taxable services (6% / 8%). For the period this adds up, per tax code, the
taxable value and the tax on everything the shop charged - posted sales invoices, customer
debit notes and till sales - less customer credit notes and till refunds. Voided documents
are left out. Documents count on their document date; till sales on the shop's business day.

It is a working paper for filling in the return on MySST, not a submission to Customs.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.localization.my import TIMEZONE
from app.models import (
    CustomerCn,
    CustomerCnLine,
    CustomerDebitNote,
    CustomerDebitNoteLine,
    PosLine,
    PosSale,
    SalesInvoice,
    SalesInvoiceLine,
    TaxCode,
)
from app.services.auth import Principal
from app.services.doclib import q2

ZERO = Decimal(0)
KIND = {"01": "SALES", "02": "SERVICE", "E": "EXEMPT", "06": "NOT_TAXABLE"}


def _docs(db, p, header, line, value_col, sign, date_from, date_to):
    """([(tax code, value, tax)], number of documents) for one kind of posted document."""
    where = (
        header.company_id == p.company_id,
        header.status == "POSTED",
        header.doc_date.between(date_from, date_to),
    )
    stmt = (
        select(line.tax_code_id, func.sum(value_col), func.sum(line.line_tax))
        .join(header, header.id == line.header_id)
        .where(*where)
        .group_by(line.tax_code_id)
    )
    n = db.scalar(select(func.count()).select_from(header).where(*where)) or 0
    return [(code, sign * (v or ZERO), sign * (t or ZERO)) for code, v, t in db.execute(stmt)], n


def _pos(db, p, kind, date_from, date_to):
    day = cast(func.timezone(TIMEZONE, PosSale.sold_at), Date)
    where = (
        PosSale.company_id == p.company_id,
        PosSale.kind == kind,
        day.between(date_from, date_to),
    )
    stmt = (
        select(PosLine.tax_code_id, func.sum(PosLine.line_subtotal), func.sum(PosLine.line_tax))
        .join(PosSale, PosSale.id == PosLine.sale_id)
        .where(*where)
        .group_by(PosLine.tax_code_id)
    )
    n = db.scalar(select(func.count()).select_from(PosSale).where(*where)) or 0
    return [(code, v or ZERO, t or ZERO) for code, v, t in db.execute(stmt)], n


def sst_return(db: Session, p: Principal, date_from: date, date_to: date) -> dict[str, Any]:
    p.require("report.run")
    if date_to < date_from:
        raise BadRequest("The period ends before it starts")
    if (date_to - date_from).days > 366:
        raise BadRequest("A taxable period is at most a year")
    codes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == p.company_id))}

    sources = {
        "invoices": _docs(
            db,
            p,
            SalesInvoice,
            SalesInvoiceLine,
            SalesInvoiceLine.line_subtotal,
            1,
            date_from,
            date_to,
        ),
        "debit_notes": _docs(
            db,
            p,
            CustomerDebitNote,
            CustomerDebitNoteLine,
            CustomerDebitNoteLine.amount,
            1,
            date_from,
            date_to,
        ),
        "till_sales": _pos(db, p, "SALE", date_from, date_to),
        "credit_notes": _docs(
            db, p, CustomerCn, CustomerCnLine, CustomerCnLine.line_subtotal, -1, date_from, date_to
        ),
        "till_refunds": _pos(db, p, "RETURN", date_from, date_to),  # already negative
    }
    rows: dict[Any, dict[str, Any]] = {}
    counts: dict[str, int] = defaultdict(int)
    for source, (data, n) in sources.items():
        if n:
            counts[source] = n
        for code_id, value, tax in data:
            t = codes.get(code_id)
            key = code_id or "none"
            r = rows.setdefault(
                key,
                {
                    "tax_code_id": code_id,
                    "code": t.code if t else "—",
                    "name": t.name if t else "No tax code",
                    "rate": t.rate if t else ZERO,
                    "kind": KIND.get(t.einvoice_tax_type, "NOT_TAXABLE") if t else "NOT_TAXABLE",
                    "sales_value": ZERO,
                    "sales_tax": ZERO,
                    "less_value": ZERO,
                    "less_tax": ZERO,
                },
            )
            if source in ("credit_notes", "till_refunds"):
                r["less_value"] += -value
                r["less_tax"] += -tax
            else:
                r["sales_value"] += value
                r["sales_tax"] += tax
    out = []
    for r in rows.values():
        r = {k: (q2(v) if isinstance(v, Decimal) and k != "rate" else v) for k, v in r.items()}
        r["net_value"] = r["sales_value"] - r["less_value"]
        r["net_tax"] = r["sales_tax"] - r["less_tax"]
        out.append(r)
    order = {"SALES": 0, "SERVICE": 1, "EXEMPT": 2, "NOT_TAXABLE": 3}
    out.sort(key=lambda r: (order[r["kind"]], -r["rate"], r["code"]))

    def total(kind: str, field: str) -> Decimal:
        return sum((r[field] for r in out if r["kind"] == kind), ZERO)

    sales_tax, service_tax = total("SALES", "net_tax"), total("SERVICE", "net_tax")
    return {
        "date_from": date_from,
        "date_to": date_to,
        "rows": out,
        "taxable_sales_value": total("SALES", "net_value"),
        "taxable_service_value": total("SERVICE", "net_value"),
        "exempt_value": total("EXEMPT", "net_value"),
        "not_taxable_value": total("NOT_TAXABLE", "net_value"),
        "sales_tax_payable": sales_tax,
        "service_tax_payable": service_tax,
        "total_payable": sales_tax + service_tax,
        "documents": dict(counts),
    }
