"""Country packs. Each country module provides the same names; pick one by company.country.

Malaysia (MY) is implemented first (SST + LHDN MyInvois). Singapore (SG: GST + InvoiceNow)
will be added as app/localization/sg.py with the same interface.
"""

from types import ModuleType

from app.localization import my

PACKS: dict[str, ModuleType] = {"MY": my}
SUPPORTED_COUNTRIES = tuple(PACKS)


def pack(country: str) -> ModuleType:
    try:
        return PACKS[country]
    except KeyError as exc:
        raise ValueError(f"Country {country!r} is not supported yet") from exc
