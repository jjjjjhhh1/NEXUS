"""Validation shared by every balance mutation."""
from decimal import Decimal, InvalidOperation
from .exceptions import BusinessRuleException


def positive_amount(value, places=2) -> Decimal:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("9999999999999.99"):
            raise InvalidOperation
        if amount != amount.quantize(Decimal(1).scaleb(-places)):
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise BusinessRuleException(f"金额或份额必须为正数，最多 {places} 位小数") from None
