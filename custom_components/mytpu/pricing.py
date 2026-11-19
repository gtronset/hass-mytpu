"""Price daily usage from MyTPU billing statements."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from .models import Bill, UsageReading

_LOGGER = logging.getLogger(__name__)

# Daily usage this far off a bill's billed usage is reported in the log
MISMATCH_TOLERANCE = 0.05
# Bills averaged for the trailing rate used as the default estimate
TRAILING_RATE_BILLS = 12


@dataclass
class BillUsage:
    """A bill's charge for one division and the usage it's spread over."""

    invoice: date
    amount: float
    usage: float
    days: int


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


def summarize_bills(
    readings: list[UsageReading], bills: list[Bill], division: str
) -> dict[date, BillUsage]:
    """Return each bill's amount for a division and the usage to spread it over.

    The usage is the larger of the bill's billedConsumption and the summed
    daily usage, so daily costs never add up to more than the bill: a bill
    whose daily data covers only part of its period keeps the bill's rate,
    and daily usage exceeding the billed usage (e.g. after a corrected read)
    shares the bill's amount instead of inflating it. Bills with no amount for
    the division, or a zero or negative amount (credits, adjustments), are
    left out, so their days use the estimate.
    """
    readings = sorted(readings, key=lambda reading: reading.date)
    groups: dict[date, list[UsageReading]] = defaultdict(list)
    for reading, invoice in zip(readings, assign_invoices(readings), strict=True):
        if invoice is not None:
            groups[invoice].append(reading)

    amounts = {bill.date: bill.amounts.get(division) for bill in bills}
    summaries: dict[date, BillUsage] = {}
    for invoice, group in sorted(groups.items()):
        amount = amounts.get(invoice)
        if amount is None:
            continue
        if amount <= 0:
            _LOGGER.warning(
                "%s bill of %s has a %s amount of %.2f; pricing its days at the "
                "estimate instead",
                division,
                invoice,
                "zero" if amount == 0 else "negative",
                amount,
            )
            continue

        summed = sum(reading.consumption for reading in group)
        billed = max(
            (r.billed_consumption for r in group if r.billed_consumption is not None),
            default=None,
        )
        if billed and abs(summed - billed) > billed * MISMATCH_TOLERANCE:
            if summed > billed:
                _LOGGER.warning(
                    "%s bill of %s: daily usage %.2f exceeds billed usage %.2f; "
                    "spreading the bill's %.2f over the daily usage",
                    division,
                    invoice,
                    summed,
                    billed,
                    amount,
                )
            else:
                _LOGGER.info(
                    "%s bill of %s: daily data covers %.2f of %.2f billed usage "
                    "(%s days); pricing those days at the bill's rate",
                    division,
                    invoice,
                    summed,
                    billed,
                    len(group),
                )
        summaries[invoice] = BillUsage(
            invoice=invoice,
            amount=amount,
            usage=max(billed or 0.0, summed),
            days=len(group),
        )
    return summaries


def daily_costs(
    readings: list[UsageReading],
    summaries: dict[date, BillUsage],
    estimate: float | None,
) -> list[tuple[datetime, float]]:
    """Return (day start, cost) for each reading that can be priced.

    A billed day costs its usage times its bill's amount divided by the
    bill's usage (see summarize_bills). A bill with no usage at all is spread
    evenly over its days. Days without a usable bill use the estimate price;
    without one they're skipped.
    """
    readings = sorted(readings, key=lambda reading: reading.date)
    costs: list[tuple[datetime, float]] = []
    for reading, invoice in zip(readings, assign_invoices(readings), strict=True):
        summary = summaries.get(invoice) if invoice is not None else None
        if summary is not None and summary.usage > 0:
            cost = reading.consumption * summary.amount / summary.usage
        elif summary is not None:
            cost = summary.amount / summary.days
        elif estimate is not None:
            cost = reading.consumption * estimate
        else:
            continue
        costs.append((reading.date, round(cost, 4)))
    return costs


def rate_summary(summaries: dict[date, BillUsage]) -> dict[str, float | str | int]:
    """Return the trailing and last-bill rates per unit of usage.

    The trailing rate is the total charged over the last TRAILING_RATE_BILLS
    bills divided by their usage, so one unusual month (e.g. very low water
    use against fixed charges) doesn't swing it. Bills with no usage still
    count their amount. The last bill's rate is left out when it had no
    usage. Empty if no bill in the window has usage.
    """
    recent = sorted(summaries.values(), key=lambda s: s.invoice)[-TRAILING_RATE_BILLS:]
    usage = sum(s.usage for s in recent)
    if not usage:
        return {}
    last = recent[-1]
    summary: dict[str, float | str | int] = {
        "rate": round(sum(s.amount for s in recent) / usage, 6),
        "bills": len(recent),
        "last_bill_date": last.invoice.isoformat(),
    }
    if last.usage > 0:
        summary["last_bill_rate"] = round(last.amount / last.usage, 6)
    return summary
