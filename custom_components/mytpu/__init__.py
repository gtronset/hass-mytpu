"""Tacoma Public Utilities integration for Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
    from homeassistant.helpers.typing import ConfigType

import aiohttp
import voluptuous as vol
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
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .auth import AuthError, MyTPUAuth, ServerError
from .client import MyTPUClient, MyTPUError
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_START_DATE,
    BILLING_CHECK_INTERVAL_HOURS,
    BILLING_CURRENCY,
    BILLING_DIVISIONS,
    BILLING_HISTORY_DAYS,
    CONF_POWER_PRICE_ENTITY,
    CONF_POWER_SERVICE,
    CONF_TOKEN_DATA,
    CONF_UPDATE_INTERVAL_HOURS,
    CONF_WATER_PRICE_ENTITY,
    CONF_WATER_SERVICE,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    HISTORY_CHUNK_DAYS,
    HISTORY_COMMIT_TIMEOUT_SECONDS,
    HISTORY_OVERLAP_DAYS,
    MAX_HISTORY_CHUNKS,
    MIN_UPDATE_INTERVAL_HOURS,
    SERVICE_IMPORT_HISTORY,
    USAGE_TOLERANCE,
)
from .models import Bill, Service, ServiceType, UsageReading
from .pricing import BillUsage, daily_costs, rate_summary, summarize_bills

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Coordinator data and rate keys for each usage statistic type
SERVICE_KEYS = {"energy": "power", "water": "water"}

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_START_DATE): cv.date,
    }
)


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


@dataclass
class _StatisticsGuard:
    """Per-entry write state that must outlive a coordinator across reloads."""

    # Keeps scheduled updates and history imports from interleaving writes
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set by a statistics rewrite whose recorder commit hasn't been confirmed
    pending_commit: asyncio.Event | None = None


def _statistics_guard(hass: HomeAssistant, entry_id: str) -> _StatisticsGuard:
    """Return the entry's guard, shared by every coordinator created for it."""
    guards: dict[str, _StatisticsGuard] = hass.data.setdefault(f"{DOMAIN}_guards", {})
    return guards.setdefault(entry_id, _StatisticsGuard())


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""

    async def _async_import_history(call: ServiceCall) -> ServiceResponse:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise ServiceValidationError(
                f"{entry_id} is not a loaded Tacoma Public Utilities entry"
            )
        coordinator: TPUDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
            "coordinator"
        ]
        return await coordinator.async_import_history(call.data.get(ATTR_START_DATE))

    # Admin-only: the import replaces the account's usage and cost statistics
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _async_import_history,
        schema=IMPORT_HISTORY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


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

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        # Options this setup used, to tell option changes from saved tokens
        "options": dict(entry.options),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change.

    Update listeners also fire when entry data changes, which happens every
    time refreshed tokens are saved; those don't need a reload.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is not None and entry_data.get("options") == dict(entry.options):
        return
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
        # Shared with any coordinator that replaces this one on reload, so a
        # rewrite still awaiting the recorder keeps blocking the new one too
        self._guard = _statistics_guard(hass, config_entry.entry_id)

        # A meter's costs are rebuilt from bills when a statement newer than the
        # last one applied to it appears; applied bills survive restarts.
        self._billing_store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{config_entry.entry_id}.billing"
        )
        # Last bill applied to each meter, by "power"/"water"
        self._applied_bills: dict[str, str] = {}
        # Meters whose whole cost series must be repriced (after an import),
        # and meters with usage days left unpriced for lack of an estimate
        self._needs_reprice: set[str] = set()
        self._unpriced: set[str] = set()
        self._billing_loaded = False
        self._warned_shared_meters: set[str] = set()
        # Rates from bills, by "power"/"water": see pricing.rate_summary
        self.rates: dict[str, dict[str, Any]] = {}
        self._last_billing_check: datetime | None = None
        self._force_cost_rebuild = False

        if hass.config.currency != BILLING_CURRENCY:
            _LOGGER.warning(
                "MyTPU bills are in %s but Home Assistant's currency is %s; TPU "
                "cost statistics and rates are recorded in %s",
                BILLING_CURRENCY,
                hass.config.currency,
                BILLING_CURRENCY,
            )

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
        async with self._guard.lock:
            if self._history_commit_pending():
                # Reading statistics now could continue the old running totals
                _LOGGER.debug("Skipping update until the history rewrite commits")
                return self.data or {}
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
            # (service, stat_type, statistic_id, new rows, price entity, migrated)
            cost_jobs: list[
                tuple[Service, str, str, list[StatisticData], str | None, bool]
            ] = []

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

                cost_jobs.append(
                    (
                        self.power_service,
                        "energy",
                        power_statistic_id,
                        new_power_stats,
                        self.config_entry.options.get(CONF_POWER_PRICE_ENTITY),
                        power_migrated,
                    )
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

                cost_jobs.append(
                    (
                        self.water_service,
                        "water",
                        water_statistic_id,
                        new_water_stats,
                        self.config_entry.options.get(CONF_WATER_PRICE_ENTITY),
                        water_migrated,
                    )
                )

            # Migration only queues its rewrite, so the recorder may still hold
            # the old timestamps; price those days on the next update instead.
            rebuilt: set[str] = set()
            if not any(job[5] for job in cost_jobs):
                rebuilt = await self._maybe_rebuild_billed_costs(cost_jobs)
                # Days skipped earlier for lack of an estimate are filled once
                # one exists; the incremental import only prices later days
                rebuilt |= await self._reprice_at_estimate(
                    [
                        job
                        for job in cost_jobs
                        if job[1] not in rebuilt
                        and SERVICE_KEYS[job[1]] in self._unpriced
                    ],
                    fill_only=True,
                )
            for (
                service,
                stat_type,
                statistic_id,
                new_stats,
                price,
                migrated,
            ) in cost_jobs:
                if not migrated and stat_type not in rebuilt:
                    await self._import_cost_statistics(
                        service, stat_type, statistic_id, new_stats, price
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

    def _estimate_price(self, stat_type: str, price_entity: str | None) -> float | None:
        """Price for days not yet billed: the price entity, else the bill rate."""
        price = self._current_price(price_entity)
        if price is not None:
            return price
        return self.rates.get(SERVICE_KEYS[stat_type], {}).get("rate")

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
        rewrite past days, but the next bill rebuild replaces them. The price
        is the price entity's value, or else the trailing rate from bills.
        """
        price = self._estimate_price(stat_type, price_entity)
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

        async_add_external_statistics(
            self.hass, self._cost_metadata(service, stat_type), statistics
        )

    def _cost_metadata(self, service: Service, stat_type: str) -> StatisticMetaData:
        """Return metadata for a meter's cost statistic."""
        label = "Energy" if stat_type == "energy" else "Water"
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"TPU {label} cost {service.display_meter_number}",
            source=DOMAIN,
            statistic_id=f"{self._statistic_id(service, stat_type)}_cost",
            unit_class=None,
            unit_of_measurement=BILLING_CURRENCY,
        )

    @staticmethod
    def _statistic_id(service: Service, stat_type: str) -> str:
        """Return the usage statistic_id for a service."""
        meter_id = f"{service.service_type.value}_{service.meter_number}".replace(
            "-", "_"
        ).lower()
        return f"{DOMAIN}:{meter_id}_{stat_type}"

    async def async_import_history(
        self, start_date: date | None = None
    ) -> dict[str, Any]:
        """Merge all history TPU returns into the usage statistics.

        Days TPU returns take its values; days it doesn't (older than its
        history, or before start_date) are kept. Fetches every configured
        meter before touching the recorder, so a failed request leaves
        existing statistics untouched. Costs are kept; the refresh that
        follows reprices them, merging with existing costs for any day it
        can't reprice.
        """
        services = [
            (service, stat_type, key)
            for service, stat_type, key in (
                (self.power_service, "energy", "power"),
                (self.water_service, "water", "water"),
            )
            if service
        ]

        async with self._guard.lock:
            if self._history_commit_pending():
                raise HomeAssistantError(
                    "The previous history import hasn't been committed by the "
                    "recorder yet; try again later"
                )
            try:
                await self.client.get_account_info()
                fetched = [
                    (
                        service,
                        stat_type,
                        key,
                        await self._fetch_history(service, start_date),
                    )
                    for service, stat_type, key in services
                ]
                await self._save_token_data()
            except AuthError as err:
                raise HomeAssistantError(f"MyTPU authentication failed: {err}") from err
            except (ServerError, MyTPUError, TimeoutError, aiohttp.ClientError) as err:
                raise HomeAssistantError(
                    f"Fetching history from MyTPU failed: {err}"
                ) from err

            # Record that every meter needs repricing before rewriting anything,
            # so a failure or reload partway through still leads to a rebuild
            await self._async_load_billing()
            self._needs_reprice = {key for _, _, key, _ in fetched}
            self._force_cost_rebuild = True
            await self._async_save_billing()

            result: dict[str, Any] = {}
            for service, stat_type, key, readings in fetched:
                if not readings:
                    _LOGGER.warning(
                        "No %s history returned; existing statistics kept", key
                    )
                    result[key] = {"days": 0}
                    continue
                await self._merge_usage(service, stat_type, readings)
                result[key] = {
                    "days": len(readings),
                    "first_day": dt_util.as_local(readings[0].date).date().isoformat(),
                    "last_day": dt_util.as_local(readings[-1].date).date().isoformat(),
                }
                _LOGGER.info(
                    "Imported %s days of %s history (%s to %s)",
                    result[key]["days"],
                    key,
                    result[key]["first_day"],
                    result[key]["last_day"],
                )

        # Reprice costs from bills and refresh sensors from the rewritten series
        await self.async_request_refresh()
        return result

    async def _fetch_history(
        self, service: Service, start_date: date | None
    ) -> list[UsageReading]:
        """Fetch readings backward in chunks until TPU returns none.

        TPU only attaches a row's invoiceDate when the invoice falls inside the
        requested range, so the last days of an older chunk come back without
        one. Chunks overlap by HISTORY_OVERLAP_DAYS and a copy with an invoice
        date wins, so every billed day is labeled by the newer request.
        """
        floor = (
            dt_util.start_of_local_day(start_date) if start_date is not None else None
        )
        by_date: dict[datetime, UsageReading] = {}
        end = dt_util.now()
        for _ in range(MAX_HISTORY_CHUNKS):
            start = end - timedelta(days=HISTORY_CHUNK_DAYS)
            if floor is not None and start < floor:
                start = floor
            readings = await self.client.get_usage(
                service, from_date=start, to_date=end
            )
            if not readings:
                break
            for reading in readings:
                seen = by_date.get(reading.date)
                if seen is None or (
                    seen.invoice_date is None and reading.invoice_date is not None
                ):
                    by_date[reading.date] = reading
            if floor is not None and start <= floor:
                break
            end = start + timedelta(days=HISTORY_OVERLAP_DAYS)
        return [
            reading
            for day, reading in sorted(by_date.items())
            if floor is None or day >= floor
        ]

    def _history_commit_pending(self) -> bool:
        """Return True while a history rewrite awaits recorder confirmation."""
        guard = self._guard
        if guard.pending_commit is not None and guard.pending_commit.is_set():
            guard.pending_commit = None
        return guard.pending_commit is not None

    async def _rewrite_statistics(
        self, service: Service, stat_type: str, readings: list[UsageReading]
    ) -> None:
        """Replace a meter's usage series, then wait for the recorder to commit.

        Without the wait, the next update could read the old series. If the
        wait times out, updates and imports stay blocked until it commits.
        Costs are left alone for the following rebuild to reprice.
        """
        statistic_id = self._statistic_id(service, stat_type)
        get_instance(self.hass).async_clear_statistics([statistic_id])
        await self._import_statistics(service, readings, stat_type)
        await self._clear_and_wait_for_recorder([], f"{stat_type} history rewrite")

    async def _clear_and_wait_for_recorder(
        self, statistic_ids: list[str], label: str
    ) -> None:
        """Queue a clear of statistic_ids and wait until the recorder runs it.

        Recorder tasks run in order, so on_done fires only after every write
        queued before it has committed. An empty list clears nothing and only
        waits. If the wait times out, updates and imports stay blocked until
        on_done fires.
        """
        committed = asyncio.Event()
        self._guard.pending_commit = committed
        get_instance(self.hass).async_clear_statistics(
            statistic_ids,
            on_done=lambda: self.hass.loop.call_soon_threadsafe(committed.set),
        )
        try:
            async with asyncio.timeout(HISTORY_COMMIT_TIMEOUT_SECONDS):
                await committed.wait()
        except TimeoutError as err:
            raise HomeAssistantError(
                f"The recorder didn't confirm the {label} within "
                f"{HISTORY_COMMIT_TIMEOUT_SECONDS}s; updates are paused until it does"
            ) from err
        self._guard.pending_commit = None

    async def _maybe_rebuild_billed_costs(
        self,
        cost_jobs: list[
            tuple[Service, str, str, list[StatisticData], str | None, bool]
        ],
    ) -> set[str]:
        """Reprice meters' costs from bills when a statement they lack appears.

        Checks billing history at most once per BILLING_CHECK_INTERVAL_HOURS,
        or right away after a history import. A meter is repriced when its
        division has a bill newer than the one last applied to it, or when an
        import left it needing a full reprice; meters that can't be priced
        from bills are repriced at the estimate instead. Work is marked done
        only once that meter's costs are written, so anything unfinished is
        retried at the next check. Returns the stat types whose costs were
        written; the incremental cost import skips those.
        """
        if not cost_jobs:
            return set()
        await self._async_load_billing()
        now = dt_util.utcnow()
        if (
            not self._force_cost_rebuild
            and self._last_billing_check is not None
            and now - self._last_billing_check
            < timedelta(hours=BILLING_CHECK_INTERVAL_HOURS)
        ):
            return set()

        try:
            bills = await self.client.get_billing_history(
                now - timedelta(days=BILLING_HISTORY_DAYS), now
            )
            account_services = await self.client.get_services()
        except (MyTPUError, TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Fetching MyTPU billing history failed: %s", err)
            return set()
        self._last_billing_check = now
        self._force_cost_rebuild = False

        # Each meter is compared with the latest bill for its own division
        pending: list[tuple[tuple, Bill]] = []
        estimate_only: list[tuple] = []
        for job in cost_jobs:
            service, stat_type = job[0], job[1]
            key = SERVICE_KEYS[stat_type]
            division = BILLING_DIVISIONS[service.service_type.value]
            division_bills = [b for b in bills if b.amounts.get(division) is not None]
            if not division_bills or not self._bills_attributable(
                service, stat_type, account_services
            ):
                if key in self._needs_reprice:
                    estimate_only.append(job)
                continue
            latest = max(division_bills, key=lambda bill: bill.date)
            if (
                key in self._needs_reprice
                or self._applied_bills.get(key) != latest.date.isoformat()
            ):
                pending.append((job, latest))

        rebuilt = await self._reprice_at_estimate(estimate_only)
        if not pending:
            return rebuilt

        # Re-fetch usage so each day carries the bill that charged for it. Fetch
        # every meter before writing, so a failure leaves all costs as they were.
        try:
            fetched = [
                (job, latest, await self._fetch_history(job[0], None))
                for job, latest in pending
            ]
        except (MyTPUError, TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Fetching usage to price from bills failed: %s", err)
            self._last_billing_check = None
            return rebuilt

        # Usage imported earlier in this update is only queued; a correction
        # rewrite reading the series before it commits would drop those days
        if any(job[3] for job, _, _ in fetched):
            await self._clear_and_wait_for_recorder([], "usage import")

        for job, latest, readings in fetched:
            service, stat_type, statistic_id, new_stats, price_entity, _ = job
            key = SERVICE_KEYS[stat_type]
            await self._apply_corrections(service, stat_type, statistic_id, readings)

            division = BILLING_DIVISIONS[service.service_type.value]
            summaries = summarize_bills(readings, bills, division)
            self.rates[key] = rate_summary(summaries)
            invoices = await self._write_billed_costs(
                service,
                stat_type,
                statistic_id,
                new_stats,
                readings,
                summaries,
                self._estimate_price(stat_type, price_entity),
            )
            if invoices is None:
                _LOGGER.info("No %s usage to price from bills yet", key)
                continue
            rebuilt.add(stat_type)
            self._needs_reprice.discard(key)
            # Applied only once the latest bill's own days were written (a credit
            # or zero bill has no rate to wait for); otherwise retry next check
            if latest.date in invoices or latest.amounts[division] <= 0:
                self._applied_bills[key] = latest.date.isoformat()
                _LOGGER.info("Priced %s costs from bills through %s", key, latest.date)
            else:
                _LOGGER.info(
                    "%s usage for the %s bill isn't available yet; will retry",
                    key,
                    latest.date,
                )

        await self._async_save_billing()
        return rebuilt

    async def _reprice_at_estimate(
        self,
        cost_jobs: list[
            tuple[Service, str, str, list[StatisticData], str | None, bool]
        ],
        *,
        fill_only: bool = False,
    ) -> set[str]:
        """Reprice these meters' usage days at the estimate.

        With fill_only, only days without a cost are priced. Returns the stat
        types whose costs were written; a full reprice also clears the meter's
        pending reprice.
        """
        rebuilt: set[str] = set()
        for service, stat_type, statistic_id, new_stats, price_entity, _ in cost_jobs:
            estimate = self._estimate_price(stat_type, price_entity)
            if estimate is None:
                continue
            usage = await self._usage_days(statistic_id, new_stats)
            readings = [
                UsageReading(
                    date=datetime.fromtimestamp(day, tz=UTC),
                    consumption=consumption,
                    unit="",
                )
                for day, consumption in sorted(usage.items())
            ]
            written = await self._write_billed_costs(
                service,
                stat_type,
                statistic_id,
                new_stats,
                readings,
                {},
                estimate,
                fill_only=fill_only,
            )
            if written is not None:
                rebuilt.add(stat_type)
                if not fill_only:
                    self._needs_reprice.discard(SERVICE_KEYS[stat_type])
        if rebuilt:
            await self._async_save_billing()
        return rebuilt

    async def _apply_corrections(
        self,
        service: Service,
        stat_type: str,
        statistic_id: str,
        readings: list[UsageReading],
    ) -> None:
        """Update stored usage days whose TPU reading has since changed.

        TPU can replace an estimated reading with a confirmed one. Only days
        already in the usage series are compared and updated, so usage and
        costs keep describing the same consumption.
        """
        usage = await self._usage_days(statistic_id, [])
        stored = [r for r in readings if r.date.timestamp() in usage]
        corrected = [
            r
            for r in stored
            if abs(usage[r.date.timestamp()] - r.consumption) > USAGE_TOLERANCE
        ]
        if corrected:
            _LOGGER.info(
                "Updating %s %s readings that MyTPU has corrected",
                len(corrected),
                SERVICE_KEYS[stat_type],
            )
            await self._merge_usage(service, stat_type, stored)

    async def _usage_days(
        self, statistic_id: str, new_stats: list[StatisticData]
    ) -> dict[float, float]:
        """Return each usage day's consumption by start timestamp.

        Includes rows queued this update, which may not be committed yet, and
        leaves out the synthetic zero row the series opens with the day before
        its first reading. Real zero-usage days come later and are kept.
        """
        usage = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(1970, 1, 1, tzinfo=UTC),
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        rows = {
            float(row["start"]): (float(row.get("state") or 0.0), row.get("sum"))
            for row in usage.get(statistic_id, [])
        }
        rows.update(
            (row["start"].timestamp(), (float(row.get("state") or 0.0), row.get("sum")))
            for row in new_stats
        )
        days = sorted(rows)
        if days and rows[days[0]] in ((0.0, 0.0), (0.0, None)):
            del rows[days[0]]
        return {day: state for day, (state, _) in rows.items()}

    async def _merge_usage(
        self, service: Service, stat_type: str, readings: list[UsageReading]
    ) -> None:
        """Rewrite the usage series with these readings merged into it.

        Readings replace stored days with the same date; other stored days
        are kept. Running totals are recalculated from the first day.
        """
        statistic_id = self._statistic_id(service, stat_type)
        merged = {
            day: UsageReading(
                date=datetime.fromtimestamp(day, tz=UTC),
                consumption=consumption,
                unit="",
            )
            for day, consumption in (await self._usage_days(statistic_id, [])).items()
        }
        merged.update((reading.date.timestamp(), reading) for reading in readings)
        await self._rewrite_statistics(
            service, stat_type, [merged[day] for day in sorted(merged)]
        )

    def _bills_attributable(
        self, service: Service, stat_type: str, account_services: list[Service]
    ) -> bool:
        """Return True if this meter's bill amounts are its own.

        Bills give one amount per division, so with two meters of the same
        type on the account they can't be split between them; those costs
        stay on the estimate.
        """
        same_type = [
            s for s in account_services if s.service_type == service.service_type
        ]
        if len(same_type) <= 1:
            return True
        key = SERVICE_KEYS[stat_type]
        if key not in self._warned_shared_meters:
            self._warned_shared_meters.add(key)
            _LOGGER.warning(
                "The account has %s %s meters and bills don't say how much each "
                "was charged, so %s costs use the estimate instead of bills",
                len(same_type),
                key,
                key,
            )
        return False

    async def _async_load_billing(self) -> None:
        """Load applied bills and bill rates saved by a previous run."""
        if self._billing_loaded:
            return
        stored = await self._billing_store.async_load() or {}
        self._applied_bills = stored.get("applied") or {}
        self._needs_reprice = set(stored.get("needs_reprice") or [])
        self._unpriced = set(stored.get("unpriced") or [])
        self.rates = stored.get("rates") or {}
        self._billing_loaded = True

    async def _async_save_billing(self) -> None:
        """Save applied bills and bill rates."""
        await self._billing_store.async_save(
            {
                "applied": self._applied_bills,
                "needs_reprice": sorted(self._needs_reprice),
                "unpriced": sorted(self._unpriced),
                "rates": self.rates,
            }
        )

    async def _write_billed_costs(
        self,
        service: Service,
        stat_type: str,
        statistic_id: str,
        new_stats: list[StatisticData],
        readings: list[UsageReading],
        summaries: dict[date, BillUsage],
        estimate: float | None,
        *,
        fill_only: bool = False,
    ) -> set[date] | None:
        """Reprice a meter's cost series, keeping what can't be repriced.

        Every usage day gets its repriced cost, or else its existing cost
        (e.g. days older than MyTPU's history, or estimates when no estimate
        is available now). Days that only have a cost are kept too. With
        fill_only, existing costs are never replaced. Running totals are
        recalculated, and whether any usage day is still unpriced is
        remembered so it can be filled once an estimate exists.

        Returns the invoices whose days were written (unbilled days have none),
        or None, writing nothing, if no usage day could be priced.
        """
        usage = await self._usage_days(statistic_id, new_stats)
        repriced = {
            start.timestamp(): (cost, invoice)
            for start, cost, invoice in daily_costs(readings, summaries, estimate)
            if start.timestamp() in usage
        }

        metadata = self._cost_metadata(service, stat_type)
        cost_id = metadata["statistic_id"]
        existing = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(1970, 1, 1, tzinfo=UTC),
            None,
            {cost_id},
            "hour",
            None,
            {"state"},
        )
        previous = {
            float(row["start"]): float(row.get("state") or 0.0)
            for row in existing.get(cost_id, [])
        }
        # Like usage, the cost series opens with a synthetic zero row
        if previous and previous[min(previous)] == 0.0:
            del previous[min(previous)]

        if fill_only:
            repriced = {day: v for day, v in repriced.items() if day not in previous}
        if not repriced:
            return None

        priced: list[tuple[float, float]] = []
        for day in sorted(set(usage) | set(previous)):
            if day in repriced:
                priced.append((day, repriced[day][0]))
            elif day in previous:
                priced.append((day, previous[day]))

        key = SERVICE_KEYS[stat_type]
        if any(day not in repriced and day not in previous for day in usage):
            self._unpriced.add(key)
        else:
            self._unpriced.discard(key)

        first = datetime.fromtimestamp(priced[0][0], tz=UTC)
        statistics = [
            StatisticData(start=first - timedelta(days=1), state=0.0, sum=0.0)
        ]
        cost_sum = 0.0
        for day, cost in priced:
            cost_sum += cost
            statistics.append(
                StatisticData(
                    start=datetime.fromtimestamp(day, tz=UTC),
                    state=cost,
                    sum=round(cost_sum, 4),
                )
            )

        get_instance(self.hass).async_clear_statistics([cost_id])
        async_add_external_statistics(self.hass, metadata, statistics)
        await self._clear_and_wait_for_recorder([], f"{stat_type} cost rebuild")
        return {invoice for _, invoice in repriced.values() if invoice is not None}
