"""Date helpers and domain validation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .errors import ValidationError


def parse_business_date(value: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` business date."""
    if not isinstance(value, str):
        raise ValidationError("business_date must be an ISO date string (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            f"business_date {value!r} is not a valid ISO date (YYYY-MM-DD)"
        ) from exc
    if parsed.isoformat() != value:
        raise ValidationError(
            f"business_date {value!r} must use canonical YYYY-MM-DD form"
        )
    return parsed


def parse_amount_cents(value: object) -> int:
    """Convert a decimal amount (e.g. ``12.34``) into integer cents.

    Rejects sub-cent precision and values that are not finite decimals so that
    downstream revenue reconciliation is exact.
    """
    if isinstance(value, bool):
        raise ValidationError("amount must be a decimal number, not a boolean")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("amount must be a decimal number") from exc
    if not decimal_value.is_finite():
        raise ValidationError("amount must be finite")
    cents = decimal_value * 100
    if cents != cents.to_integral_value():
        raise ValidationError("amount must not have sub-cent precision")
    if abs(cents) > Decimal("999999999999999"):
        raise ValidationError("amount is out of supported range")
    return int(cents)


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
