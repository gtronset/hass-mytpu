"""Tests for mytpu sensor platform."""

from collections.abc import Iterable
from datetime import datetime
from typing import cast
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.mytpu.const import CONF_POWER_SERVICE, CONF_WATER_SERVICE, DOMAIN
from custom_components.mytpu.sensor import (
    TPUEnergySensor,
    TPURateSensor,
    TPUWaterSensor,
    async_setup_entry,
)


@pytest.mark.asyncio
async def test_async_setup_entry_both_services(hass: HomeAssistant, mock_config_entry):
    """Test setting up sensors for both power and water."""
    mock_coordinator = MagicMock()
    hass.data[DOMAIN] = {mock_config_entry.entry_id: {"coordinator": mock_coordinator}}

    entities: list[Entity] = []

    def mock_add_entities(new_entities: Iterable[Entity]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(
        hass, mock_config_entry, cast(AddEntitiesCallback, mock_add_entities)
    )

    assert [type(e) for e in entities] == [
        TPUEnergySensor,
        TPURateSensor,
        TPUWaterSensor,
        TPURateSensor,
    ]


@pytest.mark.asyncio
async def test_async_setup_entry_power_only(hass: HomeAssistant, mock_power_service):
    """Test setting up sensor for power only."""
    import json

    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    # Create config entry with only power service
    power_only_entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_USERNAME: "test@example.com",
            CONF_PASSWORD: "testpassword123",
            CONF_POWER_SERVICE: json.dumps(
                {
                    "service_id": mock_power_service.service_id,
                    "service_number": mock_power_service.service_number,
                    "meter_number": mock_power_service.meter_number,
                    "display_meter_number": mock_power_service.display_meter_number,
                    "service_type": mock_power_service.service_type.value,
                }
            ),
        },
        unique_id="test_power_only",
        title="TPU - Power Only",
    )

    mock_coordinator = MagicMock()
    hass.data[DOMAIN] = {power_only_entry.entry_id: {"coordinator": mock_coordinator}}

    entities: list[Entity] = []

    def mock_add_entities(new_entities: Iterable[Entity]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(
        hass, power_only_entry, cast(AddEntitiesCallback, mock_add_entities)
    )

    assert [type(e) for e in entities] == [TPUEnergySensor, TPURateSensor]


@pytest.mark.asyncio
async def test_async_setup_entry_water_only(hass: HomeAssistant, mock_water_service):
    """Test setting up sensor for water only."""
    import json

    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    # Create config entry with only water service
    water_only_entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_USERNAME: "test@example.com",
            CONF_PASSWORD: "testpassword123",
            CONF_WATER_SERVICE: json.dumps(
                {
                    "service_id": mock_water_service.service_id,
                    "service_number": mock_water_service.service_number,
                    "meter_number": mock_water_service.meter_number,
                    "display_meter_number": mock_water_service.display_meter_number,
                    "service_type": mock_water_service.service_type.value,
                }
            ),
        },
        unique_id="test_water_only",
        title="TPU - Water Only",
    )

    mock_coordinator = MagicMock()
    hass.data[DOMAIN] = {water_only_entry.entry_id: {"coordinator": mock_coordinator}}

    entities: list[Entity] = []

    def mock_add_entities(new_entities: Iterable[Entity]) -> None:
        entities.extend(new_entities)

    await async_setup_entry(
        hass, water_only_entry, cast(AddEntitiesCallback, mock_add_entities)
    )

    assert [type(e) for e in entities] == [TPUWaterSensor, TPURateSensor]


