"""Data models for MyTPU API responses."""

import contextlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from homeassistant.util import dt as dt_util


class ServiceType(Enum):
    """Type of utility service."""

    POWER = "P"
    WATER = "W"


@dataclass
class UsageReading:
    """A single usage reading from the meter."""

    date: datetime
    consumption: float
    unit: str
    high_temp: float | None = None
    low_temp: float | None = None
    demand_peak_time: datetime | None = None
    # The bill that charged for this day, and the usage that bill charged for
    invoice_date: date | None = None
    billed_consumption: float | None = None

    @classmethod
    def from_api_response(cls, data: dict) -> "UsageReading":
        """Create a UsageReading from API response data."""
        peak_time = None
        if data.get("demandPeakTime"):
            with contextlib.suppress(ValueError, TypeError):
                peak_time = datetime.strptime(data["demandPeakTime"], "%Y-%m-%d %H:%M")

        # Parse usageDate as midnight in HA's local timezone, then convert to UTC.
        # The API returns local (Pacific) calendar dates; storing as UTC midnight
        # would cause the Energy Dashboard to display readings shifted by UTC offset.
        local_midnight = datetime.strptime(data["usageDate"], "%Y-%m-%d").replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        utc_date = dt_util.as_utc(local_midnight)

        invoice_date = None
        if data.get("invoiceDate"):
            with contextlib.suppress(ValueError, TypeError):
                invoice_date = datetime.strptime(data["invoiceDate"], "%Y-%m-%d").date()
        billed_consumption = None
        if data.get("billedConsumption") not in (None, ""):
            with contextlib.suppress(ValueError, TypeError):
                billed_consumption = float(data["billedConsumption"])

        return cls(
            date=utc_date,
            consumption=data.get("usageConsumptionValue", 0.0),
            unit=data.get("uom", ""),
            high_temp=data.get("usageHighTemp"),
            low_temp=data.get("usageLowTemp"),
            demand_peak_time=peak_time,
            invoice_date=invoice_date,
            billed_consumption=billed_consumption,
        )


@dataclass
class Bill:
    """A billing statement's charges, keyed by service division."""

    date: date
    amounts: dict[str, float]


@dataclass
class Service:
    """A utility service (meter) on the account."""

    service_id: str
    service_number: str
    meter_number: str
    display_meter_number: str
    service_type: ServiceType
    latitude: str | None = None
    longitude: str | None = None
    contract_number: str | None = None
    totalizer: bool = False

    @classmethod
    def from_graph_response(cls, data: dict) -> "Service":
        """Create a Service from servicesForGraph API response data."""
        return cls(
            service_id=data.get("serviceId", ""),
            service_number=data.get("serviceNumber", ""),
            meter_number=data.get("meterNumber", ""),
            display_meter_number=data.get(
                "exportMeterNum", data.get("meterNumber", "")
            ),
            service_type=ServiceType(data.get("serviceType", "P")),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            contract_number=data.get("serviceContract"),
            totalizer=data.get("totalizerMeter") == "Y",
        )
