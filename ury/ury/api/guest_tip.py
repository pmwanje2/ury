"""Guest gratuities apply only to the newly priced cart lines."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def guest_tip_amount(value, subtotal, precision):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        rounded = amount.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
        if rounded > min(Decimal(str(subtotal)), Decimal("999999999999")):
            raise ValueError
        return rounded
    except (ValueError, InvalidOperation) as exc:
        raise ValueError("Invalid tip amount") from exc


def tip_presets(value):
    percentages = [int(part.strip()) for part in (value or "5,10,15").split(",")]
    if len(percentages) > 8 or any(not 1 <= n <= 100 for n in percentages):
        raise ValueError("Invalid tip percentages")
    return list(dict.fromkeys(percentages))


def self_order_tip_server(assigned, waiter):
    # Guest is an existing User link. Closing entries group this bucket by outlet.
    for user in (assigned, waiter):
        if user and user not in ("Administrator", "Guest"):
            return user
    return "Guest"
