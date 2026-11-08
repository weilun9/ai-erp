"""Barcode matching that tolerates 11 / 12 / 13 digit variants of the same GTIN.

Real-world data mixes:
- EAN-13 with check digit          9556345310072  (13)
- EAN-13 without check digit       955634531007   (12, the DynaMod habit)
- UPC-A with check digit           012345678905   (12)
- UPC-A without check digit        01234567890    (11)
- UPC-A written as EAN-13          0012345678905  (13, leading 0)

Every numeric 11-13 digit code is reduced to one or more *keys*: the 12-digit GTIN-13 body
(the 13-digit form without its check digit). Two codes match when their key sets overlap.
A 12-digit code is ambiguous (UPC-A with check, or EAN-13 without), so it gets both keys
when its last digit is a valid UPC-A check digit.
Anything else (EAN-8, internal codes, letters) matches only exactly.
"""


def gtin_check_digit(body: str) -> int:
    """Check digit for a GTIN body (any length): weights 3,1,3,1... from the right."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def _has_valid_check(code: str) -> bool:
    return int(code[-1]) == gtin_check_digit(code[:-1])


def clean(code: str) -> str:
    return "".join(code.split())


def keys_for(code: str) -> list[str]:
    """All match keys for a barcode (stored on save, and used for lookup)."""
    code = clean(code)
    if not code.isdigit() or not 11 <= len(code) <= 13:
        return [f"X:{code.upper()}"]
    keys: set[str] = set()
    if len(code) == 13:
        keys.add(code[:12])  # EAN-13 (or UPC-A with leading 0) minus check digit
    elif len(code) == 12:
        keys.add(code)  # EAN-13 body without check digit
        if _has_valid_check(code):
            keys.add("0" + code[:11])  # UPC-A with check digit
    else:  # 11
        keys.add("0" + code)  # UPC-A body without check digit
    return sorted(keys)


def full_ean13(code: str) -> str | None:
    """Best 13-digit form, for printing / e-invoice. None if not a GTIN-like code."""
    code = clean(code)
    if not code.isdigit():
        return None
    if len(code) == 13:
        return code
    if len(code) == 12:
        if _has_valid_check(code):
            return "0" + code
        return code + str(gtin_check_digit(code))
    if len(code) == 11:
        body = "0" + code
        return body + str(gtin_check_digit(body))
    return None
