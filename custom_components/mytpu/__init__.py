"""Tacoma Public Utilities integration for Home Assistant."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.util import get_instance
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .auth import AuthError, MyTPUAuth, ServerError
from .client import MyTPUClient, MyTPUError
from .const import (
    CONF_POWER_PRICE_ENTITY,
    CONF_POWER_SERVICE,
    CONF_TOKEN_DATA,
    CONF_UPDATE_INTERVAL_HOURS,
    CONF_WATER_PRICE_ENTITY,
    CONF_WATER_SERVICE,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    MIN_UPDATE_INTERVAL_HOURS,
)
from .models import Service, ServiceType

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


def _service_from_config(config_json: str) -> Service:
    """Reconstruct a Service object from stored JSON config."""
    data = json.loads(config_json)
    return Service(
        service_id=data["service_id"],
        service_number=data["service_number"],
        meter_number=data["meter_number"],
        display_meter_number=data["display_meter_number"],
        service_type=ServiceType(data["service_type"]),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        contract_number=data.get("contract_number"),
        totalizer=data.get("totalizer", False),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tacoma Public Utilities from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    auth = MyTPUAuth(entry.data.get(CONF_TOKEN_DATA))
    client = MyTPUClient(auth)

    coordinator = TPUDataUpdateCoordinator(hass, client, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.close()
        raise

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        await entry_data["coordinator"].client.close()

    return unload_ok


class TPUDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching TPU data."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: MyTPUClient,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        # Clamp in case an invalid value was saved before validation existed
        update_interval_hours = max(
            config_entry.options.get(
                CONF_UPDATE_INTERVAL_HOURS, DEFAULT_UPDATE_INTERVAL_HOURS
            ),
            MIN_UPDATE_INTERVAL_HOURS,
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=update_interval_hours),
        )
        self.client = client
        self.config_entry = config_entry

        # Parse service configs from JSON
        self.power_service: Service | None = None
        self.water_service: Service | None = None
        if config_entry.data.get(CONF_POWER_SERVICE):
            self.power_service = _service_from_config(
                config_entry.data[CONF_POWER_SERVICE]
            )
        if config_entry.data.get(CONF_WATER_SERVICE):
            self.water_service = _service_from_config(
                config_entry.data[CONF_WATER_SERVICE]
            )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data, re-logging in automatically if token refresh fails."""
        try:
            return await self._do_fetch()
        except ServerError as err:
            await self._relogin_or_raise(err)
            # Re-login succeeded — retry once with the fresh token
            try:
                return await self._do_fetch()
            except ServerError as retry_err:
                raise UpdateFailed(
                    f"MyTPU server error after re-login: {retry_err}"
                ) from retry_err

    async def _do_fetch(self) -> dict[str, Any]:
        """Fetch data from TPU. Raises ServerError on token refresh failure."""
        try:
            # Ensure we have account context
            await self.client.get_account_info()

            # Save updated token data to config entry if changed
            await self._save_token_data()

            data: dict[str, Any] = {}

            # Fetch and import power usage statistics
            if self.power_service:
                power_statistic_id = (
                    f"{DOMAIN}:{self.power_service.service_type.value}_{self.power_service.meter_number}".replace(
                        "-", "_"
                    ).lower()
                    + "_energy"
                )
                last_power_stats = await self.hass.async_add_executor_job(
                    get_last_statistics, self.hass, 1, power_statistic_id, True, {"sum"}
                )
                last_power_stat_time: float | None = None
                last_power_sum = 0.0
                if power_statistic_id in last_power_stats:
                    last_power_stat = last_power_stats[power_statistic_id][0]
                    last_power_stat_time = last_power_stat.get("start")
                    last_power_sum = float(last_power_stat.get("sum") or 0.0)

                power_migrated = self._needs_utc_migration(last_power_stat_time)
                if power_migrated:
                    _LOGGER.info("Migrating power statistics to local-time timestamps")
                    (
                        last_power_sum,
                        last_power_stat_time,
                    ) = await self._migrate_statistics(
                        power_statistic_id, self.power_service, "energy"
                    )

                if last_power_stat_time:
                    power_from_date = (
                        datetime.fromtimestamp(last_power_stat_time, tz=UTC)
                        + timedelta(days=1)
                    ).replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    power_from_date = datetime.now(UTC) - timedelta(days=90)

                power_readings = await self.client.get_usage(
                    self.power_service, from_date=power_from_date
                )
                new_power_stats: list[StatisticData] = []
                if power_readings:
                    new_power_stats = await self._import_statistics(
                        self.power_service,
                        power_readings,
                        "energy",
                        cumulative_sum=last_power_sum,
                        last_stat_time=last_power_stat_time,
                    )
                    # Keep latest reading in data for sensor attributes
                    latest = power_readings[-1]
                    data["power"] = {
                        "consumption": latest.consumption,
                        "date": latest.date,
                        "unit": latest.unit,
                    }

                # Migration only queues its rewrite, so the recorder may still
                # hold the old timestamps; price days on the next update instead.
                if not power_migrated:
                    await self._import_cost_statistics(
                        self.power_service,
                        "energy",
                        power_statistic_id,
                        new_power_stats,
                        self.config_entry.options.get(CONF_POWER_PRICE_ENTITY),
                    )

            # Fetch and import water usage statistics
            if self.water_service:
                water_statistic_id = (
                    f"{DOMAIN}:{self.water_service.service_type.value}_{self.water_service.meter_number}".replace(
                        "-", "_"
                    ).lower()
                    + "_water"
                )
                last_water_stats = await self.hass.async_add_executor_job(
                    get_last_statistics, self.hass, 1, water_statistic_id, True, {"sum"}
                )
                last_water_stat_time: float | None = None
                last_water_sum = 0.0
                if water_statistic_id in last_water_stats:
                    last_water_stat = last_water_stats[water_statistic_id][0]
                    last_water_stat_time = last_water_stat.get("start")
                    last_water_sum = float(last_water_stat.get("sum") or 0.0)

                water_migrated = self._needs_utc_migration(last_water_stat_time)
                if water_migrated:
                    _LOGGER.info("Migrating water statistics to local-time timestamps")
                    (
                        last_water_sum,
                        last_water_stat_time,
                    ) = await self._migrate_statistics(
                        water_statistic_id, self.water_service, "water"
                    )

                if last_water_stat_time:
                    water_from_date = (
                        datetime.fromtimestamp(last_water_stat_time, tz=UTC)
                        + timedelta(days=1)
                    ).replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    water_from_date = datetime.now(UTC) - timedelta(days=90)

                water_readings = await self.client.get_usage(
                    self.water_service, from_date=water_from_date
                )
                new_water_stats: list[StatisticData] = []
                if water_readings:
                    new_water_stats = await self._import_statistics(
                        self.water_service,
                        water_readings,
                        "water",
                        cumulative_sum=last_water_sum,
                        last_stat_time=last_water_stat_time,
                    )
                    # Keep latest reading in data for sensor attributes
                    latest = water_readings[-1]
                    data["water"] = {
                        "consumption": latest.consumption,
                        "date": latest.date,
                        "unit": latest.unit,
                    }

                # Migration only queues its rewrite, so the recorder may still
                # hold the old timestamps; price days on the next update instead.
                if not water_migrated:
                    await self._import_cost_statistics(
                        self.water_service,
                        "water",
                        water_statistic_id,
                        new_water_stats,
                        self.config_entry.options.get(CONF_WATER_PRICE_ENTITY),
                    )

            # Save token data again in case it was refreshed during usage fetching
            await self._save_token_data()

            return data

        except AuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except ServerError:
            raise  # propagates to _async_update_data for re-login handling
        except MyTPUError as err:
            raise UpdateFailed(f"API request failed: {err}") from err
        except TimeoutError as err:
            raise UpdateFailed("Request to MyTPU timed out") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error communicating with TPU")
            raise UpdateFailed(
                f"Unexpected error communicating with TPU: {err}"
            ) from err

    async def _relogin_or_raise(self, original_err: Exception) -> None:
        """Re-login using stored credentials, or raise ConfigEntryAuthFailed."""
        username = self.config_entry.data.get(CONF_USERNAME)
        password = self.config_entry.data.get(CONF_PASSWORD)
        if not username or not password:
            _LOGGER.warning(
                "Token refresh failed and no stored password — requesting reauth"
            )
            raise ConfigEntryAuthFailed(
                "Token expired — please re-authenticate"
            ) from original_err
        _LOGGER.info("Token refresh failed (server error), attempting re-login")
        try:
            await self.client.async_login(username, password)
            await self._save_token_data()
            _LOGGER.info("Re-login successful")
        except AuthError as err:
            _LOGGER.error("Re-login failed: %s", err)
            raise ConfigEntryAuthFailed(f"Re-login failed: {err}") from err

    async def _save_token_data(self) -> None:
        """Save updated token data to config entry if changed."""
        token_data = self.client.get_token_data()
        if token_data and token_data != self.config_entry.data.get(CONF_TOKEN_DATA):
            _LOGGER.debug("Token data changed, saving to config entry")
            new_data = {**self.config_entry.data, CONF_TOKEN_DATA: token_data}
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            _LOGGER.info("Token data saved successfully")
        else:
            _LOGGER.debug("Token data unchanged, no save needed")

    @staticmethod
    def _needs_utc_migration(last_stat_time: float | None) -> bool:
        """Return True if the last statistic used the old UTC-midnight format.

        Old code stored every reading at 00:00 UTC. New code stores at local
        midnight converted to UTC (e.g. 08:00 UTC for PST). If the last
        recorded statistic is at exactly 00:00 UTC and the local timezone has
        a non-zero UTC offset, the data was written by the old code.
        """
        if last_stat_time is None:
            return False
        last_utc = datetime.fromtimestamp(last_stat_time, tz=UTC)
        local_offset = last_utc.astimezone(dt_util.DEFAULT_TIME_ZONE).utcoffset()
        return (
            local_offset is not None
            and local_offset != timedelta(0)
            and last_utc.hour == 0
            and last_utc.minute == 0
        )

    async def _migrate_statistics(
        self,
        statistic_id: str,
        service: Service,
        stat_type: str,
    ) -> tuple[float, float | None]:
        """Re-timestamp existing statistics from UTC midnight to local-midnight UTC.

        Returns (last_cumulative_sum, last_stat_time) so the caller can compute
        an incremental from_date without re-querying TPU for historical data.
        """
        all_stats = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(1970, 1, 1, tzinfo=UTC),
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        existing = all_stats.get(statistic_id, [])
        if not existing:
            return 0.0, None

        corrected = [
            StatisticData(
                start=dt_util.as_utc(
                    datetime(
                        (old_utc := datetime.fromtimestamp(stat["start"], tz=UTC)).year,
                        old_utc.month,
                        old_utc.day,
                        tzinfo=dt_util.DEFAULT_TIME_ZONE,
                    )
                ),
                state=float(stat.get("state") or 0.0),
                sum=float(stat.get("sum") or 0.0),
            )
            for stat in existing
        ]

        get_instance(self.hass).async_clear_statistics([statistic_id])

        if stat_type == "energy":
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"TPU Energy {service.display_meter_number}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class="energy",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
        else:
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"TPU Water {service.display_meter_number}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class="volume",
                unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
            )

        async_add_external_statistics(self.hass, metadata, corrected)

        last = corrected[-1]
        return float(last["sum"] or 0.0), last["start"].timestamp()

    async def _import_statistics(
        self,
        service: Service,
        readings: list,
        stat_type: str,
        *,
        cumulative_sum: float = 0.0,
        last_stat_time: float | None = None,
    ) -> list[StatisticData]:
        """Import historical usage data as statistics.

        Returns the statistics rows that were written (empty if none).
        """
        if not readings:
            return []

        # Create statistic_id based on service
        # Sanitize to avoid validation errors: lowercase and replace hyphens
        meter_id = f"{service.service_type.value}_{service.meter_number}".replace(
            "-", "_"
        ).lower()
        statistic_id = f"{DOMAIN}:{meter_id}_{stat_type}"

        # Create metadata based on type
        if stat_type == "energy":
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"TPU Energy {service.display_meter_number}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class="energy",
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
        else:  # water
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"TPU Water {service.display_meter_number}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class="volume",
                unit_of_measurement=UnitOfVolume.CENTUM_CUBIC_FEET,
            )

        # Convert readings to StatisticData
        statistics: list[StatisticData] = []

        # If this is the first import (no previous statistics), add a baseline
        # statistic with sum=0 just before the first reading. This ensures the
        # Energy Dashboard correctly shows the first day's consumption instead
        # of the cumulative total.
        if cumulative_sum == 0.0 and readings:
            # readings[0].date is already UTC-aware from models.py
            first_reading_time = readings[0].date
            # Subtract 1 day to get previous day at midnight (valid hour boundary)
            baseline_time = first_reading_time - timedelta(days=1)
            statistics.append(
                StatisticData(
                    start=baseline_time,
                    state=0.0,
                    sum=0.0,
                )
            )

        for reading in readings:
            # reading.date is already UTC-aware from models.py
            start_time = reading.date

            # Skip if we've already imported this date
            # Compare using Unix timestamps like opower does
            if last_stat_time and start_time.timestamp() <= last_stat_time:
                continue

            # Add consumption to cumulative sum
            cumulative_sum += reading.consumption

            statistics.append(
                StatisticData(
                    start=start_time,
                    state=reading.consumption,
                    sum=cumulative_sum,
                )
            )

        # Import the statistics
        if statistics:
            async_add_external_statistics(self.hass, metadata, statistics)
        return statistics

    def _current_price(self, price_entity: str | None) -> float | None:
        """Return the current numeric price from an entity, or None."""
        if not price_entity:
            return None
        state = self.hass.states.get(price_entity)
        if state is None:
            _LOGGER.debug("Price entity %s not found", price_entity)
            return None
        try:
            price = float(state.state)
        except (TypeError, ValueError):
            price = None
        if price is None or not math.isfinite(price):
            _LOGGER.debug(
                "Price entity %s has non-numeric state %s", price_entity, state.state
            )
            return None
        return price

    async def _import_cost_statistics(
        self,
        service: Service,
        stat_type: str,
        consumption_statistic_id: str,
        new_stats: list[StatisticData],
        price_entity: str | None,
    ) -> None:
        """Import daily cost statistics priced at the entity's current value.

        Cost rows are written for every consumption day after the last cost row,
        so history is backfilled on first use and days missed while the price
        was unavailable are caught up later. Each day is priced once, at the
        price current when it is first written; later price changes do not
        rewrite past days.
        """
        price = self._current_price(price_entity)
        if price is None:
            return

        cost_statistic_id = f"{consumption_statistic_id}_cost"
        last_cost_stats = await self.hass.async_add_executor_job(
            get_last_statistics, self.hass, 1, cost_statistic_id, True, {"sum"}
        )
        last_cost_time: float | None = None
        cost_sum = 0.0
        if cost_statistic_id in last_cost_stats:
            last_cost = last_cost_stats[cost_statistic_id][0]
            last_cost_time = last_cost.get("start")
            cost_sum = float(last_cost.get("sum") or 0.0)

        query_start = (
            datetime.fromtimestamp(last_cost_time, tz=UTC) + timedelta(seconds=1)
            if last_cost_time is not None
            else datetime(1970, 1, 1, tzinfo=UTC)
        )
        existing = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            query_start,
            None,
            {consumption_statistic_id},
            "hour",
            None,
            {"state"},
        )

        # Merge recorder rows with rows queued in this update (the recorder
        # may not have committed them yet), keyed by start timestamp.
        days: dict[float, float] = {
            float(row["start"]): float(row.get("state") or 0.0)
            for row in existing.get(consumption_statistic_id, [])
        }
        for row in new_stats:
            days[row["start"].timestamp()] = float(row.get("state") or 0.0)

        pending = sorted(
            (start, usage)
            for start, usage in days.items()
            if last_cost_time is None or start > last_cost_time
        )
        if not pending:
            return

        statistics: list[StatisticData] = []
        if last_cost_time is None:
            statistics.append(
                StatisticData(
                    start=datetime.fromtimestamp(pending[0][0], tz=UTC)
                    - timedelta(days=1),
                    state=0.0,
                    sum=0.0,
                )
            )
        for start, usage in pending:
            cost = round(usage * price, 4)
            cost_sum += cost
            statistics.append(
                StatisticData(
                    start=datetime.fromtimestamp(start, tz=UTC),
                    state=cost,
                    sum=round(cost_sum, 4),
                )
            )

        label = "Energy" if stat_type == "energy" else "Water"
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"TPU {label} cost {service.display_meter_number}",
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )
        async_add_external_statistics(self.hass, metadata, statistics)
