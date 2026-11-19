"""Tests for pricing costs from MyTPU bills."""

from datetime import date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.mytpu import TPUDataUpdateCoordinator
from custom_components.mytpu.auth import BASE_URL, MyTPUAuth, TokenInfo
from custom_components.mytpu.client import MyTPUClient, MyTPUError
from custom_components.mytpu.const import BILLING_CHECK_INTERVAL_HOURS, DOMAIN
from custom_components.mytpu.models import Bill, UsageReading
from custom_components.mytpu.pricing import (
    BillUsage,
    assign_invoices,
    daily_costs,
    rate_summary,
    summarize_bills,
)

ENERGY_ID = f"{DOMAIN}:p_mock_power_meter_energy"
COST_ID = f"{ENERGY_ID}_cost"
NOV_3 = date(2026, 11, 3)
OCT_5 = date(2026, 10, 5)


def _reading(
    day: int,
    consumption: float = 10.0,
    invoice: date | None = None,
    billed: float | None = None,
) -> UsageReading:
    """A reading for day `day` of October 2026 (local midnight, as UTC)."""
    start = dt_util.start_of_local_day(date(2026, 10, day))
    return UsageReading(
        date=dt_util.as_utc(start),
        consumption=consumption,
        unit="KWH",
        invoice_date=invoice,
        billed_consumption=billed,
    )


class TestModels:
    """Billing fields on usage rows."""

    def test_reading_parses_billing_fields(self):
        """invoiceDate and billedConsumption are parsed when present."""
        reading = UsageReading.from_api_response(
            {
                "usageDate": "2026-10-01",
                "usageConsumptionValue": 25.0,
                "invoiceDate": "2026-11-03",
                "billedConsumption": "729.0000000",
            }
        )
        assert reading.invoice_date == NOV_3
        assert reading.billed_consumption == 729.0

    def test_reading_without_billing_fields(self):
        """Unbilled rows have no invoice date or billed consumption."""
        reading = UsageReading.from_api_response(
            {
                "usageDate": "2026-11-10",
                "usageConsumptionValue": 25.0,
                "invoiceDate": "",
                "billedConsumption": "bad",
            }
        )
        assert reading.invoice_date is None
        assert reading.billed_consumption is None


class TestGetBillingHistory:
    """MyTPUClient.get_billing_history."""

    async def test_parses_statements(self, mock_account_info):
        """Division amounts are merged per billing date and sorted oldest first."""
        client = MyTPUClient(MyTPUAuth())
        client._account_context = mock_account_info["accountContext"]
        client._auth._token = TokenInfo(
            access_token="test",
            refresh_token="refresh",
            expires_at=9999999999,
            customer_id="CUST123",
        )
        payload = {
            "billingData": [
                {
                    "billingDate": "Nov 03, 2026",
                    "paymentAmount": "$137.42",
                    "bills": [
                        {
                            "billingDate": "2026-11-03",
                            "division": "Power",
                            "paymentAmount": "98.11",
                        },
                        {
                            "billingDate": "2026-11-03",
                            "division": "Water",
                            "paymentAmount": "$1,039.31",
                        },
                        {"billingDate": "2026-11-03", "division": None},
                    ],
                },
                {
                    "billingDate": "Oct 05, 2026",
                    "bills": [
                        {
                            "billingDate": "2026-10-05",
                            "division": "Power",
                            "paymentAmount": "103.06",
                        },
                    ],
                },
            ]
        }
        with (
            patch.object(client._auth, "get_token", new=AsyncMock(return_value="test")),
            aioresponses() as m,
        ):
            m.post(f"{BASE_URL}/rest/billing/history", payload=payload)
            async with client:
                bills = await client.get_billing_history(
                    datetime(2024, 10, 1), datetime(2026, 11, 18)
                )
            assert m.requests
            request = next(iter(m.requests.values()))[0]

        assert bills == [
            Bill(date=OCT_5, amounts={"Power": 103.06}),
            Bill(date=NOV_3, amounts={"Power": 98.11, "Water": 1039.31}),
        ]
        body = request.kwargs["json"]
        assert body["fromDate"] == "2024-10-01"
        assert body["toDate"] == "2026-11-18"
        assert "Power" in body["billingCategory"]


def _costs(readings, bills, division="Power", estimate=None):
    return daily_costs(readings, summarize_bills(readings, bills, division), estimate)


