"""Tools the AI butler (and MCP clients) may call.

Every read tool calls the same route function the web screen uses, as the current user,
and serializes it the same way (`exclude_unset`), so permissions, store scope and hidden
fields (cost, PII) come out exactly as they do on screen. A cashier's tool results simply
have no cost in them, whatever the prompt says.

Write tools never write: they return a proposal that becomes an ai.pending_action, and
`execute` runs it only after the user presses confirm.

Results are kept small for the model: ids are replaced by codes and names, empty values
are dropped, lists are capped.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1 import finance, locations, masterdata, purchasing, sales, stock
from app.core.errors import AppError, NotFound
from app.models import Customer, Item, Location, Supplier
from app.services import kb, reports
from app.services.auth import Principal

MAX_ROWS = 50

SALES_KINDS = ("quotation", "delivery-order", "sales-invoice", "customer-cn")
PURCHASE_KINDS = ("grn", "supplier-cn")
FINANCE_KINDS = ("receipt", "supplier-payment")
DOC_TYPES = SALES_KINDS + PURCHASE_KINDS + FINANCE_KINDS
DRAFT_TYPES = ("quotation", "delivery-order", "sales-invoice", "grn")


class ToolError(Exception):
    """A problem the model should see and act on (ask the user, try other words)."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[Session, Principal, dict[str, Any]], Any]
    all_of: tuple[str, ...] = ()  # needs every one of these
    any_of: tuple[str, ...] = ()  # and at least one of these
    write: bool = False

    def allowed(self, p: Principal) -> bool:
        return all(p.has(c) for c in self.all_of) and (
            not self.any_of or any(p.has(c) for c in self.any_of)
        )


# --- output shaping -----------------------------------------------------------------------

LABELS = {
    "item_id": (Item, "item"),
    "customer_id": (Customer, "customer"),
    "supplier_id": (Supplier, "supplier"),
    "location_id": (Location, "location"),
}
DROP = {"id", "version", "tax_code_id", "source_id", "do_line_id", "category_id", "doc_id"}
DROP_SUFFIX = ("_tax_code_id",)


def dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    if isinstance(value, list):
        return [dump(v) for v in value]
    return value


