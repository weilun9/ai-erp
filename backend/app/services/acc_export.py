"""Accounting export for an outside accountant or another accounting package.

XLSX: one workbook with every journal line of the period (the import format most packages
take), the trial balance at the end of the period, profit and loss for the period, the
balance sheet and the chart of accounts. CSV: the journal lines only (UTF-8 with a byte
order mark, so Excel opens it with the right characters).
"""

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.models import Account, Company, Customer, Journal, JournalLine, Supplier
from app.services import ledger
from app.services.auth import Principal

PERM = "acc.period.close"  # "Month-end close and accounting export"
JOURNAL_HEAD = [
    "Date",
    "Journal no",
    "Source",
    "Source no",
    "Memo",
    "Account code",
    "Account name",
    "Account type",
    "Debit",
    "Credit",
    "Party type",
    "Party code",
    "Party name",
    "Status",
]


def _journal_rows(db: Session, p: Principal, date_from: date, date_to: date) -> list[list[Any]]:
    accounts = {
        a.id: a for a in db.scalars(select(Account).where(Account.company_id == p.company_id))
    }
    parties: dict[Any, tuple[str, str]] = {}
    for model, kind in ((Customer, "C"), (Supplier, "S")):
        for x in db.scalars(select(model).where(model.company_id == p.company_id)):
            parties[(kind, x.id)] = (x.code, x.name)
    rows = db.execute(
        select(JournalLine, Journal)
        .join(Journal, Journal.id == JournalLine.journal_id)
        .where(Journal.company_id == p.company_id, Journal.journal_date.between(date_from, date_to))
        .order_by(Journal.journal_date, Journal.doc_no, JournalLine.line_no)
    ).all()
    out = []
    for ln, j in rows:
        a = accounts[ln.account_id]
        code, name = parties.get((ln.party_type, ln.party_id), ("", ""))
        out.append(
            [
                j.journal_date,
                j.doc_no,
                j.source_type,
                j.source_no or "",
                ln.memo or j.memo or "",
                a.code,
                a.name,
                a.type,
                ln.debit,
                ln.credit,
                {"C": "Customer", "S": "Supplier"}.get(ln.party_type or "", ""),
                code,
                name,
                "Reversal" if j.is_reversal else ("Voided" if j.status == "VOIDED" else "Posted"),
            ]
        )
    return out


def export_csv(db: Session, p: Principal, date_from: date, date_to: date) -> bytes:
    p.require(PERM)
    _check(date_from, date_to)
    ledger.sync(db, p.company_id)
    db.commit()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(JOURNAL_HEAD)
    for r in _journal_rows(db, p, date_from, date_to):
        w.writerow([x.isoformat() if isinstance(x, date) else x for x in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _check(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise BadRequest("The period ends before it starts")


HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F2937")
MONEY = "#,##0.00;[Red]-#,##0.00"


def _sheet(wb: Workbook, title: str, head: list[str], rows: list[list[Any]], money_cols=()):
    ws = wb.create_sheet(title)
    ws.append(head)
    for c in ws[1]:
        c.font, c.fill = HEAD_FONT, HEAD_FILL
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([float(x) if isinstance(x, Decimal) else x for x in r])
    for col in money_cols:
        for cell in ws.iter_cols(min_col=col, max_col=col, min_row=2):
            for c in cell:
                c.number_format = MONEY
    for i, h in enumerate(head, start=1):
        width = max(
            [len(str(h))] + [len(str(r[i - 1])) for r in rows[:500] if r[i - 1] is not None]
        )
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 50)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def export_xlsx(db: Session, p: Principal, date_from: date, date_to: date) -> bytes:
    p.require(PERM)
    _check(date_from, date_to)
    tb = ledger.trial_balance(db, p, date_to)  # also books any document not booked yet
    pl = ledger.profit_and_loss(db, p, date_from, date_to)
    bs = ledger.balance_sheet(db, p, date_to)
    company = db.get(Company, p.company_id)

    wb = Workbook()
    info = wb.active
    info.title = "About"
    for r in (
        ["Company", company.name if company else ""],
        ["Period", f"{date_from.isoformat()} to {date_to.isoformat()}"],
        ["Currency", "MYR"],
        ["Made by", "AI Butler ERP"],
        ["Note", "Journals are made from posted documents; a void adds a reversing journal."],
    ):
        info.append(r)
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 12, 80
    for c in info["A"]:
        c.font = Font(bold=True)

    _sheet(
        wb, "Journals", JOURNAL_HEAD, _journal_rows(db, p, date_from, date_to), money_cols=(9, 10)
    )
    _sheet(
        wb,
        "Trial balance",
        ["Code", "Account", "Type", "Debit", "Credit"],
        [
            [r["code"], r["name"], r["type"], r["debit_balance"], r["credit_balance"]]
            for r in tb["rows"]
        ]
        + [["", "Total", "", tb["total_debit"], tb["total_credit"]]],
        money_cols=(4, 5),
    )
    _sheet(
        wb,
        "Profit and loss",
        ["Code", "Account", "Section", "Amount"],
        [[r["code"], r["name"], "Income", r["balance"]] for r in pl["income"]]
        + [[r["code"], r["name"], "Expense", r["balance"]] for r in pl["expense"]]
        + [
            ["", "Net sales", "", pl["net_sales"]],
            ["", "Gross profit", "", pl["gross_profit"]],
            ["", "Net profit", "", pl["net_profit"]],
        ],
        money_cols=(4,),
    )
    _sheet(
        wb,
        "Balance sheet",
        ["Code", "Account", "Section", "Amount"],
        [[r["code"], r["name"], "Assets", r["balance"]] for r in bs["assets"]]
        + [[r["code"], r["name"], "Liabilities", r["balance"]] for r in bs["liabilities"]]
        + [[r["code"], r["name"], "Equity", r["balance"]] for r in bs["equity"]]
        + [
            ["", "Profit since the last year-end close", "Equity", bs["profit_to_date"]],
            ["", "Total assets", "", bs["total_assets"]],
            ["", "Total liabilities + equity", "", bs["total_liabilities"] + bs["total_equity"]],
        ],
        money_cols=(4,),
    )
    _sheet(
        wb,
        "Chart of accounts",
        ["Code", "Account", "Type", "System key", "Active"],
        [
            [a.code, a.name, a.type, a.system_key or "", "yes" if a.is_active else "no"]
            for a in db.scalars(
                select(Account).where(Account.company_id == p.company_id).order_by(Account.code)
            )
        ],
    )
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