class TestPricing:
    """assign_invoices, summarize_bills, daily_costs and rate_summary."""

    def test_gap_days_join_the_next_bill(self):
        """Days without an invoice before a later billed day belong to that bill."""
        readings = [
            _reading(1, invoice=OCT_5),
            _reading(2),  # gap: TPU didn't attach the invoice
            _reading(3, invoice=NOV_3),
            _reading(4),  # unbilled tail
        ]
        assert assign_invoices(readings) == [OCT_5, NOV_3, NOV_3, None]

    def test_billed_days_use_bill_rate(self):
        """A billed day costs usage x amount / billedConsumption."""
        readings = [
            _reading(1, 10.0, NOV_3, billed=40.0),
            _reading(2, 30.0, NOV_3, billed=40.0),
        ]
        bills = [Bill(date=NOV_3, amounts={"Power": 8.0})]
        assert [c for _, c in _costs(readings, bills)] == [2.0, 6.0]

    def test_partial_bill_keeps_bill_rate(self, caplog):
        """With only part of a bill's days, the rate still comes from the bill."""
        readings = [_reading(1, 10.0, NOV_3, billed=100.0)]
        bills = [Bill(date=NOV_3, amounts={"Power": 12.0})]
        with caplog.at_level("INFO"):
            assert _costs(readings, bills) == [(readings[0].date, 1.2)]
        assert "covers 10.00 of 100.00 billed usage" in caplog.text

    def test_costs_never_exceed_bill(self, caplog):
        """Daily usage above the billed usage shares the bill instead of inflating it."""
        readings = [
            _reading(1, 10.0, NOV_3, billed=1.0),
            _reading(2, 10.0, NOV_3, billed=1.0),
        ]
        bills = [Bill(date=NOV_3, amounts={"Power": 4.0})]
        costs = _costs(readings, bills)
        assert [c for _, c in costs] == [2.0, 2.0]
        assert sum(c for _, c in costs) == 4.0
        assert "exceeds billed usage" in caplog.text

    @pytest.mark.parametrize("amount", [0.0, -12.5])
    def test_credit_bills_use_estimate(self, caplog, amount):
        """A zero or negative amount is an adjustment, not a rate."""
        readings = [_reading(1, 10.0, NOV_3, billed=10.0)]
        bills = [Bill(date=NOV_3, amounts={"Power": amount})]
        assert [c for _, c in _costs(readings, bills, estimate=0.1)] == [1.0]
        assert _costs(readings, bills) == []
        assert "pricing its days at the estimate" in caplog.text

    def test_unbilled_days_use_estimate(self):
        """Unbilled days and bills without this division use the estimate."""
        readings = [
            _reading(1, 10.0, OCT_5, billed=10.0),  # bill has no Power amount
            _reading(2, 5.0),  # unbilled
        ]
        bills = [Bill(date=OCT_5, amounts={"Water": 40.0})]
        assert [c for _, c in _costs(readings, bills, estimate=0.2)] == [2.0, 1.0]

    def test_unbilled_days_skipped_without_estimate(self):
        """Without an estimate, unbilled days aren't priced."""
        readings = [_reading(1, 10.0, NOV_3, billed=10.0), _reading(2, 5.0)]
        bills = [Bill(date=NOV_3, amounts={"Power": 1.0})]
        assert _costs(readings, bills) == [(readings[0].date, 1.0)]

    def test_bill_without_usage_spreads_evenly(self):
        """A bill that charged for no usage is spread evenly across its days."""
        readings = [
            _reading(1, 0.0, NOV_3, billed=0.0),
            _reading(2, 0.0, NOV_3, billed=0.0),
        ]
        bills = [Bill(date=NOV_3, amounts={"Water": 30.0})]
        assert [c for _, c in _costs(readings, bills, "Water")] == [15.0, 15.0]

    def test_rate_summary_uses_trailing_bills(self):
        """The rate is total charged / total usage over the last 12 bills."""
        summaries = {
            date(2025, m, 1): BillUsage(date(2025, m, 1), amount, usage, 30)
            for m, amount, usage in [(1, 999.0, 1.0)]
            + [(m, 10.0, 100.0) for m in range(2, 13)]
        }
        summaries[date(2026, 1, 1)] = BillUsage(date(2026, 1, 1), 30.0, 100.0, 30)

        summary = rate_summary(summaries)

        # The 13th-oldest bill (999/1) falls outside the trailing window
        assert summary["bills"] == 12
        assert summary["rate"] == round((11 * 10.0 + 30.0) / (12 * 100.0), 6)
        assert summary["last_bill_rate"] == 0.3
        assert summary["last_bill_date"] == "2026-01-01"
        assert rate_summary({}) == {}


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
    coordinator._billing_store = MagicMock()
    coordinator._billing_store.async_load = AsyncMock(return_value=None)
    coordinator._billing_store.async_save = AsyncMock()
    return coordinator


