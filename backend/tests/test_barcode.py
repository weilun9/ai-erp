import pytest

from app.core.barcode import full_ean13, gtin_check_digit, keys_for


def _match(a: str, b: str) -> bool:
    return bool(set(keys_for(a)) & set(keys_for(b)))


def test_check_digit_matches_design_doc_example() -> None:
    # 9556345310072 -> body 955634531007, check 2
    assert gtin_check_digit("955634531007") == 2


@pytest.mark.parametrize(
    "a,b",
    [
        ("9556345310072", "955634531007"),  # EAN-13 vs EAN-13 without check digit
        ("036000291452", "03600029145"),  # UPC-A vs UPC-A without check digit
        ("036000291452", "0036000291452"),  # UPC-A vs UPC-A as EAN-13
        ("03600029145", "0036000291452"),  # 11 vs 13
        ("955 6345 31007 2", "9556345310072"),  # spaces ignored
    ],
)
def test_variants_of_same_code_match(a: str, b: str) -> None:
    assert _match(a, b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("9556345310072", "9556345310089"),
        ("12345678", "012345678"),  # short / internal codes: exact only
        ("ABC123", "abc1234"),
    ],
)
def test_different_codes_do_not_match(a: str, b: str) -> None:
    assert not _match(a, b)


def test_non_gtin_codes_match_exactly_and_case_insensitive() -> None:
    assert keys_for("abc-1") == ["X:ABC-1"]
    assert _match("abc-1", "ABC-1")


def test_full_ean13() -> None:
    assert full_ean13("955634531007") == "9556345310072"
    assert full_ean13("9556345310072") == "9556345310072"
    assert full_ean13("03600029145") == "0036000291452"
    assert full_ean13("036000291452") == "0036000291452"
    assert full_ean13("ABC") is None
