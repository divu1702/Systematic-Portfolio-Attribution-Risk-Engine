"""Display formatting helpers."""

from __future__ import annotations


_CURRENCY = "INR"


def set_currency(code: str) -> None:
    """Display currency for money(): INR uses Indian digit grouping, USD western grouping."""
    global _CURRENCY
    _CURRENCY = code


def money(amount: float, decimals: int = 0) -> str:
    if _CURRENCY == "USD":
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.{decimals}f}"
    return _inr(amount, decimals)


def inr(amount: float, decimals: int = 0) -> str:
    """Money in the current display currency (rupees by default, the name predates USD support)."""
    return money(amount, decimals)


def _inr(amount: float, decimals: int = 0) -> str:
    """Format rupees with Indian digit grouping: 500000 -> '₹5,00,000'."""
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    whole, _, frac = f"{amount:.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}" + (f".{frac}" if frac else "")


def pct(x: float, decimals: int = 1) -> str:
    return f"{x * 100:.{decimals}f}%"
