"""Tests for the import_history action."""

from datetime import date, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mytpu import TPUDataUpdateCoordinator, async_setup
from custom_components.mytpu.client import MyTPUError
from custom_components.mytpu.const import (
    DOMAIN,
    HISTORY_CHUNK_DAYS,
    HISTORY_OVERLAP_DAYS,
    MAX_HISTORY_CHUNKS,
    SERVICE_IMPORT_HISTORY,
)
from custom_components.mytpu.models import UsageReading

ENERGY_ID = f"{DOMAIN}:p_mock_power_meter_energy"
WATER_ID = f"{DOMAIN}:w_mock_water_meter_water"


def _day(days_ago: int) -> UsageReading:
    start = dt_util.start_of_local_day() - timedelta(days=days_ago)
    return UsageReading(date=dt_util.as_utc(start), consumption=1.0, unit="kWh")


def _recorder() -> MagicMock:
    """Recorder stand-in that runs on_done callbacks immediately."""
    recorder = MagicMock()

    def clear(statistic_ids, *, on_done=None):
        if on_done:
            on_done()

    recorder.async_clear_statistics.side_effect = clear
    return recorder


def _coordinator(hass, entry):
    coordinator = cast(Any, TPUDataUpdateCoordinator(hass, AsyncMock(), entry))
    coordinator.client.get_token_data = lambda: entry.data.get("token_data")
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


class TestFetchHistory:
    """Chunked backward fetching."""

    async def test_fetches_backward_until_empty(
        self, hass: HomeAssistant, make_config_entry, mock_power_service
    ):
        """Chunks walk back until TPU returns nothing; overlap is de-duplicated."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        boundary = _day(HISTORY_CHUNK_DAYS)
        coordinator.client.get_usage = AsyncMock(
            side_effect=[
                [boundary, _day(1)],
                [_day(HISTORY_CHUNK_DAYS + 5), boundary],
                [],
            ]
        )

        readings = await coordinator._fetch_history(mock_power_service, None)

        calls = coordinator.client.get_usage.call_args_list
        assert len(calls) == 3
        # Each chunk overlaps the newer one so its billed days carry invoices
        assert calls[1].kwargs["to_date"] == calls[0].kwargs["from_date"] + timedelta(
            days=HISTORY_OVERLAP_DAYS
        )
        assert calls[0].kwargs["to_date"] - calls[0].kwargs["from_date"] == (
            timedelta(days=HISTORY_CHUNK_DAYS)
        )
        assert [r.date for r in readings] == [
            _day(HISTORY_CHUNK_DAYS + 5).date,
            boundary.date,
            _day(1).date,
        ]

    async def test_overlap_prefers_rows_with_invoice(
        self, hass: HomeAssistant, make_config_entry, mock_power_service
    ):
        """A day returned by two chunks keeps the copy that carries its invoice."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        unlabeled = _day(HISTORY_CHUNK_DAYS + 1)
        labeled = UsageReading(
            date=unlabeled.date,
            consumption=unlabeled.consumption,
            unit=unlabeled.unit,
            invoice_date=date(2026, 1, 1),
        )
        coordinator.client.get_usage = AsyncMock(
            side_effect=[[_day(1)], [unlabeled], [labeled], []]
        )

        readings = await coordinator._fetch_history(mock_power_service, None)

        assert readings[0].invoice_date == date(2026, 1, 1)

    async def test_start_date_limits_range(
        self, hass: HomeAssistant, make_config_entry, mock_power_service
    ):
        """A start date inside the first chunk needs one request and trims rows."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        start = dt_util.as_local(_day(10).date).date()
        coordinator.client.get_usage = AsyncMock(return_value=[_day(11), _day(10)])

        readings = await coordinator._fetch_history(mock_power_service, start)

        coordinator.client.get_usage.assert_awaited_once()
        from_date = coordinator.client.get_usage.call_args.kwargs["from_date"]
        assert from_date == dt_util.start_of_local_day(start)
        assert [r.date for r in readings] == [_day(10).date]

    async def test_stops_at_chunk_limit(
        self, hass: HomeAssistant, make_config_entry, mock_power_service
    ):
        """A meter that never runs out of data stops after MAX_HISTORY_CHUNKS."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_usage = AsyncMock(return_value=[_day(1)])

        await coordinator._fetch_history(mock_power_service, None)

        assert coordinator.client.get_usage.await_count == MAX_HISTORY_CHUNKS


