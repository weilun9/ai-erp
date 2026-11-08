"""Import every model module so Base.metadata is complete (Alembic autogenerate)."""

from app.models.ai import Conversation, Message, PendingAction, Usage  # noqa: F401
from app.models.audit import EventLog  # noqa: F401
from app.models.core import (  # noqa: F401
    AppUser,
    Company,
    Device,
    Location,
    Permission,
    Tag,
    UserSession,
    tag_permission,
    user_location_scope,
    user_tag,
)
from app.models.docs import DocSequence, PostingJob  # noqa: F401
from app.models.einvoice import Einvoice  # noqa: F401
from app.models.fin import (  # noqa: F401
    CustomerReceipt,
    CustomerReceiptLine,
    DocIssue,
    Knockoff,
    SupplierFee,
    SupplierPayment,
    SupplierPaymentLine,
)
from app.models.inbox import BankTxn, InboxExtraction, InboxFile  # noqa: F401
from app.models.inv import StockBalance, StockLedger  # noqa: F401
from app.models.invdoc import (  # noqa: F401
    StockAdjustment,
    StockAdjustmentLine,
    Stocktake,
    StocktakeLine,
    StockTransfer,
    StockTransferLine,
    StockWriteOff,
    StockWriteOffLine,
)
from app.models.kb import KbChunk, KbDocument  # noqa: F401
from app.models.md import (  # noqa: F401
    Customer,
    Item,
    ItemBarcode,
    ItemCategory,
    ItemUom,
    Supplier,
    TaxCode,
    Uom,
)
from app.models.pur import Grn, GrnLine, SupplierCn, SupplierCnLine  # noqa: F401
from app.models.sal import (  # noqa: F401
    CustomerCn,
    CustomerCnLine,
    DeliveryOrder,
    DeliveryOrderLine,
    Quotation,
    QuotationLine,
    SalesInvoice,
    SalesInvoiceLine,
)
