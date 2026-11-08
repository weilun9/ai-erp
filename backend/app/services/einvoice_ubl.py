"""Build the MyInvois UBL 2.1 JSON document for a sales invoice (01) or credit note (02).

Document type version 1.0 (no digital signature): LHDN still accepts it until it announces
its retirement (SDK FAQ, checked 2026-11-08). Version 1.1 needs a signing certificate from
an approved CA; `VERSION` and the signer are the only things that would change.

UBL JSON shape: every value is a list of objects, the value under "_" and attributes
beside it, e.g. {"ID": [{"_": "INV000001"}]}.

`build()` returns (document, problems, warnings): problems block submission (LHDN would
reject the document), warnings are for a person to look at.
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.localization import pack
from app.models import Company, Customer, Item, TaxCode, Uom

VERSION = "1.0"
TYPE_CODES = {"INV": "01", "CN": "02"}
NS = {
    "_D": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "_A": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "_B": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
TAX_SCHEME = [{"ID": [{"_": "OTH", "schemeID": "UN/ECE 5153", "schemeAgencyID": "6"}]}]
GENERAL_PUBLIC_TIN = "EI00000000010"
NA = "NA"


def _v(value: Any, **attrs: Any) -> list[dict[str, Any]]:
    return [{"_": value, **attrs}]


def _n(x: Decimal, places: str = "0.01") -> float:
    return float(Decimal(x).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _money(x: Decimal, currency: str) -> list[dict[str, Any]]:
    return _v(_n(x), currencyID=currency)


def _party_problems(prefix: str, party, *, seller: bool, my) -> list[dict[str, str]]:
    out = []

    def need(field: str, ok: bool, message: str) -> None:
        if not ok:
            out.append({"field": f"{prefix}.{field}", "message": message})

    need("tin", bool(party.tin) and my.validate_tin(party.tin), "TIN missing or not valid")
    need(
        "id_no",
        bool(party.id_type and party.id_no) and my.validate_id(party.id_type, party.id_no),
        "Registration / ID number missing (BRN, NRIC, passport...)",
    )
    need("address1", bool(party.address1), "Address missing")
    need("city", bool(party.city), "City missing")
    need("state_code", party.state_code in my.STATE_CODES, "State code missing")
    need("phone", bool(party.phone), "Phone number missing")
    if seller:
        need(
            "msic_code",
            bool(party.msic_code) and my.validate_msic(party.msic_code),
            "MSIC code missing",
        )
        need("business_activity", bool(party.business_activity), "Business activity missing")
    return out


def _party(party, name: str, *, seller: bool, general: bool = False) -> dict[str, Any]:
    if general:  # buyer gave no TIN: LHDN's general-public TIN, details "NA"
        ids = [
            {"ID": _v(GENERAL_PUBLIC_TIN, schemeID="TIN")},
            {"ID": _v(NA, schemeID="BRN")},
            {"ID": _v(NA, schemeID="SST")},
        ]
        address = {
            "CityName": _v(NA),
            "PostalZone": _v(""),
            "CountrySubentityCode": _v("17"),
            "AddressLine": [{"Line": _v(NA)}],
        }
        contact = {"Telephone": _v(NA)}
    else:
        ids = [
            {"ID": _v(party.tin, schemeID="TIN")},
            {"ID": _v(party.id_no, schemeID=party.id_type)},
            {"ID": _v(party.sst_no or NA, schemeID="SST")},
        ]
        if seller:
            ids.append({"ID": _v(NA, schemeID="TTX")})  # tourism tax registration
        address = {
            "CityName": _v(party.city),
            "PostalZone": _v(party.postcode or ""),
            "CountrySubentityCode": _v(party.state_code),
            "AddressLine": [
                {"Line": _v(line)}
                for line in (party.address1, party.address2, party.address3)
                if line
            ],
        }
        contact = {"Telephone": _v(party.phone)}
        if party.email:
            contact["ElectronicMail"] = _v(party.email)
    address["Country"] = [
        {
            "IdentificationCode": _v(
                party.country_code or "MYS", listID="ISO3166-1", listAgencyID="6"
            )
        }
    ]
    out: dict[str, Any] = {}
    if seller:
        out["IndustryClassificationCode"] = _v(party.msic_code, name=party.business_activity)
    out |= {
        "PartyIdentification": ids,
        "PostalAddress": [address],
        "PartyLegalEntity": [{"RegistrationName": _v(name if not general else "General Public")}],
        "Contact": [contact],
    }
    return out


def build(
    db: Session,
    company_id,
    doc_type: str,
    doc,
    *,
    original: tuple[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    """`original` = (invoice number, LHDN UUID) that a credit note corrects."""
    company = db.get(Company, company_id)
    my = pack(company.country)
    customer = db.get(Customer, doc.customer_id)
    problems: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    currency = doc.currency or my.CURRENCY

    if not company.einvoice_enabled:
        problems.append({"field": "company.einvoice_enabled", "message": "E-invoice is off"})
    problems += _party_problems("company", company, seller=True, my=my)
    general = not customer.tin
    if general:
        warnings.append(
            {
                "field": "customer.tin",
                "message": "Customer has no TIN: issued to the General Public TIN "
                "(only right for individuals who did not ask for their own e-invoice)",
            }
        )
    else:
        problems += _party_problems("customer", customer, seller=False, my=my)
    if doc_type == "CN" and original is None:
        problems.append(
            {
                "field": "ref_invoice_id",
                "message": "A credit note e-invoice must point at the original invoice's "
                "validated e-invoice",
            }
        )

    items = {
        i.id: i
        for i in db.scalars(select(Item).where(Item.id.in_({ln.item_id for ln in doc.lines})))
    }
    taxes = {t.id: t for t in db.scalars(select(TaxCode).where(TaxCode.company_id == company_id))}
    uoms = {u.code: u for u in db.scalars(select(Uom).where(Uom.company_id == company_id))}
    na_tax = next((t for t in taxes.values() if t.einvoice_tax_type == "06"), None)

    lines = []
    tax_groups: dict[tuple[str, Decimal], list[Decimal]] = {}
    for ln in doc.lines:
        item = items[ln.item_id]
        tax = taxes.get(ln.tax_code_id) if ln.tax_code_id else na_tax
        tax_type = tax.einvoice_tax_type if tax else "06"
        uom = uoms.get(ln.uom)
        if uom is None:
            warnings.append(
                {"field": f"lines.{ln.line_no}.uom", "message": f"Unit {ln.uom} sent as C62"}
            )
        if not my.validate_classification(item.einvoice_classification or ""):
            problems.append(
                {"field": f"lines.{ln.line_no}.classification", "message": "Bad classification"}
            )
        gross = Decimal(ln.qty) * Decimal(ln.unit_price)
        discount = gross - ln.line_subtotal
        category: dict[str, Any] = {"ID": _v(tax_type), "TaxScheme": TAX_SCHEME}
        if tax_type == "E":
            category["TaxExemptionReason"] = _v(tax.name if tax else "Exempt")
        line: dict[str, Any] = {
            "ID": _v(str(ln.line_no)),
            "InvoicedQuantity": _v(
                _n(ln.qty, "0.0001"), unitCode=uom.einvoice_code if uom else "C62"
            ),
            "LineExtensionAmount": _money(ln.line_subtotal, currency),
        }
        if discount > Decimal("0.004"):
            line["AllowanceCharge"] = [
                {
                    "ChargeIndicator": _v(False),
                    "AllowanceChargeReason": _v("Discount"),
                    "MultiplierFactorNumeric": _v(_n(ln.discount_pct / 100, "0.0001")),
                    "Amount": _money(discount, currency),
                }
            ]
        line |= {
            "TaxTotal": [
                {
                    "TaxAmount": _money(ln.line_tax, currency),
                    "TaxSubtotal": [
                        {
                            "TaxableAmount": _money(ln.line_subtotal, currency),
                            "TaxAmount": _money(ln.line_tax, currency),
                            "Percent": _v(_n(ln.tax_rate)),
                            "TaxCategory": [category],
                        }
                    ],
                }
            ],
            "Item": [
                {
                    "CommodityClassification": [
                        {
                            "ItemClassificationCode": _v(
                                item.einvoice_classification or my.DEFAULT_CLASSIFICATION,
                                listID="CLASS",
                            )
                        }
                    ],
                    "Description": _v(item.name),
                }
            ],
            "Price": [{"PriceAmount": _money(ln.unit_price, currency)}],
            "ItemPriceExtension": [{"Amount": _money(gross, currency)}],
        }
        # Line discounts are inside LineExtensionAmount; there is no document-level
        # allowance, so LegalMonetaryTotal carries no AllowanceTotalAmount.
        lines.append(line)
        g = tax_groups.setdefault((tax_type, ln.tax_rate), [Decimal(0), Decimal(0)])
        g[0] += ln.line_subtotal
        g[1] += ln.line_tax

    now = (now or datetime.now(UTC)).astimezone(UTC)
    invoice: dict[str, Any] = {
        "ID": _v(doc.doc_no),
        "IssueDate": _v(now.strftime("%Y-%m-%d")),  # must be "now" in UTC (LHDN rule)
        "IssueTime": _v(now.strftime("%H:%M:%SZ")),
        "InvoiceTypeCode": _v(TYPE_CODES[doc_type], listVersionID=VERSION),
        "DocumentCurrencyCode": _v(currency),
    }
    if original is not None:
        invoice["BillingReference"] = [
            {"InvoiceDocumentReference": [{"ID": _v(original[0]), "UUID": _v(original[1])}]}
        ]
    invoice |= {
        "AccountingSupplierParty": [{"Party": [_party(company, company.name, seller=True)]}],
        "AccountingCustomerParty": [
            {"Party": [_party(customer, customer.name, seller=False, general=general)]}
        ],
        "TaxTotal": [
            {
                "TaxAmount": _money(doc.tax_total, currency),
                "TaxSubtotal": [
                    {
                        "TaxableAmount": _money(base, currency),
                        "TaxAmount": _money(tax, currency),
                        "TaxCategory": [{"ID": _v(t), "TaxScheme": TAX_SCHEME}],
                    }
                    for (t, _rate), (base, tax) in sorted(tax_groups.items())
                ],
            }
        ],
        "LegalMonetaryTotal": [
            {
                "LineExtensionAmount": _money(doc.subtotal, currency),
                "TaxExclusiveAmount": _money(doc.subtotal, currency),
                "TaxInclusiveAmount": _money(doc.total, currency),
                "PayableAmount": _money(doc.total, currency),
            }
        ],
        "InvoiceLine": lines,
    }
    return {**NS, "Invoice": [invoice]}, problems, warnings
