"""Input parsers. Convert free-form Chinese / numeric amounts into Decimal.
No business logic, no DB access. Pure string -> Decimal.

This layer does NOT own:
  - Whether the amount is reasonable for the account         -> boundary / demo_agent
  - Whether the amount matches the proposal's amount_evidence -> grounder.ground
"""
from __future__ import annotations

import re
from decimal import Decimal

from ...core.exceptions import BusinessRuleException
from ...core.money import positive_amount
from ..security.guard import normalized


_UNIT_MULTIPLIER = {"万": 10000, "千": 1000, "百": 100}
_DIGIT_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_DIGITS = "零一二两三四五六七八九"
_SMALL_UNITS = "十百千"


def evidence_amount(text: str) -> Decimal:
    """Accept a short, explicit amount expression in either Arabic digits
    or Chinese numerals. Ambiguous colloquial phrasing is *not* guessed —
    a BusinessRuleException is raised so the caller can ask for clarification.

    Accepted shapes:
      - "100" / "100.50" / "200.00"
      - "1万" / "3千" / "5百" (single trailing unit)
      - "一百" / "三百五十" / "二万五" / "一千二百三十四点五"
      - "三百五" / "一万二" — refused as ambiguous (multiple readings possible)
    """
    value = (
        normalized(text)
        .strip()
        .replace("人民币", "")
        .replace("块钱", "")
        .replace("元", "")
        .replace("块", "")
        .replace("¥", "")
        .replace("￥", "")
        .strip()
    )
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return positive_amount(value)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[万千百]", value):
        unit = value[-1]
        return positive_amount(Decimal(value[:-1]) * _UNIT_MULTIPLIER[unit])
    if not re.fullmatch(rf"[{_DIGITS}十百千万]+(?:点[{_DIGITS}]{{1,2}})?", value):
        raise BusinessRuleException("请用明确的人民币数字金额，例如 200.00 元")
    integer, _, fraction = value.partition("点")
    # "三百五" / "一万二" can mean either 305 or 350; refuse rather than guess.
    if len(integer) >= 2 and integer[-1] in _DIGIT_MAP and integer[-2] in "百千万":
        raise BusinessRuleException("金额表达有歧义，请用数字明确金额")
    total = section = number = 0
    last_unit = 10000
    for char in integer:
        if char in _DIGIT_MAP:
            if number:
                raise BusinessRuleException("请用数字明确金额")
            number = _DIGIT_MAP[char]
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
            last_unit = 10000
        else:
            unit = {"十": 10, "百": 100, "千": 1000}[char]
            if unit >= last_unit:
                raise BusinessRuleException("请用数字明确金额")
            section += (number or 1) * unit
            number = 0
            last_unit = unit
    decimal = "".join(str(_DIGIT_MAP[c]) for c in fraction)
    return positive_amount(f"{total + section + number}.{decimal or '0'}")


__all__ = ["evidence_amount"]