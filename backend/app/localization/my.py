"""Malaysia: SST and LHDN MyInvois e-invoice code lists.

Code lists follow the MyInvois SDK (state codes, tax types, ID types, classification).
Verify against the current SDK before go-live; values live here so a change is one file.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

COUNTRY = "MY"
COUNTRY_ISO3 = "MYS"
CURRENCY = "MYR"
UTC_OFFSET_HOURS = 8  # Malaysia has no daylight saving

# MyInvois state codes
STATE_CODES: dict[str, str] = {
    "01": "Johor",
    "02": "Kedah",
    "03": "Kelantan",
    "04": "Melaka",
    "05": "Negeri Sembilan",
    "06": "Pahang",
    "07": "Pulau Pinang",
    "08": "Perak",
    "09": "Perlis",
    "10": "Selangor",
    "11": "Terengganu",
    "12": "Sabah",
    "13": "Sarawak",
    "14": "Wilayah Persekutuan Kuala Lumpur",
    "15": "Wilayah Persekutuan Labuan",
    "16": "Wilayah Persekutuan Putrajaya",
    "17": "Not Applicable",
}

# MyInvois tax types
TAX_TYPES: dict[str, str] = {
    "01": "Sales Tax",
    "02": "Service Tax",
    "03": "Tourism Tax",
    "04": "High-Value Goods Tax",
    "05": "Sales Tax on Low Value Goods",
    "06": "Not Applicable",
    "E": "Tax exemption",
}

# Registration / identification scheme for a party (MyInvois "ID type")
ID_TYPES: dict[str, str] = {
    "BRN": "Business registration number",
    "NRIC": "MyKad / MyTentera",
    "PASSPORT": "Passport",
    "ARMY": "Army number",
}

# General TINs LHDN accepts when the counterparty has none
GENERAL_TINS: dict[str, str] = {
    "EI00000000010": "General public (consolidated / walk-in buyer)",
    "EI00000000020": "Foreign buyer",
    "EI00000000030": "Foreign supplier",
    "EI00000000040": "Government / statutory body",
}

DEFAULT_CLASSIFICATION = "022"  # "Others"


@dataclass(frozen=True)
class TaxCodeSeed:
    code: str
    name: str
    rate: Decimal
    einvoice_tax_type: str


DEFAULT_TAX_CODES: tuple[TaxCodeSeed, ...] = (
    TaxCodeSeed("ST10", "Sales tax 10%", Decimal("10"), "01"),
    TaxCodeSeed("ST5", "Sales tax 5%", Decimal("5"), "01"),
    TaxCodeSeed("SV8", "Service tax 8%", Decimal("8"), "02"),
    TaxCodeSeed("SV6", "Service tax 6%", Decimal("6"), "02"),
    TaxCodeSeed("EX", "Exempt", Decimal("0"), "E"),
    TaxCodeSeed("NA", "Not applicable / not taxable", Decimal("0"), "06"),
)


@dataclass(frozen=True)
class UomSeed:
    code: str
    name: str
    einvoice_code: str  # UN/ECE Recommendation 20


DEFAULT_UOMS: tuple[UomSeed, ...] = (
    UomSeed("PCS", "Piece", "C62"),
    UomSeed("CTN", "Carton", "CT"),
    UomSeed("BOX", "Box", "BX"),
    UomSeed("PKT", "Packet", "PA"),
    UomSeed("BTL", "Bottle", "BO"),
    UomSeed("BAG", "Bag", "BG"),
    UomSeed("BD", "Bundle", "BE"),
    UomSeed("TIN", "Tin / can", "CA"),
    UomSeed("DOZ", "Dozen", "DZN"),
    UomSeed("SET", "Set", "SET"),
    UomSeed("KG", "Kilogram", "KGM"),
    UomSeed("G", "Gram", "GRM"),
    UomSeed("L", "Litre", "LTR"),
    UomSeed("ML", "Millilitre", "MLT"),
)

_TIN = re.compile(r"^(C|CS|D|E|F|FA|PT|TA|TC|TN|TR|TP|J|LE|IG|EI)\d{8,13}$")
_MSIC = re.compile(r"^\d{5}$")
_CLASSIFICATION = re.compile(r"^\d{3}$")
_NRIC = re.compile(r"^\d{12}$")


def validate_tin(tin: str) -> bool:
    return bool(_TIN.match(tin))


def validate_msic(code: str) -> bool:
    return bool(_MSIC.match(code))


def validate_classification(code: str) -> bool:
    return bool(_CLASSIFICATION.match(code))


def validate_id(id_type: str, id_no: str) -> bool:
    if id_type not in ID_TYPES:
        return False
    if id_type == "NRIC":
        return bool(_NRIC.match(id_no))
    return 1 <= len(id_no) <= 30