def _collect(value: Any, found: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in LABELS and isinstance(v, str):
                found.setdefault(k, set()).add(v)
            else:
                _collect(v, found)
    elif isinstance(value, list):
        for v in value:
            _collect(v, found)


def _rewrite(value: Any, names: dict[str, dict[str, str]]) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if v is None or v == [] or k in DROP or k.endswith(DROP_SUFFIX):
                continue
            if k in LABELS and isinstance(v, str):
                out[LABELS[k][1]] = names.get(k, {}).get(v, v)
            else:
                out[k] = _rewrite(v, names)
        return out
    if isinstance(value, list):
        return [_rewrite(v, names) for v in value]
    return value


def humanize(db: Session, p: Principal, value: Any) -> Any:
    """ids -> 'CODE Name', drop empty and internal fields."""
    value = dump(value)
    found: dict[str, set[str]] = {}
    _collect(value, found)
    names: dict[str, dict[str, str]] = {}
    for key, ids in found.items():
        model = LABELS[key][0]
        rows = db.execute(
            select(model.id, model.code, model.name).where(
                model.company_id == p.company_id, model.id.in_([uuid.UUID(i) for i in ids])
            )
        )
        names[key] = {
            str(i): (code if model is Location else f"{code} {name}") for i, code, name in rows
        }
    return _rewrite(value, names)


def _capped(rows: list[Any], total: int | None = None) -> dict[str, Any]:
    total = len(rows) if total is None else total
    out: dict[str, Any] = {"rows": rows[:MAX_ROWS], "total": total}
    if total > MAX_ROWS:
        out["note"] = f"Showing {MAX_ROWS} of {total}; narrow the search for the rest"
    return out


# --- finding things by the words people use -----------------------------------------------


def _item_brief(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: item[k] for k in ("code", "name", "base_uom") if k in item}
    out["units"] = [
        {k: u[k] for k in ("uom", "factor", "selling_price", "purchase_price") if k in u}
        for u in item.get("uoms", [])
    ]
    out["barcodes"] = [b["barcode"] for b in item.get("barcodes", [])]
    return out


def find_items(db: Session, p: Principal, query: str, limit: int = 10) -> list[dict[str, Any]]:
    query = query.strip()
    if query.isdigit() and len(query) >= 8:
        hits = masterdata.lookup_barcode(p=p, db=db, barcode=query).hits
        found = [dump(h.item) for h in hits]
        if found:
            return found
    page = masterdata.search_items(
        p=p, db=db, q=query, category_id=None, active=True, page=1, page_size=limit
    )
    return dump(page.items)


def resolve_item(db: Session, p: Principal, text: str) -> dict[str, Any]:
    found = find_items(db, p, text, limit=10)
    exact = [i for i in found if i["code"].lower() == text.strip().lower()]
    if exact:
        return exact[0]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ToolError(f"No item matches '{text}'")
    raise ToolError(
        f"'{text}' matches several items; ask the user which one",
        candidates=[f"{i['code']} {i['name']}" for i in found],
    )


def resolve_party(db: Session, p: Principal, party_type: str, text: str) -> dict[str, Any]:
    fn = masterdata.list_customers if party_type == "customer" else masterdata.list_suppliers
    found = dump(fn(p=p, db=db, q=text.strip(), active=True, page=1, page_size=10).items)
    exact = [x for x in found if x["code"].lower() == text.strip().lower()]
    if exact:
        return exact[0]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ToolError(f"No {party_type} matches '{text}'")
    raise ToolError(
        f"'{text}' matches several {party_type}s; ask the user which one",
        candidates=[f"{x['code']} {x['name']}" for x in found],
    )


def resolve_location(db: Session, p: Principal, text: str | None) -> str | None:
    if not text:
        return None
    rows = dump(locations.list_locations(p=p, db=db).items)
    t = text.strip().lower()
    for loc in rows:
        if t in (loc["code"].lower(), loc["name"].lower()):
            return loc["id"]
    raise ToolError(
        f"No store '{text}' that this user can see",
        stores=[f"{x['code']} {x['name']}" for x in rows],
    )


def _date(value: Any, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError(f"{name} must be YYYY-MM-DD") from exc


def _choice(value: Any, options: tuple[str, ...], name: str) -> str:
    if value not in options:
        raise ToolError(f"{name} must be one of {list(options)}")
    return value


# --- read tools ---------------------------------------------------------------------------


def t_search_items(db, p, a):
    found = find_items(db, p, str(a.get("query", "")), limit=min(int(a.get("limit", 10)), 20))
    out = _capped([_item_brief(i) for i in found])
    if not found:
        out["note"] = "No item matches. Tell the user it is not in the item list."
    return out


def t_stock(db, p, a):
    item = resolve_item(db, p, str(a.get("item", "")))
    loc = resolve_location(db, p, a.get("location"))
    page = stock.balance(
        p=p,
        db=db,
        item_id=uuid.UUID(item["id"]),
        location_id=uuid.UUID(loc) if loc else None,
        page=1,
        page_size=100,
    )
    rows = humanize(db, p, page.items)
    for r in rows:
        for k in ("item", "item_code", "item_name", "base_uom"):
            r.pop(k, None)
    return {
        "item": f"{item['code']} {item['name']}",
        "base_uom": item["base_uom"],
        "by_store": rows,
    }


def t_find_parties(db, p, a):
    party_type = _choice(a.get("party_type"), ("customer", "supplier"), "party_type")
    fn = masterdata.list_customers if party_type == "customer" else masterdata.list_suppliers
    page = fn(
        p=p, db=db, q=str(a.get("query", "")).strip() or None, active=True, page=1, page_size=20
    )
    keep = ("code", "name", "tin", "term_days", "credit_limit", "phone", "city")
    return _capped(
        [{k: r[k] for k in keep if r.get(k) is not None} for r in dump(page.items)], page.total
    )


def _list(
    db,
    p,
    kind,
    *,
    party=None,
    status=None,
    date_from=None,
    date_to=None,
    q=None,
    page_size=MAX_ROWS,
):
    if kind in SALES_KINDS:
        return sales.list_docs(
            kind=kind,
            p=p,
            db=db,
            status=status,
            customer_id=party,
            location_id=None,
            date_from=date_from,
            date_to=date_to,
            q=q,
            page=1,
            page_size=page_size,
        )
    if kind in PURCHASE_KINDS:
        return purchasing.list_docs(
            kind=kind,
            p=p,
            db=db,
            status=status,
            supplier_id=party,
            location_id=None,
            date_from=date_from,
            date_to=date_to,
            q=q,
            page=1,
            page_size=page_size,
        )
    return finance.list_docs(
        kind=kind,
        p=p,
        db=db,
        status=status,
        party_id=party,
        date_from=date_from,
        date_to=date_to,
        page=1,
        page_size=page_size,
    )


def _party_type(kind: str) -> str:
    return "supplier" if kind in PURCHASE_KINDS or kind == "supplier-payment" else "customer"


def t_list_documents(db, p, a):
    kind = _choice(a.get("doc_type"), DOC_TYPES, "doc_type")
    party = None
    if a.get("party"):
        party = uuid.UUID(resolve_party(db, p, _party_type(kind), str(a["party"]))["id"])
    status = a.get("status")
    if status is not None:
        status = _choice(status, ("DRAFT", "SAVED", "POSTED", "VOIDED"), "status")
    page = _list(
        db,
        p,
        kind,
        party=party,
        status=status,
        date_from=_date(a.get("date_from"), "date_from"),
        date_to=_date(a.get("date_to"), "date_to"),
    )
    rows = humanize(db, p, page.items)
    for r in rows:
        r.pop("lines", None)
        r.pop("warnings", None)
    return _capped(rows, page.total)


def _find_doc(db, p, kind, doc_no):
    want = doc_no.strip().upper()
    q = want if kind not in FINANCE_KINDS else None
    for d in _list(db, p, kind, q=q, page_size=200).items:
        if (d.doc_no or "").upper() == want:
            return d.id
    raise ToolError(f"No {kind} numbered {doc_no} that this user can see")


def t_get_document(db, p, a):
    kind = _choice(a.get("doc_type"), DOC_TYPES, "doc_type")
    doc_id = _find_doc(db, p, kind, str(a.get("doc_no", "")))
    module = sales if kind in SALES_KINDS else purchasing if kind in PURCHASE_KINDS else finance
    return humanize(db, p, module.get_doc(kind=kind, doc_id=doc_id, p=p, db=db))


def t_open_items(db, p, a):
    party_type = _choice(a.get("party_type"), ("customer", "supplier"), "party_type")
    party = resolve_party(db, p, party_type, str(a.get("party", "")))
    out = humanize(
        db,
        p,
        finance.open_items(party_type=party_type, party_id=uuid.UUID(party["id"]), p=p, db=db),
    )
    out["party"] = f"{party['code']} {party['name']}"
    out["note"] = "balance > 0: they owe us (customer) / we owe them (supplier)"
    return out


def t_report(db, p, a):
    report = _choice(a.get("report"), ("sales_summary", "low_stock"), "report")
    loc = resolve_location(db, p, a.get("location"))
    loc_id = uuid.UUID(loc) if loc else None
    limit = min(int(a.get("limit", 20)), MAX_ROWS)
    if report == "low_stock":
        return reports.low_stock(
            db, p, threshold=Decimal(str(a.get("threshold", 5))), location_id=loc_id, limit=limit
        )
    date_from = _date(a.get("date_from"), "date_from")
    date_to = _date(a.get("date_to"), "date_to")
    if not date_from or not date_to:
        raise ToolError("sales_summary needs date_from and date_to")
    group_by = _choice(
        a.get("group_by", "day"), ("day", "item", "customer", "location"), "group_by"
    )
    return reports.sales_summary(
        db,
        p,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        location_id=loc_id,
        limit=limit,
    )


def t_issues(db, p, a):
    status = a.get("status", "OPEN")
    status = _choice(status, ("OPEN", "RESOLVED", "CANCELLED"), "status")
    rows = finance.list_issues(p=p, db=db, status=status, doc_id=None)
    keep = ("issue_type", "status", "title", "doc_type", "doc_no", "created_at", "resolution")
    return _capped(
        [{k: v for k, v in dump(r).items() if k in keep and v is not None} for r in rows]
    )


def t_locations(db, p, a):
    return [
        {"code": x["code"], "name": x["name"]}
        for x in dump(locations.list_locations(p=p, db=db).items)
    ]


def t_knowledge(db, p, a):
    query = str(a.get("query", "")).strip()
    if not query:
        raise ToolError("query is empty")
    return kb.cited(kb.search(db, p, query, limit=6))


# --- write tools (proposal only) ----------------------------------------------------------


def t_draft(db, p, a):
    kind = _choice(a.get("doc_type"), DRAFT_TYPES, "doc_type")
    p.require("pur.doc.create" if kind == "grn" else "sal.doc.create")
    party_type = _party_type(kind)
    party = resolve_party(db, p, party_type, str(a.get("party", "")))
    loc = resolve_location(db, p, a.get("location"))
    if loc is None:
        stores = dump(locations.list_locations(p=p, db=db).items)
        if len(stores) != 1:
            raise ToolError(
                "Which store? Ask the user", stores=[f"{x['code']} {x['name']}" for x in stores]
            )
        loc = stores[0]["id"]
    lines_in = a.get("lines") or []
    if not isinstance(lines_in, list) or not lines_in:
        raise ToolError("A draft needs at least one line")
    lines, shown = [], []
    estimate: Decimal | None = Decimal(0)
    need: dict[str, Decimal] = {}  # base units per item
    names: dict[str, tuple[str, str]] = {}
    for ln in lines_in[:100]:
        item = resolve_item(db, p, str(ln.get("item", "")))
        uom = str(ln.get("uom") or item["base_uom"]).upper()
        units = {u["uom"] for u in item.get("uoms", [])}
        if units and uom not in units:
            raise ToolError(f"{item['code']} is sold in {sorted(units)}, not {uom}")
        try:
            qty = Decimal(str(ln.get("qty")))
        except Exception as exc:
            raise ToolError(f"qty for {item['code']} is not a number") from exc
        if qty <= 0:
            raise ToolError(f"qty for {item['code']} must be above zero")
        line: dict[str, Any] = {"item_id": item["id"], "uom": uom, "qty": str(qty)}
        price = ln.get("unit_price")
        if price not in (None, ""):
            line["unit_price"] = str(Decimal(str(price)))
        elif kind == "grn":
            raise ToolError(f"A goods received note needs the price for {item['code']}")
        lines.append(line)
        unit = line.get("unit_price") or next(
            (u.get("selling_price") for u in item.get("uoms", []) if u["uom"] == uom), None
        )
        if unit is None:
            estimate = None  # no list price: the document will say MISSING
        elif estimate is not None:
            estimate += qty * Decimal(str(unit))
        factor = next(
            (Decimal(str(u["factor"])) for u in item.get("uoms", []) if u["uom"] == uom),
            Decimal(1),
        )
        need[item["id"]] = need.get(item["id"], Decimal(0)) + qty * factor
        names[item["id"]] = (f"{item['code']} {item['name']}", item["base_uom"])
        shown.append(
            {
                "item": f"{item['code']} {item['name']}",
                "uom": uom,
                "qty": str(qty),
                "unit_price": line.get("unit_price") or (f"list {unit}" if unit else "none"),
            }
        )
    doc_date = _date(a.get("doc_date"), "doc_date") or date.today()
    payload = {
        "doc_type": kind,
        "body": {
            "doc_date": doc_date.isoformat(),
            f"{party_type}_id": party["id"],
            "location_id": loc,
            "remark": (str(a["remark"])[:500] if a.get("remark") else None),
            "lines": lines,
        },
    }
    summary = {
        "action": f"Create a DRAFT {kind}",
        party_type: f"{party['code']} {party['name']}",
        "store": next(
            x["code"] for x in dump(locations.list_locations(p=p, db=db).items) if x["id"] == loc
        ),
        "date": doc_date.isoformat(),
        "lines": shown,
        "estimated_subtotal": (
            str(estimate.quantize(Decimal("0.01"))) if estimate is not None else None
        ),
        "checks": _draft_checks(db, p, kind, party, loc, need, names, estimate),
        "note": "Before tax and discounts. Only a draft is created on Confirm; saving and "
        "posting stay with the user",
    }
    return {"summary": summary, "payload": payload}


def _draft_checks(db, p, kind, party, loc, need, names, estimate) -> list[str]:
    """Worked out by the system, so the model never has to do the arithmetic."""
    out: list[str] = []
    if kind in ("delivery-order", "sales-invoice") and p.has("inv.balance.read"):
        for item_id, qty in need.items():
            rows = stock.balance(
                p=p,
                db=db,
                item_id=uuid.UUID(item_id),
                location_id=uuid.UUID(loc),
                page=1,
                page_size=1,
            ).items
            have = rows[0].qty if rows else Decimal(0)
            if have < qty:
                name, base = names[item_id]
                out.append(
                    f"Stock short: {name} has {have.normalize():f} {base} here, this needs "
                    f"{qty.normalize():f} {base} (short {(qty - have).normalize():f})"
                )
    limit = party.get("credit_limit")
    if (
        kind in ("delivery-order", "sales-invoice")
        and limit not in (None, "0", "0.0000")
        and estimate is not None
        and p.has("fin.doc.read")
    ):
        owed = finance.open_items(
            party_type="customer", party_id=uuid.UUID(party["id"]), p=p, db=db
        ).balance
        after = owed + estimate
        if after > Decimal(str(limit)):
            out.append(
                f"Credit limit: owes RM {owed:.2f} now, RM {after:.2f} with this "
                f"(limit RM {Decimal(str(limit)):.2f})"
            )
    return out


def execute(db: Session, p: Principal, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a confirmed proposal as the user (permissions are checked again here)."""
    if tool != "draft_document":
        raise NotFound(f"Unknown action {tool}")
    kind = payload["doc_type"]
    if kind == "grn":
        doc = purchasing.create_doc(kind=kind, body=purchasing.DocIn(**payload["body"]), p=p, db=db)
    else:
        doc = sales.create_doc(kind=kind, body=sales.DocIn(**payload["body"]), p=p, db=db)
    out = dump(doc)
    return {
        "doc_type": kind,
        "doc_id": out["id"],
        "status": out["status"],
        "total": out.get("total"),
    }


# --- registry -----------------------------------------------------------------------------

S = "string"
DOC_TYPE = {"type": S, "enum": list(DOC_TYPES)}
DATE = {"type": S, "description": "YYYY-MM-DD"}

TOOLS: list[Tool] = [
    Tool(
        "search_items",
        "Find items by name, code or barcode (8+ digits, 12 or 13 digit EAN both work). "
        "Returns units with selling prices. Use before answering anything about an item.",
        {
            "type": "object",
            "properties": {"query": {"type": S}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
        t_search_items,
        all_of=("md.item.read",),
    ),
    Tool(
        "get_stock_balance",
        "Stock on hand of ONE item in each store the user can see (base units). "
        "`item` is a code, barcode or name; `location` a store code, optional.",
        {
            "type": "object",
            "properties": {"item": {"type": S}, "location": {"type": S}},
            "required": ["item"],
        },
        t_stock,
        all_of=("inv.balance.read", "md.item.read"),
    ),
    Tool(
        "find_parties",
        "Search customers or suppliers by code or name.",
        {
            "type": "object",
            "properties": {
                "party_type": {"type": S, "enum": ["customer", "supplier"]},
                "query": {"type": S},
            },
            "required": ["party_type"],
        },
        t_find_parties,
        any_of=("md.customer.read", "md.supplier.read"),
    ),
    Tool(
        "list_documents",
        "List documents of one type, newest first. Filters: party (customer/supplier code "
        "or name), status, date range. For one document's lines use get_document.",
        {
            "type": "object",
            "properties": {
                "doc_type": DOC_TYPE,
                "party": {"type": S},
                "status": {"type": S, "enum": ["DRAFT", "SAVED", "POSTED", "VOIDED"]},
                "date_from": DATE,
                "date_to": DATE,
            },
            "required": ["doc_type"],
        },
        t_list_documents,
        any_of=("sal.doc.read", "pur.doc.read", "fin.doc.read"),
    ),
    Tool(
        "get_document",
        "One document with its lines, by its number (e.g. INV000012, GRN000003).",
        {
            "type": "object",
            "properties": {"doc_type": DOC_TYPE, "doc_no": {"type": S}},
            "required": ["doc_type", "doc_no"],
        },
        t_get_document,
        any_of=("sal.doc.read", "pur.doc.read", "fin.doc.read"),
    ),
    Tool(
        "get_open_items",
        "What a customer owes us, or what we owe a supplier: unpaid documents and balance.",
        {
            "type": "object",
            "properties": {
                "party_type": {"type": S, "enum": ["customer", "supplier"]},
                "party": {"type": S},
            },
            "required": ["party_type", "party"],
        },
        t_open_items,
        all_of=("fin.doc.read",),
    ),
    Tool(
        "run_report",
        "Predefined reports. sales_summary: sales (before tax) less returns for a date "
        "range, grouped by day / item / customer / location (needs date_from, date_to). "
        "low_stock: items at or below `threshold` base units per store, negatives first.",
        {
            "type": "object",
            "properties": {
                "report": {"type": S, "enum": ["sales_summary", "low_stock"]},
                "date_from": DATE,
                "date_to": DATE,
                "group_by": {"type": S, "enum": ["day", "item", "customer", "location"]},
                "location": {"type": S},
                "threshold": {"type": "number"},
                "limit": {"type": "integer"},
            },
            "required": ["report"],
        },
        t_report,
        all_of=("report.run",),
    ),
    Tool(
        "list_issues",
        "Problem documents to follow up (missing CN, amount mismatch, unknown invoice...).",
        {
            "type": "object",
            "properties": {"status": {"type": S, "enum": ["OPEN", "RESOLVED", "CANCELLED"]}},
        },
        t_issues,
        all_of=("core.issue.read",),
    ),
    Tool(
        "list_stores",
        "The stores / warehouses this user can see.",
        {"type": "object", "properties": {}},
        t_locations,
        all_of=("core.location.read",),
    ),
    Tool(
        "search_knowledge",
        "Search the company knowledge base: rules and SOPs (returns, cashier shift close), "
        "supplier terms, SST / e-invoice notes, how to use this system. Use it for any "
        "'how do we / what is the rule / what does the contract say' question. Not for stock, "
        "prices or documents: use the other tools for those. Cite results as [1], [2].",
        {"type": "object", "properties": {"query": {"type": S}}, "required": ["query"]},
        t_knowledge,
        all_of=("ai.kb.read",),
    ),
    Tool(
        "draft_document",
        "Propose a DRAFT quotation, delivery order, sales invoice or goods received note "
        "(grn). Nothing is created until the user presses confirm on the card; never say it "
        "is done. Leave unit_price out on sales to use the list price; grn needs prices.",
        {
            "type": "object",
            "properties": {
                "doc_type": {"type": S, "enum": list(DRAFT_TYPES)},
                "party": {"type": S, "description": "customer (sales) or supplier (grn)"},
                "location": {"type": S, "description": "store code"},
                "doc_date": DATE,
                "remark": {"type": S},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item": {"type": S},
                            "qty": {"type": "number"},
                            "uom": {"type": S},
                            "unit_price": {"type": "number"},
                        },
                        "required": ["item", "qty"],
                    },
                },
            },
            "required": ["doc_type", "party", "lines"],
        },
        t_draft,
        all_of=("ai.write.enabled",),
        any_of=("sal.doc.create", "pur.doc.create"),
        write=True,
    ),
]
BY_NAME = {t.name: t for t in TOOLS}


def available(p: Principal, *, include_writes: bool = True) -> list[Tool]:
    return [t for t in TOOLS if t.allowed(p) and (include_writes or not t.write)]


def run(db: Session, p: Principal, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool. Always returns a dict the model can read, never raises for its input.
    Callers run it inside a savepoint they roll back (see butler.run_tool)."""
    tool = BY_NAME.get(name)
    if tool is None or not tool.allowed(p):
        return {"error": "NOT_AVAILABLE", "message": f"No tool {name} for this user"}
    try:
        return {"ok": True, "result": tool.run(db, p, args)}
    except ToolError as exc:
        return {"error": "CHECK_INPUT", "message": str(exc), **exc.details}
    except AppError as exc:
        return {"error": exc.code, "message": exc.message, **exc.details}
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        return {"error": "CHECK_INPUT", "message": f"Bad arguments: {exc}"}