class TestTPUEnergySensor:
    """Test TPUEnergySensor class."""

    def test_sensor_attributes(self, mock_config_entry):
        """Test sensor static attributes."""
        mock_coordinator = MagicMock()
        sensor = TPUEnergySensor(mock_coordinator, mock_config_entry)

        assert sensor.device_class == SensorDeviceClass.ENERGY
        assert sensor.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
        assert sensor.has_entity_name is True
        assert sensor.unique_id == f"{mock_config_entry.entry_id}_energy"

    def test_native_value_with_data(self, mock_config_entry):
        """Test native_value returns consumption from coordinator data."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {
            "power": {
                "consumption": 25.5,
                "date": datetime(2026, 1, 15),
                "unit": "kWh",
            }
        }
        sensor = TPUEnergySensor(mock_coordinator, mock_config_entry)

        assert sensor.native_value == 25.5

    def test_native_value_no_data(self, mock_config_entry):
        """Test native_value returns None when no data."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        sensor = TPUEnergySensor(mock_coordinator, mock_config_entry)

        assert sensor.native_value is None

    def test_native_value_no_power_key(self, mock_config_entry):
        """Test native_value returns None when power key missing."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"water": {"consumption": 1.5}}
        sensor = TPUEnergySensor(mock_coordinator, mock_config_entry)

        assert sensor.native_value is None

    def test_extra_state_attributes_with_data(self, mock_config_entry):
        """Test extra_state_attributes returns proper attributes."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {
            "power": {
                "consumption": 25.5,
                "date": datetime(2026, 1, 15, 12, 0, 0),
                "unit": "kWh",
            }
        }
        sensor = TPUEnergySensor(mock_coordinator, mock_config_entry)

        attrs = sensor.extra_state_attributes

        assert "last_reading_date" in attrs
        assert attrs["last_reading_date"] == "2026-01-15T12:00:00"
        assert attrs["unit"] == "kWh"

    def test_extra_state_attributes_no_data(self, mock_config_entry):
        """Test extra_state_attributes returns empty dict when no data."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        sensor = TPUEnergySensor(mock_coordinator, mock_config_entry)

        attrs = sensor.extra_state_attributes

        assert attrs == {}


class TestTPUWaterSensor:
    """Test TPUWaterSensor class."""

    def test_sensor_attributes(self, mock_config_entry):
        """Test sensor static attributes."""
        mock_coordinator = MagicMock()
        sensor = TPUWaterSensor(mock_coordinator, mock_config_entry)

        assert sensor.device_class == SensorDeviceClass.WATER
        assert sensor.native_unit_of_measurement == UnitOfVolume.CENTUM_CUBIC_FEET
        assert sensor.has_entity_name is True
        assert sensor.unique_id == f"{mock_config_entry.entry_id}_water"

    def test_native_value_with_data(self, mock_config_entry):
        """Test native_value returns consumption from coordinator data."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {
            "water": {
                "consumption": 1.5,
                "date": datetime(2026, 1, 15),
                "unit": "CCF",
            }
        }
        sensor = TPUWaterSensor(mock_coordinator, mock_config_entry)

        assert sensor.native_value == 1.5

    def test_native_value_no_data(self, mock_config_entry):
        """Test native_value returns None when no data."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        sensor = TPUWaterSensor(mock_config_entry, mock_config_entry)

        assert sensor.native_value is None

    def test_native_value_no_water_key(self, mock_config_entry):
        """Test native_value returns None when water key missing."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"power": {"consumption": 25.5}}
        sensor = TPUWaterSensor(mock_coordinator, mock_config_entry)

        assert sensor.native_value is None

    def test_extra_state_attributes_with_data(self, mock_config_entry):
        """Test extra_state_attributes returns proper attributes."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {
            "water": {
                "consumption": 1.5,
                "date": datetime(2026, 1, 15, 12, 0, 0),
                "unit": "CCF",
            }
        }
        sensor = TPUWaterSensor(mock_coordinator, mock_config_entry)

        attrs = sensor.extra_state_attributes

        assert "last_reading_date" in attrs
        assert attrs["last_reading_date"] == "2026-01-15T12:00:00"
        assert attrs["unit"] == "CCF"

    def test_extra_state_attributes_no_data(self, mock_config_entry):
        """Test extra_state_attributes returns empty dict when no data."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        sensor = TPUWaterSensor(mock_coordinator, mock_config_entry)

        attrs = sensor.extra_state_attributes

        assert attrs == {}


class TestTPURateSensor:
    """Rate sensors from bills."""

    def _sensor(self, mock_config_entry, rates, service="water"):
        coordinator = MagicMock()
        coordinator.hass.config.currency = "USD"
        coordinator.rates = rates
        return TPURateSensor(
            coordinator,
            mock_config_entry,
            "_water_rate",
            service,
            "water_rate",
            UnitOfVolume.CENTUM_CUBIC_FEET,
        )

    def test_state_and_attributes(self, mock_config_entry):
        """The state is the trailing rate; attributes describe the last bill."""
        sensor = self._sensor(
            mock_config_entry,
            {
                "water": {
                    "rate": 11.2,
                    "bills": 12,
                    "last_bill_rate": 15.95,
                    "last_bill_date": "2026-11-03",
                }
            },
        )
        assert sensor.native_value == 11.2
        assert sensor.native_unit_of_measurement == "USD/CCF"
        assert sensor.unique_id == f"{mock_config_entry.entry_id}_water_rate"
        assert sensor.extra_state_attributes == {
            "last_bill_rate": 15.95,
            "last_bill_date": "2026-11-03",
            "bills": 12,
        }

    def test_no_bills_yet(self, mock_config_entry):
        """Before any bill has been priced, the rate is unknown."""
        sensor = self._sensor(mock_config_entry, {})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}
