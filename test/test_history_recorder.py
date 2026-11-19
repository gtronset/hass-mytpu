"""import_history against a real recorder."""

from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.mytpu import TPUDataUpdateCoordinator
from custom_components.mytpu.const import DOMAIN
from custom_components.mytpu.models import Bill, UsageReading

ENERGY_ID = f"{DOMAIN}:p_mock_power_meter_energy"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start the recorder before hass, which the conftest fixture would set up first."""
    yield


def _day(days_ago: int) -> UsageReading:
    start = dt_util.start_of_local_day() - timedelta(days=days_ago)
    return UsageReading(date=dt_util.as_utc(start), consumption=1.0, unit="kWh")


async def test_rewrite_committed_before_return(hass: HomeAssistant, make_config_entry):
    """With a real recorder, the rewritten series is readable once the call returns."""
    entry = make_config_entry(include_power=True)
    coordinator = cast(Any, TPUDataUpdateCoordinator(hass, AsyncMock(), entry))
    coordinator.client.get_token_data = lambda: entry.data.get("token_data")
    coordinator.async_request_refresh = AsyncMock()
    service = coordinator.power_service
    # Existing short series whose running total doesn't start at zero
    await coordinator._import_statistics(
        service, [_day(3)], "energy", cumulative_sum=100.0
    )
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="cost",
            source=DOMAIN,
            statistic_id=f"{ENERGY_ID}_cost",
            unit_class=None,
            unit_of_measurement="USD",
        ),
        [StatisticData(start=_day(3).date, state=1.0, sum=50.0)],
    )
    await async_wait_recording_done(hass)

    coordinator.client.get_usage = AsyncMock(
        side_effect=[[_day(5), _day(4), _day(3)], []]
    )
    await coordinator.async_import_history()

    # No extra wait: the import must not return before the recorder commits
    last = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, ENERGY_ID, True, {"sum"}
    )
    assert last[ENERGY_ID][0]["sum"] == 3.0
    # Costs are kept for the following rebuild to reprice
    cost = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, f"{ENERGY_ID}_cost", True, {"sum"}
    )
    assert cost[f"{ENERGY_ID}_cost"][0]["sum"] == 50.0


async def test_billed_cost_rebuild_committed_before_return(
    hass: HomeAssistant, make_config_entry
):
    """With a real recorder, rebuilt costs are readable once the rebuild returns."""
    entry = make_config_entry(include_power=True)
    coordinator = cast(Any, TPUDataUpdateCoordinator(hass, AsyncMock(), entry))
    service = coordinator.power_service
    invoice = dt_util.as_local(_day(0).date).date()
    readings = [
        UsageReading(
            date=r.date,
            consumption=r.consumption,
            unit=r.unit,
            invoice_date=invoice,
            billed_consumption=2.0,
        )
        for r in (_day(2), _day(1))
    ]
    await coordinator._import_statistics(service, readings, "energy")
    await async_wait_recording_done(hass)

    coordinator.client.get_billing_history = AsyncMock(
        return_value=[Bill(date=invoice, amounts={"Power": 5.0})]
    )
    coordinator.client.get_services = AsyncMock(return_value=[service])
    coordinator._fetch_history = AsyncMock(return_value=readings)
    job = (service, "energy", ENERGY_ID, [], None, False)
    assert await coordinator._maybe_rebuild_billed_costs([job]) == {"energy"}

    # No extra wait: the rebuild must not return before the recorder commits
    cost = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, f"{ENERGY_ID}_cost", True, {"sum"}
    )
    assert cost[f"{ENERGY_ID}_cost"][0]["sum"] == 5.0
