"""How to use this system: the AI butler's built-in user guide.

One entry per task: where it is (a page the butler links to, so the user lands on it in one
click), the steps, and the permission it needs. The butler answers "how do I ..." from here
instead of guessing, and offers to draft the document itself where it can.
"""

from dataclasses import dataclass

from app.services.auth import Principal


@dataclass(frozen=True)
class Guide:
    key: str
    title: str
    page: str  # in-app path the answer links to
    steps: tuple[str, ...]
    words: tuple[str, ...] = ()  # other ways people ask for it (any language)
    perm: str | None = None
    ai_can_draft: bool = False


def _doc(key, title, party, extra=(), words=(), perm="sal.doc.create", posts=True, draft=False):
    steps = (
        f"Open the page (link below) - it starts a new {title.lower()}.",
        f"Pick the {party}: type part of the code or name and choose from the list.",
        "Check the date and the store.",
        "Add items: scan a barcode or type the item name and press Enter; set unit and "
        "quantity. Leave the price empty to use the list price.",
        *extra,
        "Click 'Save & number' (it gets its document number).",
    ) + (("Click 'Post' to make it final (stock and accounts update).",) if posts else ())
    return Guide(key, title, f"/docs/{key}/new", steps, words, perm, draft)


GUIDES: tuple[Guide, ...] = (
    _doc(
        "sales-invoice",
        "Sales invoice",
        "customer",
        words=("invoice", "bill", "发票", "开单", "销售单", "invois"),
        draft=True,
    ),
    _doc(
        "quotation",
        "Quotation",
        "customer",
        posts=False,
        extra=("Later, 'Convert' turns it into a sales order, delivery order or invoice.",),
        words=("quote", "报价", "sebut harga"),
        draft=True,
    ),
    _doc(
        "sales-order",
        "Sales order",
        "customer",
        extra=("Posting confirms the order; deliver or invoice it later in parts.",),
        words=("order", "销售订单", "订单"),
    ),
    _doc(
        "delivery-order",
        "Delivery order",
        "customer",
        extra=("Invoice it later from the delivery order (pick the lines and quantities).",),
        words=("DO", "送货单", "出货"),
        draft=True,
    ),
    _doc(
        "customer-cn",
        "Customer credit note",
        "customer",
        extra=("Choose the invoice it refunds; returned stock comes back at its cost.",),
        words=("credit note", "return", "退货", "退款", "CN"),
    ),
    _doc(
        "grn",
        "Goods received",
        "supplier",
        perm="pur.doc.create",
        extra=(
            "Type the supplier's invoice number and the price of each line.",
            "Tracked items need their batch / serial numbers on the line.",
        ),
        words=("GRN", "receive", "purchase invoice", "进货", "收货", "采购单据"),
        draft=True,
    ),
    _doc(
        "purchase-order",
        "Purchase order",
        "supplier",
        perm="pur.doc.create",
        extra=("When goods arrive, 'Receive' turns it into a goods received note.",),
        words=("PO", "采购订单", "下单给供应商"),
    ),
    _doc(
        "supplier-cn",
        "Supplier credit note",
        "supplier",
        perm="pur.doc.create",
        words=("return to supplier", "退货给供应商"),
    ),
    Guide(
        "receipt",
        "Customer receipt (money in)",
        "/docs/receipt/new",
        (
            "Open the page (link below).",
            "Pick the customer, the date, how they paid and the amount.",
            "The open invoices are knocked off oldest first; change the amounts if needed.",
            "Click 'Save & number', then 'Post'.",
        ),
        ("receipt", "payment received", "收款", "收钱"),
        "fin.doc.create",
    ),
    Guide(
        "supplier-payment",
        "Supplier payment (money out)",
        "/docs/supplier-payment/new",
        (
            "Open the page (link below).",
            "Pick the supplier and type the invoice numbers from their payment advice.",
            "Check the amount, then 'Save & number' and 'Post'.",
        ),
        ("pay supplier", "付款", "付供应商"),
        "fin.doc.create",
    ),
    Guide(
        "till",
        "Till (POS sale)",
        "/till",
        (
            "Open the till (link below); open a shift with the cash float if asked.",
            "Scan the items (or type a name and press Enter).",
            "For a member, press 'Member' and scan the card or type the phone number.",
            "Choose cash / card / e-wallet / points and press 'Charge'.",
        ),
        ("pos", "cashier", "收银", "零售"),
        "pos.sale.create",
    ),
    Guide(
        "scan",
        "Scan a paper invoice",
        "/scan",
        (
            "Open Scan documents (link below) and drop the photo or PDF in.",
            "Check what was read in the sheet; fix the yellow / red cells.",
            "Confirm: it becomes a draft goods received note, credit note or payment.",
        ),
        ("ocr", "photo", "扫描", "拍照"),
        "pur.doc.create",
    ),
    Guide(
        "item",
        "New item",
        "/items/edit/new",
        (
            "Open the page (link below).",
            "Type the code, name and base unit; add other units (carton = 24) and prices.",
            "Add barcodes, then 'Create'.",
        ),
        ("product", "商品", "新商品", "产品"),
        "md.item.manage",
    ),
    Guide(
        "customer",
        "New customer",
        "/parties/customer/new",
        ("Open the page (link below), fill in code, name, terms and credit limit, then save.",),
        ("客户", "pelanggan"),
        "md.customer.manage",
    ),
    Guide(
        "supplier",
        "New supplier",
        "/parties/supplier/new",
        ("Open the page (link below), fill in code, name and terms, then save.",),
        ("供应商", "pembekal"),
        "md.supplier.manage",
    ),
    Guide(
        "promotion",
        "Promotion",
        "/promotions",
        (
            "Open Promotions (link below) and click 'New promotion'.",
            "Choose the type (% off, buy X get Y, N for RM...), the dates and the items.",
            "Save: the till applies it by itself on those days.",
        ),
        ("promo", "discount", "促销", "优惠"),
        "pos.promo.manage",
    ),
    Guide(
        "member",
        "Loyalty member",
        "/promotions?tab=members",
        ("Open Loyalty members (link below), click 'New member', type the card number and name.",),
        ("membership", "会员", "积分"),
        "pos.member.manage",
    ),
    Guide(
        "labels",
        "Print barcode labels",
        "/labels",
        ("Open Barcode labels (link below), add the items and copies, pick the paper, Print.",),
        ("barcode", "条码", "标签"),
        "md.item.read",
    ),
    Guide(
        "assembly",
        "Assemble a product (hamper, set)",
        "/assembly",
        (
            "First give the product a bill of materials on its item page.",
            "Open Assembly (link below), pick the product, quantity and store, click Assemble.",
        ),
        ("bom", "组装", "套装"),
        "inv.doc.create",
    ),
    Guide(
        "batches",
        "Batches, expiry and serial numbers",
        "/lots",
        (
            "Tick 'Track batch' or 'Track serial' on the item.",
            "Enter the numbers on goods received; sales take the earliest expiry first.",
            "Open Batches & expiry (link below) to see what expires soon and trace a batch.",
        ),
        ("batch", "expiry", "serial", "批号", "有效期", "序列号"),
        "inv.balance.read",
    ),
    Guide(
        "bank",
        "Bank reconciliation",
        "/accounting?tab=bank",
        (
            "Open Bank reconciliation (link below).",
            "Paste the bank statement rows, then 'Auto-match'.",
            "Tick the rest by hand or 'Book it' for bank charges; enter the closing balance.",
        ),
        ("对账", "银行"),
        "acc.read",
    ),
    Guide(
        "year-end",
        "Year-end close",
        "/accounting?tab=yearend",
        ("Open Year-end close (link below), pick the last day of the year, tick and close.",),
        ("年结", "closing"),
        "acc.period.close",
    ),
    Guide(
        "sst",
        "SST return",
        "/accounting?tab=sst",
        ("Open SST return (link below), pick the two-month period, file the figures on MySST.",),
        ("tax return", "税", "申报"),
        "report.run",
    ),
    Guide(
        "export",
        "Export the accounts",
        "/accounting",
        ("Open Accounting (link below), click 'Export', pick the period, Excel or CSV.",),
        ("excel", "导出", "accountant"),
        "acc.period.close",
    ),
    Guide(
        "users",
        "Add a user",
        "/settings?tab=users",
        (
            "Open Settings > Users (link below), add the user with a password and tags.",
            "Their computer needs approval in Settings > Computers on first login.",
        ),
        ("staff", "员工", "用户", "账号"),
        "core.user.manage",
    ),
)
BY_KEY = {g.key: g for g in GUIDES}


