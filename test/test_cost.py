"""Tests for cost statistics import."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.components.recorder.models import StatisticData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mytpu import TPUDataUpdateCoordinator
from custom_components.mytpu.const import (
    CONF_POWER_PRICE_ENTITY,
    DOMAIN,
)
from custom_components.mytpu.models import UsageReading

ENERGY_ID = f"{DOMAIN}:p_mock_power_meter_energy"
COST_ID = f"{ENERGY_ID}_cost"
DAY1 = datetime(2026, 1, 1, 8, tzinfo=UTC)
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY2 + timedelta(days=1)


def _coordinator(hass, entry):
    return TPUDataUpdateCoordinator(hass, AsyncMock(), entry)


async def test_no_price_entity_skips(
    hass: HomeAssistant, make_config_entry, mock_power_service
):
    """Without a configured price entity nothing is queried or written."""
    coordinator = _coordinator(hass, make_config_entry())
    with (
        patch("custom_components.mytpu.get_last_statistics") as mock_last,
        patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
    ):
        await coordinator._import_cost_statistics(
            mock_power_service, "energy", ENERGY_ID, [], None
        )
    mock_last.assert_not_called()
    mock_add.assert_not_called()


async def test_unavailable_price_skips(
    hass: HomeAssistant, make_config_entry, mock_power_service
):
    """A missing or non-numeric price entity writes nothing."""
    coordinator = _coordinator(hass, make_config_entry())
    hass.states.async_set("input_number.price", "unavailable")
    with patch("custom_components.mytpu.async_add_external_statistics") as mock_add:
        await coordinator._import_cost_statistics(
            mock_power_service, "energy", ENERGY_ID, [], "input_number.price"
        )
        await coordinator._import_cost_statistics(
            mock_power_service, "energy", ENERGY_ID, [], "input_number.missing"
        )
    mock_add.assert_not_called()


async def test_first_import_backfills_history(
    hass: HomeAssistant, make_config_entry, mock_power_service
):
    """First cost import prices recorder history plus queued rows, with a baseline."""
    coordinator = _coordinator(hass, make_config_entry())
    hass.states.async_set("input_number.price", "0.1")
    hass.config.currency = "USD"
    recorder_rows = {ENERGY_ID: [{"start": DAY1.timestamp(), "state": 10.0}]}
    queued = [StatisticData(start=DAY2, state=20.0, sum=30.0)]
    with (
        patch("custom_components.mytpu.get_last_statistics", return_value={}),
        patch(
            "custom_components.mytpu.statistics_during_period",
            return_value=recorder_rows,
        ),
        patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
    ):
        await coordinator._import_cost_statistics(
            mock_power_service, "energy", ENERGY_ID, queued, "input_number.price"
        )
    metadata, stats = mock_add.call_args.args[1], mock_add.call_args.args[2]
    assert metadata["statistic_id"] == COST_ID
    assert metadata["unit_of_measurement"] == "USD"
    assert metadata["unit_class"] is None
    assert metadata["has_sum"] is True
    assert "cost" in metadata["name"]
    assert [s["start"] for s in stats] == [DAY1 - timedelta(days=1), DAY1, DAY2]
    assert [s["state"] for s in stats] == [0.0, 1.0, 2.0]
    assert [s["sum"] for s in stats] == [0.0, 1.0, 3.0]


async def test_incremental_import_continues_sum(
    hass: HomeAssistant, make_config_entry, mock_power_service
):
    """Later imports skip priced days and continue the running sum."""
    coordinator = _coordinator(hass, make_config_entry())
    hass.states.async_set("input_number.price", "0.2")
    last = {COST_ID: [{"start": DAY1.timestamp(), "sum": 5.0}]}
    recorder_rows = {ENERGY_ID: [{"start": DAY2.timestamp(), "state": 10.0}]}
    queued = [StatisticData(start=DAY3, state=5.0, sum=0.0)]
    with (
        patch("custom_components.mytpu.get_last_statistics", return_value=last),
        patch(
            "custom_components.mytpu.statistics_during_period",
            return_value=recorder_rows,
        ) as mock_period,
        patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
    ):
        await coordinator._import_cost_statistics(
            mock_power_service, "energy", ENERGY_ID, queued, "input_number.price"
        )
    assert mock_period.call_args.args[1] > DAY1  # queried after last cost row
    stats = mock_add.call_args.args[2]
    assert [s["start"] for s in stats] == [DAY2, DAY3]
    assert [s["state"] for s in stats] == [2.0, 1.0]
    assert [s["sum"] for s in stats] == [7.0, 8.0]


async def test_nothing_pending(
    hass: HomeAssistant, make_config_entry, mock_power_service
):
    """No new consumption days means nothing is written."""
    coordinator = _coordinator(hass, make_config_entry())
    hass.states.async_set("input_number.price", "0.2")
    last = {COST_ID: [{"start": DAY2.timestamp(), "sum": 5.0}]}
    stale = {ENERGY_ID: [{"start": DAY1.timestamp(), "state": 10.0}]}
    with (
        patch("custom_components.mytpu.get_last_statistics", return_value=last),
        patch("custom_components.mytpu.statistics_during_period", return_value=stale),
        patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
    ):
        await coordinator._import_cost_statistics(
            mock_power_service, "energy", ENERGY_ID, [], "input_number.price"
        )
    mock_add.assert_not_called()


async def test_water_cost_name(
    hass: HomeAssistant, make_config_entry, mock_water_service
):
    """Water cost statistics get a water label."""
    coordinator = _coordinator(hass, make_config_entry())
    hass.states.async_set("sensor.water_price", "12.5")
    water_id = f"{DOMAIN}:w_mock_water_meter_water"
    rows = {water_id: [{"start": DAY1.timestamp(), "state": 0.2}]}
    with (
        patch("custom_components.mytpu.get_last_statistics", return_value={}),
        patch("custom_components.mytpu.statistics_during_period", return_value=rows),
        patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
    ):
        await coordinator._import_cost_statistics(
            mock_water_service, "water", water_id, [], "sensor.water_price"
        )
    metadata, stats = mock_add.call_args.args[1], mock_add.call_args.args[2]
    assert metadata["statistic_id"] == f"{water_id}_cost"
    assert "Water cost" in metadata["name"]
    assert stats[-1]["sum"] == 2.5


async def test_update_imports_cost_when_configured(
    hass: HomeAssistant, make_config_entry
):
    """A full update passes new rows and the configured price to the cost import."""
    base = make_config_entry(include_power=True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(base.data),
        options={CONF_POWER_PRICE_ENTITY: "input_number.price"},
    )
    coordinator = _coordinator(hass, entry)
    coordinator.client.get_account_info = AsyncMock()
    coordinator.client.get_token_data = lambda: entry.data.get("token_data")
    coordinator.client.get_usage = AsyncMock(
        return_value=[UsageReading(date=DAY1, consumption=10.0, unit="kWh")]
    )
    with (
        patch("custom_components.mytpu.get_last_statistics", return_value={}),
        patch("custom_components.mytpu.async_add_external_statistics"),
        patch.object(
            coordinator, "_import_cost_statistics", new=AsyncMock()
        ) as mock_cost,
    ):
        await coordinator._do_fetch()
    args = mock_cost.call_args.args
    assert args[1] == "energy"
    assert args[2] == ENERGY_ID
    assert args[3][-1]["state"] == 10.0
    assert args[4] == "input_number.price"
