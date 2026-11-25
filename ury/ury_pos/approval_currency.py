"""Company-currency amounts for POS approval limits."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from ury.brand import coded_message


# Standard ISO 4217 non-2dp currencies for reliable site-less / fallback precision.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "TZS", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def currency_precision(currency: str | None, db) -> int:
    code = (currency or "").strip().upper()
    if not code:
        return 2
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    try:
        fraction_units = db.get_value("Currency", code, "fraction_units")
        if fraction_units is not None:
            fu = int(fraction_units)
            if fu == 1:
                return 0
            if fu == 1000:
                return 3
            if fu == 100:
                return 2
    except Exception:
        pass
    return 2


def invoice_approval_currency(invoice, framework) -> tuple[str, Decimal]:
    """Read the company currency and the saved POS invoice exchange rate."""
    currency = str(invoice.get("currency") or "").strip().upper()
    company = invoice.get("company")
    company_currency = framework.db.get_value("Company", company, "default_currency") if company else None
    company_currency = company_currency.strip().upper() if isinstance(company_currency, str) else ""
    if not currency or currency == company_currency:
        return company_currency, Decimal(1)

    raw_rate = invoice.get("conversion_rate")
    # A legacy invoice without currency metadata can still use its original units.
    if not company_currency and not raw_rate:
        return "", Decimal(1)
    try:
        rate = Decimal(str(raw_rate))
    except InvalidOperation:
        rate = Decimal(0)
    if not rate.is_finite() or rate <= 0:
        framework.throw(
            coded_message(
                "FX-RATE-INVALID",
                framework._("A foreign-currency check requires a valid exchange rate for approval."),
            ),
            framework.ValidationError,
        )
    return company_currency or currency, rate


def company_approval_amount(invoice, amount, framework, *, currency_and_rate=None) -> float:
    """Convert a transaction amount for the company-currency POS privilege cap."""
    currency, rate = currency_and_rate or invoice_approval_currency(invoice, framework)
    value = abs(Decimal(str(amount)))
    if not value.is_finite():
        framework.throw(
            coded_message("FX-AMOUNT-INVALID", framework._("The approval amount must be finite.")),
            framework.ValidationError,
        )
    if not currency:
        return float(value)
    quantum = Decimal(1).scaleb(-currency_precision(currency, db=framework.db))
    return float((value * rate).quantize(quantum, rounding=ROUND_HALF_UP))
