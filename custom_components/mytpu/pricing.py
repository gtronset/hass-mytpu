"""Price daily usage from MyTPU billing statements."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from .models import Bill, UsageReading


def assign_invoices(readings: list[UsageReading]) -> list[date | None]:
    """Return the invoice date for each reading, in date order.

    TPU leaves invoiceDate empty on rows whose bill was issued after the
    request's end date. Bills cover consecutive days, so a day with no
    invoice that comes before a later billed day belongs to that next bill.
    Days after the last billed day are unbilled.
    """
    invoices = [reading.invoice_date for reading in readings]
    next_invoice: date | None = None
    for index in range(len(readings) - 1, -1, -1):
        if invoices[index] is not None:
            next_invoice = invoices[index]
        elif next_invoice is not None:
            invoices[index] = next_invoice
    return invoices


def daily_costs(
    readings: list[UsageReading],
    bills: list[Bill],
    division: str,
    estimate: float | None,
) -> list[tuple[datetime, float]]:
    """Return (day start, cost) for each reading that can be priced.

    A billed day costs its usage times that bill's effective rate: the
    division's amount divided by the usage the bill charged for. Using the
    bill's own billedConsumption keeps the rate right even when daily data
    covers only part of the bill. A bill that charged for no usage is spread
    evenly over its days. Unbilled days, and days whose bill has no amount
    for this division, use the estimate price; without one they're skipped.
    """
    readings = sorted(readings, key=lambda reading: reading.date)
    invoices = assign_invoices(readings)
    amounts = {bill.date: bill.amounts.get(division) for bill in bills}

    groups: dict[date, list[UsageReading]] = defaultdict(list)
    for reading, invoice in zip(readings, invoices, strict=True):
        if invoice is not None:
            groups[invoice].append(reading)

    # Cost per unit of usage, or per day for bills with no usage
    unit_rates: dict[date, float] = {}
    day_rates: dict[date, float] = {}
    for invoice, group in groups.items():
        amount = amounts.get(invoice)
        if amount is None:
            continue
        billed = max(
            (r.billed_consumption for r in group if r.billed_consumption is not None),
            default=None,
        )
        if not billed:
            billed = sum(r.consumption for r in group)
        if billed:
            unit_rates[invoice] = amount / billed
        else:
            day_rates[invoice] = amount / len(group)

    costs: list[tuple[datetime, float]] = []
    for reading, invoice in zip(readings, invoices, strict=True):
        if invoice in unit_rates:
            cost = reading.consumption * unit_rates[invoice]
        elif invoice in day_rates:
            cost = day_rates[invoice]
        elif estimate is not None:
            cost = reading.consumption * estimate
        else:
            continue
        costs.append((reading.date, round(cost, 4)))
    return costs