def _norm(s: str) -> str:
    return s.strip().lower()


def find(p: Principal, topic: str) -> dict:
    """The guide for a topic (by key, title or a common word), or the list of topics."""
    mine = [g for g in GUIDES if g.perm is None or p.has(g.perm)]
    q = _norm(topic or "")

    def score(g: Guide) -> int:
        """Longest phrase of the guide found in the question ("purchase invoice" beats
        "invoice"); 0 = not about this guide."""
        if not q:
            return 0
        names = (g.key, g.key.replace("-", " "), _norm(g.title), *(_norm(w) for w in g.words))
        return max((len(n) for n in names if n and (n in q or q in n)), default=0)

    ranked = sorted(((score(g), i, g) for i, g in enumerate(mine)), key=lambda x: (-x[0], x[1]))
    hits = [g for s, _, g in ranked if s > 0]
    if len(hits) >= 1:
        g = hits[0]
        return {
            "topic": g.key,
            "title": g.title,
            "page": g.page,
            "link_markdown": f"[Open {g.title}]({g.page})",
            "steps": list(g.steps),
            "butler_can_draft_it": g.ai_can_draft,
            "also": [x.key for x in hits[1:4]],
        }
    return {
        "error": "NO_SUCH_TOPIC",
        "topics": [{"key": g.key, "title": g.title} for g in mine],
    }
