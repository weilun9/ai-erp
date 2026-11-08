"""Permission codes and default tag templates (design doc section 7).

Codes follow ``module.object.action``. Field-level codes start with ``field.``.
The registry here is the single source of truth: ``app.services.seed`` syncs it
into ``core.permission`` and the system tags of each company.
"""

from dataclasses import dataclass

PERMISSIONS: dict[str, str] = {
    # core
    "core.company.read": "View company profile",
    "core.company.manage": "Edit company profile",
    "core.location.read": "View stores / warehouses",
    "core.location.manage": "Create and edit stores / warehouses",
    "core.user.read": "View users",
    "core.user.manage": "Create, edit and deactivate users",
    "core.tag.read": "View tags",
    "core.tag.manage": "Create and edit tags",
    "core.tag.assign": "Assign tags to employees",
    "core.device.manage": "Approve computers for login and sign them out",
    # master data
    "md.setup.read": "View tax codes, units and item categories",
    "md.setup.manage": "Edit tax codes, units and item categories",
    "md.item.read": "View items",
    "md.item.manage": "Create and edit items",
    "md.supplier.read": "View suppliers",
    "md.supplier.manage": "Create and edit suppliers",
    "md.customer.read": "View customers",
    "md.customer.manage": "Create and edit customers",
    # purchasing
    "pur.doc.read": "View purchase documents",
    "pur.doc.create": "Create and edit purchase documents",
    "pur.doc.post": "Post purchase documents",
    "pur.doc.void": "Void posted purchase documents",
    # sales
    "sal.doc.read": "View sales documents",
    "sal.doc.create": "Create and edit sales documents",
    "sal.doc.post": "Post sales documents",
    "sal.doc.void": "Void posted sales documents",
    # receipts, payments, knock-off
    "fin.doc.read": "View receipts, payments, knock-offs and statements",
    "fin.doc.create": "Create receipts and payments; knock off documents",
    "fin.doc.post": "Post receipts and payments",
    "fin.doc.void": "Void receipts and payments",
    # inventory
    "inv.balance.read": "View stock balances and ledger",
    "inv.doc.create": "Create stock adjustments, transfers and counts",
    "inv.doc.post": "Post stock documents",
    "inv.doc.void": "Void posted stock documents",
    # POS
    "pos.sale.create": "Ring up POS sales",
    "pos.shift.close": "Close POS shifts",
    # issues
    "core.issue.read": "View problem documents",
    "core.issue.handle": "Handle problem documents",
    # reports / accounting
    "report.run": "Run predefined reports",
    "acc.period.close": "Month-end close and accounting export",
    # sensitive fields
    "field.cost.read": "See cost price, average cost and margin",
    "field.finance.read": "See receivable / payable balances",
    "field.pii.read": "See customer phone, address and ID numbers",
    # AI
    "ai.chat.use": "Use the AI butler",
    "ai.write.enabled": "Let the AI draft write actions for this user",
    "ai.vision.use": "Use document image recognition",
    "ai.kb.read": "Search the knowledge base",
    "ai.kb.manage": "Manage knowledge base documents",
    # audit
    "audit.read": "View audit log",
}


@dataclass(frozen=True)
class TagTemplate:
    code: str
    name: str
    color: str
    all_locations: bool
    permissions: frozenset[str]


def _match(*prefixes: str) -> frozenset[str]:
    return frozenset(c for c in PERMISSIONS if c.startswith(prefixes))


def _reads() -> frozenset[str]:
    return frozenset(c for c in PERMISSIONS if c.endswith(".read"))


TAG_TEMPLATES: dict[str, TagTemplate] = {
    t.code: t
    for t in (
        TagTemplate("owner", "老板", "#d4a017", True, frozenset(PERMISSIONS)),
        TagTemplate(
            "clerk",
            "书记",
            "#1677ff",
            False,
            (_match("md.", "pur.", "sal.", "fin.", "inv.", "core.issue.") - {"md.setup.manage"})
            | {
                "core.location.read",
                "report.run",
                "field.cost.read",
                "field.finance.read",
                "field.pii.read",
                "ai.chat.use",
                "ai.write.enabled",
                "ai.vision.use",
                "ai.kb.read",
            },
        ),
        TagTemplate(
            "accountant",
            "会计",
            "#52c41a",
            True,
            (_reads() - {"core.issue.read", "audit.read"})
            | _match("field.")
            | {"report.run", "acc.period.close", "ai.chat.use"},
        ),
        TagTemplate(
            "cashier",
            "收银员",
            "#fa8c16",
            False,
            frozenset(
                {
                    "core.location.read",
                    "md.item.read",
                    "inv.balance.read",
                    "pos.sale.create",
                    "pos.shift.close",
                    "ai.chat.use",
                    "ai.kb.read",
                }
            ),
        ),
        TagTemplate(
            "admin",
            "系统管理员",
            "#722ed1",
            True,
            _match("core.company.", "core.location.", "core.user.")
            | {"core.tag.read"}
            | {"ai.kb.manage", "ai.kb.read", "ai.chat.use", "audit.read", "field.cost.read"},
        ),
    )
}

# Unknown codes in templates are a programming error; fail at import time.
for _t in TAG_TEMPLATES.values():
    _unknown = _t.permissions - PERMISSIONS.keys()
    assert not _unknown, f"tag {_t.code} has unknown permissions {_unknown}"