def _job(coordinator, price_entity=None):
    return (
        coordinator.power_service,
        "energy",
        ENERGY_ID,
        [],
        price_entity,
        False,
    )


class TestRebuildBilledCosts:
    """TPUDataUpdateCoordinator._maybe_rebuild_billed_costs."""

    async def test_new_bill_rebuilds_costs(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A new statement rewrites the cost series from bills and is remembered."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        readings = [
            _reading(1, 10.0, NOV_3, billed=20.0),
            _reading(2, 10.0, NOV_3, billed=20.0),
        ]
        coordinator.client.get_billing_history = AsyncMock(
            return_value=[Bill(date=NOV_3, amounts={"Power": 4.0})]
        )
        coordinator._fetch_history = AsyncMock(return_value=readings)
        recorder = _recorder()
        usage_rows = {ENERGY_ID: [{"start": r.date.timestamp()} for r in readings]}

        with (
            patch("custom_components.mytpu.get_instance", return_value=recorder),
            patch(
                "custom_components.mytpu.statistics_during_period",
                return_value=usage_rows,
            ),
            patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
        ):
            rebuilt = await coordinator._maybe_rebuild_billed_costs([_job(coordinator)])

        assert rebuilt is True
        clears = [c.args[0] for c in recorder.async_clear_statistics.call_args_list]
        assert clears == [[COST_ID], []]  # clear, write, then wait for commit
        metadata, stats = mock_add.call_args.args[1], mock_add.call_args.args[2]
        assert metadata["statistic_id"] == COST_ID
        assert [s["state"] for s in stats] == [0.0, 2.0, 2.0]
        assert [s["sum"] for s in stats] == [0.0, 2.0, 4.0]
        coordinator._billing_store.async_save.assert_awaited_once_with(
            {
                "applied_bill": "2026-11-03",
                "rates": {
                    "power": {
                        "rate": 0.2,
                        "bills": 1,
                        "last_bill_rate": 0.2,
                        "last_bill_date": "2026-11-03",
                    }
                },
            }
        )
        assert coordinator.rates["power"]["rate"] == 0.2

    async def test_same_bill_skips_rebuild(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A statement that was already applied doesn't trigger a rebuild."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator._billing_store.async_load = AsyncMock(
            return_value={"applied_bill": "2026-11-03"}
        )
        coordinator.client.get_billing_history = AsyncMock(
            return_value=[Bill(date=NOV_3, amounts={"Power": 4.0})]
        )
        coordinator._fetch_history = AsyncMock()

        assert not await coordinator._maybe_rebuild_billed_costs([_job(coordinator)])
        coordinator._fetch_history.assert_not_awaited()

    async def test_checks_at_most_daily(self, hass: HomeAssistant, make_config_entry):
        """Billing isn't requested again within the check interval."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_billing_history = AsyncMock(return_value=[])
        coordinator._last_billing_check = dt_util.utcnow() - timedelta(
            hours=BILLING_CHECK_INTERVAL_HOURS - 1
        )

        assert not await coordinator._maybe_rebuild_billed_costs([_job(coordinator)])
        coordinator.client.get_billing_history.assert_not_awaited()

    async def test_forced_rebuild_ignores_interval_and_applied_bill(
        self, hass: HomeAssistant, make_config_entry
    ):
        """After a history import, costs are rebuilt even for the same bill."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator._billing_store.async_load = AsyncMock(
            return_value={"applied_bill": "2026-11-03"}
        )
        coordinator._last_billing_check = dt_util.utcnow()
        coordinator._force_cost_rebuild = True
        coordinator.client.get_billing_history = AsyncMock(
            return_value=[Bill(date=NOV_3, amounts={"Power": 4.0})]
        )
        coordinator._fetch_history = AsyncMock(return_value=[])
        coordinator._write_billed_costs = AsyncMock()

        assert await coordinator._maybe_rebuild_billed_costs([_job(coordinator)])
        coordinator._write_billed_costs.assert_awaited_once()
        assert coordinator._force_cost_rebuild is False

    async def test_billing_failure_retries_next_update(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A failed billing request returns False and is retried next update."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_billing_history = AsyncMock(
            side_effect=MyTPUError("boom")
        )

        assert not await coordinator._maybe_rebuild_billed_costs([_job(coordinator)])
        assert coordinator._last_billing_check is None

    async def test_usage_failure_writes_nothing(
        self, hass: HomeAssistant, make_config_entry
    ):
        """If re-fetching usage fails, no meter's costs are touched."""
        coordinator = _coordinator(
            hass, make_config_entry(include_power=True, include_water=True)
        )
        coordinator.client.get_billing_history = AsyncMock(
            return_value=[Bill(date=NOV_3, amounts={"Power": 4.0})]
        )
        coordinator._fetch_history = AsyncMock(side_effect=[[], MyTPUError("boom")])
        coordinator._write_billed_costs = AsyncMock()
        water_job = (
            coordinator.water_service,
            "water",
            "mytpu:w_x_water",
            [],
            None,
            False,
        )

        assert not await coordinator._maybe_rebuild_billed_costs(
            [_job(coordinator), water_job]
        )
        coordinator._write_billed_costs.assert_not_awaited()
        coordinator._billing_store.async_save.assert_not_awaited()

    async def test_no_bills_clears_forced_rebuild(
        self, hass: HomeAssistant, make_config_entry
    ):
        """With no bills at all, a forced rebuild doesn't keep re-requesting them."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator._force_cost_rebuild = True
        coordinator.client.get_billing_history = AsyncMock(return_value=[])

        assert not await coordinator._maybe_rebuild_billed_costs([_job(coordinator)])
        assert coordinator._force_cost_rebuild is False

    async def test_update_skips_incremental_cost_after_rebuild(
        self, hass: HomeAssistant, make_config_entry
    ):
        """When bills rebuilt the costs, the price-entity import doesn't run."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_token_data = lambda: None
        coordinator.client.get_usage = AsyncMock(return_value=[])
        coordinator._maybe_rebuild_billed_costs = AsyncMock(return_value=True)
        coordinator._import_cost_statistics = AsyncMock()

        with patch("custom_components.mytpu.get_last_statistics", return_value={}):
            await coordinator._do_fetch()

        coordinator._maybe_rebuild_billed_costs.assert_awaited_once()
        coordinator._import_cost_statistics.assert_not_awaited()

    async def test_import_history_forces_rebuild(
        self, hass: HomeAssistant, make_config_entry
    ):
        """import_history asks the refresh that follows to rebuild from bills."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.client.get_token_data = lambda: None
        coordinator._fetch_history = AsyncMock(return_value=[_reading(1)])
        coordinator._rewrite_statistics = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()

        await coordinator.async_import_history()

        assert coordinator._force_cost_rebuild is True
        coordinator._billing_store.async_save.assert_awaited_once_with(
            {"applied_bill": None, "rates": {}}
        )
        coordinator.async_request_refresh.assert_awaited_once()


class TestEstimatePrice:
    """The price used for days not yet billed."""

    async def test_price_entity_overrides_bill_rate(
        self, hass: HomeAssistant, make_config_entry
    ):
        """A valid price entity wins; otherwise the trailing bill rate is used."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.rates = {"power": {"rate": 0.12}}
        hass.states.async_set("input_number.price", "0.15")

        assert coordinator._estimate_price("energy", "input_number.price") == 0.15
        assert coordinator._estimate_price("energy", None) == 0.12
        hass.states.async_set("input_number.price", "unavailable")
        assert coordinator._estimate_price("energy", "input_number.price") == 0.12
        assert coordinator._estimate_price("water", None) is None

    async def test_incremental_import_uses_bill_rate(
        self, hass: HomeAssistant, make_config_entry, mock_power_service
    ):
        """Without a price entity, new days are priced at the trailing bill rate."""
        coordinator = _coordinator(hass, make_config_entry(include_power=True))
        coordinator.rates = {"power": {"rate": 0.5}}
        day = _reading(1).date
        last = {COST_ID: [{"start": (day - timedelta(days=1)).timestamp(), "sum": 1.0}]}
        rows = {ENERGY_ID: [{"start": day.timestamp(), "state": 4.0}]}
        with (
            patch("custom_components.mytpu.get_last_statistics", return_value=last),
            patch(
                "custom_components.mytpu.statistics_during_period", return_value=rows
            ),
            patch("custom_components.mytpu.async_add_external_statistics") as mock_add,
        ):
            await coordinator._import_cost_statistics(
                mock_power_service, "energy", ENERGY_ID, [], None
            )
        stats = mock_add.call_args.args[2]
        assert [(s["state"], s["sum"]) for s in stats] == [(2.0, 3.0)]
