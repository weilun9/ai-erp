"""Fields every e-invoice party (our company, suppliers, customers) carries.

Named after what LHDN MyInvois asks for; Singapore will reuse the same columns
(UEN goes into id_no with id_type BRN, GST reg no. into sst_no's sibling later).
"""

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column


class EinvoiceParty:
    tin: Mapped[str | None] = mapped_column(String(20))
    id_type: Mapped[str | None] = mapped_column(String(10))  # BRN / NRIC / PASSPORT / ARMY
    id_no: Mapped[str | None] = mapped_column(String(30))
    sst_no: Mapped[str | None] = mapped_column(String(50))
    msic_code: Mapped[str | None] = mapped_column(String(5))
    address1: Mapped[str | None] = mapped_column(String(150))
    address2: Mapped[str | None] = mapped_column(String(150))
    address3: Mapped[str | None] = mapped_column(String(150))
    city: Mapped[str | None] = mapped_column(String(50))
    postcode: Mapped[str | None] = mapped_column(String(10))
    state_code: Mapped[str | None] = mapped_column(String(2))
    country_code: Mapped[str] = mapped_column(String(3), server_default=text("'MYS'"))
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(200))
