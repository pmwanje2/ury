"""Company-currency amounts for POS approval limits."""

from datetime import date
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


def company_currency_for(company, framework) -> str:
    company_currency = framework.db.get_value("Company", company, "default_currency") if company else None
    return company_currency.strip().upper() if isinstance(company_currency, str) else ""


def _positive_rate(value):
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate > 0 else None


def _invalid_rate(framework):
    framework.throw(
        coded_message(
            "FX-RATE-INVALID",
            framework._("A foreign-currency check requires a valid exchange rate for approval."),
        ),
        framework.ValidationError,
    )


def profile_approval_quote(profile, framework, *, posting_date=None, currency=None, rate_type=None) -> tuple[str, Decimal | None]:
    """Look up FX for an action without a saved invoice rate."""
    currency = str(currency or profile.get("currency") or "").strip().upper()
    company_currency = company_currency_for(profile.get("company"), framework)
    if not currency or not company_currency or currency == company_currency:
        return company_currency or currency, Decimal(1)

    posting_date = posting_date or date.today().isoformat()
    try:
        from erpnext.setup.utils import get_exchange_rate

        rate = _positive_rate(get_exchange_rate(currency, company_currency, posting_date, args=rate_type))
    except (ImportError, framework.ValidationError):
        rate = None
    return company_currency, rate


def profile_approval_currency(profile, framework, *, posting_date=None, currency=None, rate_type=None) -> tuple[str, Decimal]:
    company_currency, rate = profile_approval_quote(
        profile, framework, posting_date=posting_date, currency=currency, rate_type=rate_type
    )
    if rate is None:
        _invalid_rate(framework)
    return company_currency, rate


def invoice_approval_currency(invoice, framework) -> tuple[str, Decimal]:
    """Read the company currency and the saved POS invoice exchange rate."""
    currency = str(invoice.get("currency") or "").strip().upper()
    company_currency = company_currency_for(invoice.get("company"), framework)
    if not currency or currency == company_currency:
        return company_currency, Decimal(1)

    raw_rate = invoice.get("conversion_rate")
    # A legacy invoice without currency metadata can still use its original units.
    if not company_currency and not raw_rate:
        return "", Decimal(1)
    rate = _positive_rate(raw_rate)
    if rate is None:
        _invalid_rate(framework)
    return company_currency or currency, rate


def verified_invoice_approval_currency(invoice, framework) -> tuple[str, Decimal]:
    """Verify a new or changed invoice rate against the ERPNext posting-date quote."""
    company_currency, recorded = invoice_approval_currency(invoice, framework)
    currency = str(invoice.get("currency") or "").strip().upper()
    if not company_currency or not currency or currency == company_currency:
        return company_currency, recorded

    _, authoritative = profile_approval_currency(
        invoice, framework, posting_date=invoice.get("posting_date"), rate_type="for_selling"
    )
    # ERPNext rounds the stored conversion rate to nine decimal places.
    if recorded != authoritative and abs(recorded - authoritative) > Decimal("0.0000000005"):
        _invalid_rate(framework)
    return company_currency, authoritative


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