class TestImportHistory:
    """Rewriting statistics from fetched history."""

    async def test_rewrites_usage_and_keeps_cost(
        self, hass: HomeAssistant, make_config_entry
    ):
        """Usage is rewritten, costs are kept for repricing, then a refresh."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_usage = AsyncMock(side_effect=[[_day(2), _day(1)], []])
        recorder = _recorder()

        with (
            patch("custom_components.mytpu.get_instance", return_value=recorder),
            patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
        ):
            result = await coordinator.async_import_history()

        clears = [c.args[0] for c in recorder.async_clear_statistics.call_args_list]
        # Clear usage, then an empty clear that only waits for the commit
        assert clears == [[ENERGY_ID], []]
        assert recorder.async_clear_statistics.call_args.kwargs["on_done"]
        stats = mock_add.call_args.args[2]
        # Baseline row plus both days, with the running total starting from zero
        assert [s["sum"] for s in stats] == [0.0, 1.0, 2.0]
        assert result == {
            "power": {
                "days": 2,
                "first_day": dt_util.as_local(_day(2).date).date().isoformat(),
                "last_day": dt_util.as_local(_day(1).date).date().isoformat(),
            }
        }
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_failed_fetch_leaves_statistics(
        self, hass: HomeAssistant, make_config_entry
    ):
        """If any meter's fetch fails, nothing is cleared for any meter."""
        coordinator = _coordinator(
            hass, make_config_entry(include_power=True, include_water=True)
        )
        coordinator.client.get_usage = AsyncMock(
            side_effect=[[_day(1)], [], MyTPUError("boom")]
        )
        recorder = _recorder()

        with (
            patch("custom_components.mytpu.get_instance", return_value=recorder),
            pytest.raises(HomeAssistantError, match="Fetching history"),
        ):
            await coordinator.async_import_history()

        recorder.async_clear_statistics.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_empty_history_keeps_statistics(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A meter with no history returned keeps its existing statistics."""
        coordinator = _coordinator(hass, make_config_entry(include_water=True))
        coordinator.client.get_usage = AsyncMock(return_value=[])
        recorder = _recorder()

        with patch("custom_components.mytpu.get_instance", return_value=recorder):
            result = await coordinator.async_import_history()

        recorder.async_clear_statistics.assert_not_called()
        assert result == {"water": {"days": 0}}

    async def test_commit_timeout_blocks_until_confirmed(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A commit timeout fails the action and pauses updates until on_done."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_usage = AsyncMock(side_effect=[[_day(1)], []])
        coordinator.data = {"power": {"consumption": 1.0}}
        recorder = MagicMock()  # never calls on_done

        with (
            patch("custom_components.mytpu.get_instance", return_value=recorder),
            patch("custom_components.mytpu.HISTORY_COMMIT_TIMEOUT_SECONDS", 0.01),
            patch("custom_components.mytpu.async_add_external_statistics"),
            pytest.raises(HomeAssistantError, match="updates are paused"),
        ):
            await coordinator.async_import_history()
        coordinator.async_request_refresh.assert_not_awaited()

        # Updates keep the current data and don't touch TPU or statistics
        coordinator._do_fetch = AsyncMock(return_value={"fresh": True})
        assert await coordinator._async_update_data() == {"power": {"consumption": 1.0}}
        coordinator._do_fetch.assert_not_awaited()
        with pytest.raises(HomeAssistantError, match="hasn't been committed"):
            await coordinator.async_import_history()

        # The recorder eventually commits; updates resume
        on_done = recorder.async_clear_statistics.call_args.kwargs["on_done"]
        on_done()
        await hass.async_block_till_done()
        assert await coordinator._async_update_data() == {"fresh": True}

    async def test_pending_commit_survives_reload(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A coordinator replacing one on reload stays blocked by its pending rewrite."""
        entry = make_config_entry(include_power=True)
        old = _coordinator(hass, entry)
        old.client.get_usage = AsyncMock(side_effect=[[_day(1)], []])
        recorder = MagicMock()  # never calls on_done

        with (
            patch("custom_components.mytpu.get_instance", return_value=recorder),
            patch("custom_components.mytpu.HISTORY_COMMIT_TIMEOUT_SECONDS", 0.01),
            patch("custom_components.mytpu.async_add_external_statistics"),
            pytest.raises(HomeAssistantError, match="updates are paused"),
        ):
            await old.async_import_history()

        # The entry reloads: a fresh coordinator for the same entry
        new = _coordinator(hass, entry)
        new._do_fetch = AsyncMock(return_value={"fresh": True})
        assert await new._async_update_data() == {}
        new._do_fetch.assert_not_awaited()

        recorder.async_clear_statistics.call_args.kwargs["on_done"]()
        await hass.async_block_till_done()
        assert await new._async_update_data() == {"fresh": True}


class TestImportHistoryAction:
    """The mytpu.import_history action."""

    async def test_unknown_entry_rejected(self, hass: HomeAssistant):
        """An entry id that isn't a loaded TPU entry raises a validation error."""
        await async_setup(hass, {})
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_HISTORY,
                {"config_entry_id": "missing"},
                blocking=True,
                return_response=True,
            )

    async def test_requires_admin(self, hass: HomeAssistant, hass_read_only_user):
        """Non-admin users can't run the import."""
        await async_setup(hass, {})
        entry = MockConfigEntry(domain=DOMAIN, state=ConfigEntryState.LOADED)
        entry.add_to_hass(hass)
        coordinator = MagicMock()
        coordinator.async_import_history = AsyncMock(return_value={})
        hass.data[DOMAIN] = {entry.entry_id: {"coordinator": coordinator}}

        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_IMPORT_HISTORY,
                {"config_entry_id": entry.entry_id},
                blocking=True,
                return_response=True,
                context=Context(user_id=hass_read_only_user.id),
            )
        coordinator.async_import_history.assert_not_awaited()

    async def test_calls_coordinator(self, hass: HomeAssistant):
        """The action passes the start date through and returns the result."""
        await async_setup(hass, {})
        entry = MockConfigEntry(domain=DOMAIN, state=ConfigEntryState.LOADED)
        entry.add_to_hass(hass)
        coordinator = MagicMock()
        coordinator.async_import_history = AsyncMock(return_value={"power": {}})
        hass.data[DOMAIN] = {entry.entry_id: {"coordinator": coordinator}}

        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_IMPORT_HISTORY,
            {"config_entry_id": entry.entry_id, "start_date": "2025-01-01"},
            blocking=True,
            return_response=True,
        )

        coordinator.async_import_history.assert_awaited_once_with(date(2025, 1, 1))
        assert response == {"power": {}}
