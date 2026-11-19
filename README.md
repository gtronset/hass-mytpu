# MyTPU - Tacoma Public Utilities Integration

[![Test](https://github.com/thetic/hass-mytpu/actions/workflows/test.yml/badge.svg)](https://github.com/thetic/hass-mytpu/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/thetic/hass-mytpu/graph/badge.svg)](https://codecov.io/gh/thetic/hass-mytpu)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Home Assistant custom component for tracking energy and water usage from [Tacoma Public Utilities](https://www.mytpu.org/).

> [!NOTE]
> This is an unofficial, community-developed integration and is not affiliated with, endorsed by, or supported by Tacoma Public Utilities (TPU). Use at your own risk.

## Features

- Track daily electricity consumption (kWh)
- Track daily water consumption (CCF)
- Compatible with Home Assistant's Energy Dashboard
- Automatic hourly updates

## Requirements

- Home Assistant 2025.12.0 or newer
- A MyTPU online account at [myaccount.mytpu.org](https://myaccount.mytpu.org)

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=thetic&repository=hass-mytpu&category=integration)

1. Open HACS in Home Assistant
2. Search for "Tacoma Public Utilities"
3. Click "Download"
4. Restart Home Assistant

<details>
<summary><strong>Manual Installation</strong></summary>

1. Copy the `custom_components/mytpu` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant
</details>

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "Tacoma Public Utilities"
4. Enter your MyTPU username and password
5. Select which meters to track (the integration will automatically discover all available meters on your account)

## Sensors

This integration creates the following sensors:

| Sensor             | Description             | Unit | Device Class |
| ------------------ | ----------------------- | ---- | ------------ |
| Energy Consumption | Daily electricity usage | kWh  | energy       |
| Water Consumption  | Daily water usage       | CCF  | water        |
| Energy Rate        | Electricity price per kWh from your bills | USD/kWh | |
| Water Rate         | Drinking water price per CCF from your bills | USD/CCF | |

The rate sensors show the total charged over your last 12 bills divided by the
usage those bills charged for, including fixed charges and taxes. Attributes
give the most recent bill's rate and date. They update when a new bill arrives.

Historical data and cumulative totals are tracked using Home Assistant's [statistics](https://www.home-assistant.io/integrations/statistics/) system for proper [Energy Dashboard](https://www.home-assistant.io/docs/energy/) integration.

## Energy Dashboard Setup

Usage is imported as external statistics (`mytpu:p_<meter>_energy` and
`mytpu:w_<meter>_water`), one row per day. TPU publishes readings once a day,
a day or more behind, so recent days fill in later.

1. Go to **Settings** → **Dashboards** → **Energy**
2. Under "Electricity grid", click "Add consumption"
3. Select the "TPU Energy …" statistic
4. Under "Water consumption", click "Add water source"
5. Select the "TPU Water …" statistic

### Costs

Home Assistant can't apply a price entity or a static price to external
statistics, so the integration imports cost statistics itself:
`mytpu:p_<meter>_energy_cost` and `mytpu:w_<meter>_water_cost`, in US dollars
like your bills (a price entity should be in USD too).

**Billed days come from your actual MyTPU bills.** Once a day the integration
checks your billing history. When a new statement appears, it rebuilds the cost
series: each billed day costs its usage times that bill's effective rate (the
bill's Electricity or Drinking water amount divided by the usage it charged
for). Totals per bill match what you were charged, including that service's
fixed charges and taxes. Wastewater, solid waste and stormwater aren't included.

**Days since the last bill use an estimate:** the Energy Rate or Water Rate
above, so no helpers are needed. To override it, for example after a known
rate increase, pick an entity holding your price (an `input_number` or a
template sensor, per kWh or per CCF) as the power and/or water price entity in
the integration's **Configure** dialog. When the next bill arrives, it replaces
the estimates for its days.

Each new bill reprices every day MyTPU still returns. Days it no longer returns
(older than about two years) keep the costs they already had. If the account
has two meters of the same type, bills can't be split between them, so that
type's costs use the estimate.

Bills are checked against their daily usage. A bill whose daily usage exceeds
what it billed (e.g. after a corrected meter read) has its amount shared across
those days rather than inflated, and a credit or zero amount is priced at the
estimate instead. Both are reported in the Home Assistant log.

In the Energy settings, choose **Use an entity tracking the total costs** and
select the "TPU Energy cost …" (or "TPU Water cost …") statistic.

Fixed charges are spread by usage, so a bill's total is exact but busier days
carry more of it. This matters most for water, where fixed charges are a large
part of the bill.

## Importing History

The first setup imports the last 90 days. To bring in everything MyTPU still
has (about two years of daily readings), run the **Tacoma Public Utilities:
Import history** action (`mytpu.import_history`) from **Developer Tools** →
**Actions**:

```yaml
action: mytpu.import_history
data:
  config_entry_id: <your TPU entry>
  # start_date: "2025-01-01"  # optional; omit to import everything
```

The integration fetches history backward from today, a year at a time, until
MyTPU returns no more readings. Only once every meter has been fetched does it
merge the history into the usage statistics: days MyTPU returns take its values,
and days it doesn't (older than its history, or before `start_date`) are kept.
If a request fails, the existing statistics are left alone. Costs are then
repriced from your bills.

The action returns the number of days and the date range imported for each
meter. It can only be run by an administrator. Taking a Home Assistant backup
first is a good idea, since the usage statistics are rewritten.

Days MyTPU later corrects (for example an estimated reading replaced by a
confirmed one) are also updated whenever a new bill is priced.

## Acknowledgments

This project was inspired by and builds upon the work of [ex-nerd/mytpu](https://github.com/ex-nerd/mytpu).
