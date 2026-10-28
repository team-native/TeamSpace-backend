import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_AMOUNT_PATTERN = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?")


def parse_money(value: Any) -> int:
    """Parse a currency value and round fractional units to the nearest won."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("금액 형식이 올바르지 않습니다.")

    text = str(value).strip().replace("₩", "").replace("$", "").replace(" ", "")
    multiplier = 1
    for suffix, factor in (("만원", 10_000), ("천원", 1_000), ("원", 1)):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            multiplier = factor
            break
    if not _AMOUNT_PATTERN.fullmatch(text):
        raise ValueError("금액 형식이 올바르지 않습니다.")

    try:
        amount = Decimal(text.replace(",", "")) * multiplier
    except InvalidOperation:
        raise ValueError("금액 형식이 올바르지 않습니다.") from None
    if not amount.is_finite():
        raise ValueError("금액은 유한한 숫자여야 합니다.")
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
