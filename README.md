# Deyeconfig

**English** | [Español](README.es.md)

Read and change the configuration of Deye hybrid inverters through the Solarman
data logger on your local network (Modbus TCP, port 8899), without relying on the
cloud. Includes an automatic mode that charges the battery in the cheapest hours
from the real hourly electricity prices and protects the house against storms and
power outages.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Project structure](#project-structure)
- [Connection test](#connection-test)
- [Reading the inverter configuration](#reading-the-inverter-configuration)
- [Profiles](#profiles)
- [Exporting the current configuration](#exporting-the-current-configuration)
- [Automatic mode](#automatic-mode)
- [Configuration reference](#configuration-reference)
- [Backups, state and rolling back](#backups-state-and-rolling-back)
- [Register maps](#register-maps)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [Credits](#credits)
- [Disclaimer](#disclaimer)

## Features

- **Local access**: talks directly to the inverter through the Solarman stick
  (Modbus TCP), no cloud account or API quota needed.
- **Full configuration readout**: battery, grid and generator charging, work mode,
  grid protections, GEN port / SmartLoad and the 6 Time Of Use slots.
- **Profiles**: small TOML files with only the parameters you want to change, in
  normal units. Validated, backed up and verified on every write.
- **Export**: dump the current configuration as a profile to keep it or go back to it.
- **Automatic mode**: plans grid charging from the real hourly PVPC prices, the
  solar forecast and the consumption, keeps a reserve when storms are forecast and
  makes the whole battery available during an outage.
- **Safe by default**: nothing is written without `--apply`, critical parameters are
  read-only, every write is preceded by a backup and read back to verify.

## Requirements

- A Deye hybrid inverter with a Solarman WiFi/LAN data logger (stick) on your local
  network. Tested with a SUN-6K-SG03LP1-EU (see [Register maps](#register-maps)).
- The logger's **IP address** and **serial number** (the logger's, not the inverter's):
  - Serial number: on the sticker of the stick, or in the Solarman app (device info).
  - IP address: in your router's list of connected devices. It is a good idea to give
    it a fixed IP with a DHCP reservation. You can also find it by looking for the
    device with TCP port 8899 open on your network.
- Python 3.11 or later on a machine on the same network (a small Linux VM is enough).
- For the automatic mode: Internet access (prices and weather forecast) and, for outage protection, the
  machine and network equipment powered during outages (inverter backup output or UPS).

## Installation

```bash
git clone https://github.com/rafabolivar/deyeconfig.git
cd deyeconfig
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml`: at least `[logger] ip` and `serial`, and for the automatic mode
`[storm] latitude` and `longitude`. See the [Configuration reference](#configuration-reference).

Then check the connection:

```bash
.venv/bin/python deye_test_connection.py
```

## Project structure

```
deye_test_connection.py   Connection test (read-only)
deye_read_config.py       Show the full configuration and save a backup (read-only)
deye_apply.py             Apply a profile (dry run unless --apply)
deye_export.py            Export the current configuration as a profile (read-only)
deye_optimizer.py         Automatic mode: price optimizer with storm and outage protection
deye_common.py            Shared: configuration loading, connection, register reads
deye_map.py               Shared: register map handling and value conversions
deye_tariff.py            Shared: tariff periods (fallback prices), weekends and holidays
maps/                     Register maps per inverter model (TOML)
profiles/examples/        Example profiles (afternoon, autumn, winter, summer)
profiles/                 Your own profiles (ignored by git)
systemd/                  systemd unit for the automatic mode service
config.example.toml       Configuration template
config.toml               Your configuration (ignored by git)
backups/                  JSON backups of the inverter registers (ignored by git)
state/                    Automatic mode state, price cache and plans (ignored by git)
```

## Connection test

```bash
.venv/bin/python deye_test_connection.py
```

Reads the inverter serial number, battery SOC and battery voltage. Read-only. If the
inverter serial number matches the label on your inverter, the connection works.

## Reading the inverter configuration

```bash
.venv/bin/python deye_read_config.py          # show everything and save a backup
.venv/bin/python deye_read_config.py --raw    # also show id, register and raw value
.venv/bin/python deye_read_config.py --no-backup
```

Shows the whole configuration and saves a JSON backup with all raw registers of the
configuration range in `backups/`. Read-only.

`--raw` shows, for each parameter, the `id` to use in profiles, whether it is
read-only, its register number and raw value.

## Profiles

A profile (`profiles/*.toml`) lists only the parameters to change, in normal units
(A, %, V, "HH:MM", true/false). Everything else is left untouched.

```bash
.venv/bin/python deye_apply.py profiles/winter.toml                # show what would change
.venv/bin/python deye_apply.py profiles/winter.toml --show         # same, explicitly
.venv/bin/python deye_apply.py profiles/winter.toml --apply        # write (asks for confirmation)
.venv/bin/python deye_apply.py profiles/winter.toml --apply --yes  # no confirmation (cron)
```

Nothing is ever written without `--apply`. With `--apply`:

1. The whole profile is validated: parameters exist, are writable and in range,
   times are valid and slot start times are increasing. If there is any error,
   nothing is written.
2. A backup of the current configuration is saved in `backups/`.
3. Only the registers that change are written, in contiguous blocks (the Time Of
   Use table in one operation), and they are read back a few seconds later to verify.

### Profile format

```toml
description = "Winter: grid charging during off-peak hours up to 80 %"

[parameters]
grid_charge = true
grid_charge_current = 40
time_of_use = "MTWTFSS"

[[tou]]
slot = 1
time = "00:00"
power = 6000
soc = 80
grid_charge = true

[[tou]]
slot = 2
time = "08:00"
soc = 20
grid_charge = false
```

- `description`: optional, shown when applying the profile.
- `[parameters]`: any writable parameter of the map, by its `id`
  (list them with `deye_read_config.py --raw`). Value types:
  - numbers in the parameter's unit (`grid_charge_current = 40`, `shutdown_voltage = 46.5`)
  - option names for lists (`work_mode = "Zero export to load"`)
  - `true` / `false` for switches
  - `time_of_use`: `true`, `false` or the active days as 7 characters, e.g.
    `"MTWTF--"` for Monday to Friday
- `[[tou]]`: Time Of Use slots (1 to 6). Each entry needs `slot` and any of
  `time` ("HH:MM", start of the slot), `power` (W), `voltage` (V), `soc` (%),
  `grid_charge` and `gen_charge` (true/false). Each slot runs until the next
  slot's start time; slot 6 runs until slot 1 of the next day.

Notes:

- Some parameters are read-only for safety (grid protections, battery type,
  charging voltages...). This is controlled by `writable` in the map.
- The charge register of each slot has other bits besides grid and generator
  charging (some firmwares use bit 2). They are shown as "other bits" and always
  preserved when writing.
- `profiles/examples/` contains examples aligned with the Spanish 2.0TD tariff
  periods (see [Seasonal profiles](#seasonal-profiles)). Copy them to `profiles/` and adjust them: files directly in `profiles/`
  are excluded from the repository, so your personal settings are never committed.

### Seasonal profiles

Examples for sunny days in autumn and winter (`profiles/examples/autumn.toml` and
`winter.toml`):

| Slot | Start | Period | SOC | Grid charge | Effect |
|---|---|---|---|---|---|
| 1 | 00:00 | Off-peak | 60 % autumn / 70 % winter | Yes | Charge at off-peak price; the battery does not go below this level at night |
| 2 | 08:00 | Mid | 15 % | No | Battery available to start the day |
| 3 | 10:00 | Peak | 15 % | No | Battery available; the sun charges it |
| 4 | 14:00 | Mid | 15 % | No | Battery available |
| 5 | 18:00 | Peak | 15 % | No | Battery available for the evening peak |
| 6 | 22:00 | Mid | 15 % | No | Battery available |

- The night charge gives battery for the first hours of the day, when the sun is
  still weak and grid energy is expensive, and leaves room for the solar surplus:
  charging more would fill the battery with grid energy and the surplus would be
  exported at a low price. Autumn has more sun, so it charges less.
- At night the house uses cheap off-peak grid energy instead of the battery.
- Gentle grid charge at 25 A (about 4-4.5 h from 15 %), and grid draw limited to 4000 W
  (peak shaving) to stay below the contracted power.
- Battery current limits below the BMS protection thresholds (100 A for 3 s when
  charging, 100 A for 30 s when discharging): 90 A charge, 95 A discharge. Above the
  BMS threshold the battery would disconnect, which during an outage would leave the
  house without power. Check your own BMS values before copying them.
- These profiles are for manual use. While the automatic mode service runs, it
  manages the Time Of Use slots itself and overwrites them at the next plan.

`profiles/examples/summer.toml` is an earlier example without grid charging.

## Exporting the current configuration

```bash
.venv/bin/python deye_export.py                            # print the profile to the screen
.venv/bin/python deye_export.py -o profiles/current.toml   # save it to a file
.venv/bin/python deye_export.py -o profiles/current.toml --force   # overwrite if it exists
```

Creates a profile with the inverter's current values: all writable parameters and
the 6 Time Of Use slots, ready to be applied with `deye_apply.py`. Read-only
parameters are included as comments. The `.toml` extension is added automatically
if missing. Read-only.

Good practice: export your configuration before experimenting
(`deye_export.py -o profiles/original.toml`), so you can always go back to it.

## Automatic mode

`deye_optimizer.py` is a service that manages the battery on its own: it plans
grid charging from the **real hourly PVPC prices**, the solar forecast, the
expected consumption and the SOC, protects the house against storms and outages,
and writes the Time Of Use slots to the inverter.

### Why real prices

The fixed tariff periods (off-peak, mid, peak) only set part of the price (tolls
and charges). The hourly PVPC price also includes the wholesale market price,
which drops at midday because of solar generation. In September-October 2026 the
cheapest 3 hours of the day were at midday or in the afternoon on 27 of 31 days
(about 0.06 EUR/kWh at 14-17 h, versus 0.18 at night and 0.33 at 19-21 h). Prices
come from REData, Red Eléctrica's public API (no key needed).

### How it works

- **Every `interval` seconds (900), full plan:**
  1. Reads the SOC, battery capacity and voltage.
  2. Gets the PVPC prices for today and tomorrow (tomorrow's are published around
     20:15; until then the previous day's prices are used as an estimate, and the
     `[tariff]` fallback prices if there is nothing else; missing or invalid prices
     are requested again in every cycle, and a warning is logged if tomorrow's are
     still missing at `price_warning_hour`) and, in one Open-Meteo
     call, the solar forecast on the plane of the panels and the storm forecast.
  3. **Storm protection**: compares several weather models to rate each storm hour
     (see [Storm confirmation](#storm-confirmation)). From `reserve_lead_hours`
     (0.5) before it until `grace_hours` (1) after it, the battery must stay at or
     above `reserve_soc` (80 %, high confidence) or `reserve_soc_medium` (50 %,
     medium confidence). Being below has a high cost in the calculation
     (`shortfall_penalty`), so the plan charges in the cheapest hours before the
     storm, and in peak hours only as much as needed if there is no other way.
  4. Finds the optimal plan by **linear programming** (scipy, HiGHS): the hours to
     **charge** from the grid and the hours to **keep** the battery (the house uses
     the grid while it is cheap) that minimise the total cost, including battery
     losses, a wear cost per kWh and the value of the energy left at the end.
     Solar charging comes first; the grid only completes what is worth buying.
  5. Builds the 6 Time Of Use slots and writes them only if they differ from the
     inverter.
- **Today's solar forecast is corrected with the measured production**: once at
  least `pv_correction_min_hours` (2) daylight hours have been measured, the rest of
  the day's forecast is scaled by the ratio measured / forecast, so the plan does not
  buy energy the sun is going to provide anyway.
- **Real-time grid charge control** (every minute during a charge slot): the grid
  charge current is set to what the sun will not provide, from the energy still
  needed to reach the slot's target SOC, the time left in the slot and the measured
  solar surplus. The target is reached at the end of the slot instead of filling the
  battery early and exporting the solar production that comes after. The current
  changes in steps of 5 A, at most every `charge_control_minutes` (10), up to
  `max_grid_charge_current`. At equal price the plan also prefers charging later.
- **Every `grid_check_interval` seconds (60), outage protection:** if the grid goes
  down, all slots are lowered to `outage_soc` (15 %) at once, so the whole battery
  is available. When the grid returns, the plan is recalculated immediately.

### Storm confirmation

A single weather model often predicts a "thunderstorm" for what turns out to be a
weak shower. To avoid paying for false alarms, the service compares several models
(`models`: ECMWF, ICON, Météo-France, GFS and UKMO) and looks, around each hour, at:

- **thunderstorm**: weather code in `storm_codes` (WMO 95, 96, 99);
- **heavy rain**: at least `heavy_rain_mm` (10 mm) in the hour;
- **CAPE**: convective energy (J/kg), the "fuel" of thunderstorms.

| Confidence | Condition | Reserve |
|---|---|---|
| High | `min_models` (2) models agree on thunderstorm or on heavy rain, or a thunderstorm with CAPE >= `cape_high` (1000) | `reserve_soc` (80 %) |
| Medium | One model with thunderstorm and CAPE >= `cape_medium` (500), or heavy rain in one model | `reserve_soc_medium` (50 %) |
| Low | Anything else (drizzle, weak showers) | None |

### Time Of Use slots

Each hour of the plan ends up as one of three actions:

| Action | Slot | Effect |
|---|---|---|
| Charge | Grid charge on, SOC = target | Solar first, the grid completes up to the target |
| Keep | Grid charge off, SOC = current level | The battery is not used; the house uses the grid while it is cheap |
| Use | Grid charge off, SOC = minimum (15 %, or the storm reserve) | The battery supplies the house |

The 24 hours are grouped into the 6 slots of the inverter, joining the
neighbouring hours that damage the plan least (charging hours are never moved).
Example plan for a normal October day: charge at 15:00-17:00 (the cheapest hours)
up to 100 %, battery available the rest of the day.

### Writes to the inverter

Slot SOC values are rounded up to `soc_step` (5 %), so small changes in the plan
do not rewrite the inverter. Registers are written in contiguous blocks and read
back to verify them, and every write is preceded by a backup in `backups/`.

To protect the inverter's memory against a malfunction, writes are limited:

| Write | Normal | Test mode |
|---|---|---|
| Plan changes | `plan_writes_per_hour` (4, one per cycle), `plan_writes_per_day` (30) | No hourly limit, `test_writes_per_day` (100) |
| Real-time charge control (current only) | `charge_writes_per_hour` (4), `charge_writes_per_day` (40) | Same |
| Outage protection, and plans with a storm reserve in the next `storm_priority_hours` (6) | Never limited | Never limited |

**Loop detection**: if the same change is written `loop_max_repeats` (3) times in a
row within `loop_window_hours` (2), the inverter is reverting it, and the service
stops insisting and logs a warning. Only consecutive repeats count, so several
outages in a row are never blocked.

During development, `--test-mode` relaxes the plan limits for a while; it expires
on its own and the running service picks it up within a minute:

```bash
.venv/bin/python deye_optimizer.py --test-mode 4h       # or 30m
.venv/bin/python deye_optimizer.py --test-mode off
.venv/bin/python deye_optimizer.py --reset-write-count  # if a limit was reached during tests
``` The battery current limits (`max_charge_current`, 90 A;
`max_discharge_current`, 95 A) and the grid power limit (`grid_power_limit`,
4000 W, inverter peak shaving) are written with every plan.

### Running it

```bash
.venv/bin/python deye_optimizer.py                    # single plan, dry run
.venv/bin/python deye_optimizer.py --apply            # single plan, write it
.venv/bin/python deye_optimizer.py --daemon --apply   # service (used by systemd)
.venv/bin/python deye_optimizer.py --assume-storm 6   # dry run with a storm in 6 h (testing)
.venv/bin/python deye_optimizer.py --assume-outage    # dry run with the grid down (testing)
```

The latest plan is saved in `state/optimizer_plan.json` and every plan is logged in
`state/optimizer_log.csv`.

### Installing the service

Adjust `User` and the paths in `systemd/deye-optimizer.service` if needed, then:

```bash
sudo cp systemd/deye-optimizer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deye-optimizer.service
```

Useful commands:

```bash
systemctl status deye-optimizer.service             # is it running?
journalctl -u deye-optimizer.service -f             # follow the log live (Ctrl+C to exit)
journalctl -u deye-optimizer.service --since today  # what it did today
sudo systemctl restart deye-optimizer.service       # after changing config.toml or the code
sudo systemctl stop deye-optimizer.service          # stop it (manual profiles take over)
```

While the service runs it manages the Time Of Use slots, so profiles applied by
hand are overwritten at the next plan. Stop the service to use manual profiles.

**Requirement for outage protection:** the machine running the service and the
network equipment must stay powered during an outage (e.g. connected to the
inverter's backup output or a UPS). The logger is powered by the inverter.

## Configuration reference

`config.toml` (copy of `config.example.toml`, never committed):

### `[logger]`

| Key | Default | Description |
|---|---|---|
| `ip` | | IP address of the Solarman logger |
| `serial` | | Serial number of the logger (number, without quotes) |
| `port` | 8899 | Modbus TCP port of the logger |

### `[modbus]`

| Key | Default | Description |
|---|---|---|
| `slave_id` | 1 | Modbus slave ID of the inverter |
| `timeout` | 10 | Connection timeout (s) |

### `[inverter]`

| Key | Default | Description |
|---|---|---|
| `map` | `maps/deye_sg0xlp1.toml` | Register map for your inverter model |

### `[storm]`

| Key | Default | Description |
|---|---|---|
| `latitude`, `longitude` | | Location for the forecasts (decimal degrees) |
| `timezone` | `Europe/Madrid` | Local time zone |
| `enabled` | true | Storm protection on or off (price optimization always runs) |
| `grid_check_interval` | 60 | Seconds between grid checks (outage detection) |
| `models` | ECMWF, ICON, Météo-France, GFS, UKMO | Weather models compared to confirm a storm |
| `storm_codes` | [95, 96, 99] | WMO weather codes considered a thunderstorm |
| `heavy_rain_mm` | 10.0 | Rain considered very heavy (mm in one hour) |
| `min_models` | 2 | Models that must agree for high confidence |
| `cape_high`, `cape_medium` | 1000, 500 | CAPE (J/kg) for high / medium confidence with a single model |
| `reserve_soc` | 80 | Minimum battery level around a high-confidence storm (%) |
| `reserve_soc_medium` | 50 | Minimum battery level around a medium-confidence storm (%) |
| `reserve_lead_hours` | 0.5 | The reserve applies from this many hours before each storm hour |
| `grace_hours` | 1 | ... until this many hours after it |
| `shortfall_penalty` | 2.0 | Cost (EUR per kWh and hour) of being below the reserve |
| `outage_soc` | 15 | Battery level allowed while the grid is down (%) |

### `[tariff]`

Fallback only, used when the real PVPC prices are not available.

| Key | Default | Description |
|---|---|---|
| `fallback_prices` | off-peak 0.18, mid 0.15, peak 0.25 | Representative price of each period (EUR/kWh) |
| `periods` | Spanish 2.0TD | Weekday periods: `["HH:MM", "off-peak" \| "mid" \| "peak"]`, first at 00:00, at most 6 |
| `weekend_off_peak` | true | Saturdays and Sundays are off-peak all day |
| `holidays` | Spanish fixed national holidays | Off-peak days: `"MM-DD"` (every year) or `"YYYY-MM-DD"` |

### `[optimizer]`

| Key | Default | Description |
|---|---|---|
| `interval` | 900 | Seconds between plans |
| `pv_kwp` | 3.535 | Solar peak power (kWp) |
| `pv_performance` | 0.54 | Actual production / theoretical from the irradiance on the panels |
| `panel_tilt`, `panel_azimuth` | 35, -45 | Panel tilt (degrees) and azimuth (0 = south, -90 = east) |
| `min_soc`, `max_soc` | 15, 100 | Battery range used by the plan (%) |
| `charge_efficiency`, `discharge_efficiency` | 0.95, 0.95 | Battery efficiencies |
| `cycle_cost` | 0.01 | Battery wear cost per kWh discharged (EUR) |
| `export_price` | 0.04 | Price paid for exported energy (EUR/kWh) |
| `grid_power_limit` | 4000 | Maximum grid draw (W, inverter peak shaving) |
| `max_grid_charge_current` | 65 | Maximum grid charge current (A) |
| `max_charge_current`, `max_discharge_current` | 90, 95 | Battery current limits (A) |
| `horizon_hours` | 36 | Hours ahead to plan (limited by the published prices) |
| `soc_step` | 5 | Slot SOC values are rounded up to this step (%) |
| `plan_writes_per_hour`, `plan_writes_per_day` | 4, 30 | Plan write limits |
| `charge_writes_per_hour`, `charge_writes_per_day` | 4, 40 | Charge control write limits |
| `test_writes_per_day` | 100 | Plan writes per day in test mode (no hourly limit) |
| `storm_priority_hours` | 6 | Plans with a storm reserve within these hours are never limited |
| `loop_max_repeats`, `loop_window_hours` | 3, 2 | Loop detection: same change in a row this many times within these hours |
| `charge_control_minutes` | 10 | Minimum minutes between grid charge current changes |
| `late_charge_preference` | 0.0001 | Preference (EUR/kWh per hour) for charging later at equal price |
| `pv_correction_min_hours` | 2 | Measured daylight hours needed to correct today's solar forecast |
| `price_warning_hour` | 23 | Hour from which a warning is logged if tomorrow's prices are still missing |
| `load_profile` | about 12.7 kWh/day | Expected consumption (kW) for each hour, 00 to 23 |

Location and time zone are taken from `[storm]` unless set in `[optimizer]`.

## Backups, state and rolling back

- **`backups/`**: JSON files with all raw configuration registers, created by
  `deye_read_config.py` and before every write by `deye_apply.py` and
  `deye_optimizer.py`. Each file records the date and the reason.
- **`state/optimizer.json`**: automatic mode state (outage, daily write count).
- **`state/prices.json`**: cache of the PVPC prices of the last days.
- **`state/optimizer_plan.json`** and **`state/optimizer_log.csv`**: latest plan
  and history of plans.

To go back to a known configuration manually:

```bash
sudo systemctl stop deye-optimizer.service                # otherwise it rewrites the slots
.venv/bin/python deye_apply.py profiles/original.toml --apply
```

Start the service again (`sudo systemctl start deye-optimizer.service`) to go back
to automatic mode.

## Register maps

The registers of each model are defined in `maps/` (TOML), separate from the code.
The map is selected in `config.toml` (`[inverter] map = ...`). To add parameters or
support another model, edit or create a map. The file header documents the format
(`group`, `id`, `writable`, `min`/`max`, `reg`, `name`, `type`, `scale`, `unit`,
`options`, `bit`).

| Map | Models | Tested with |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye single-phase LV hybrid (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

The live data registers used by the automatic mode (battery voltage 183, SOC 184,
grid status 194, battery capacity 204) are defined in `deye_optimizer.py`.

Before writing to a model that has not been tested, compare `deye_read_config.py`
with the inverter's screen or app: older single-phase models use a different
register layout.

## Troubleshooting

- **Connection timeout**: check the logger IP, that port 8899 answers
  (`nc -zv <ip> 8899`) and that the machine is on the same network. Some logger
  firmware versions disable this port.
- **Connection opens but reads fail**: the serial number in `config.toml` must be
  the logger's, not the inverter's.
- **Values that make no sense** (times like 00:92, huge currents): the register map
  does not match your model or firmware. Do not write; check the map.
- **The automatic mode does something unexpected**: run `deye_optimizer.py`
  without options to see the current plan hour by hour (dry run), and check the log
  with `journalctl -u deye-optimizer.service`.
- **Manual profile changes are undone**: the automatic mode service manages the
  Time Of Use slots. Stop it to use manual profiles.
- **"daily write limit reached"**: something is changing values repeatedly. Check
  the log before raising `max_writes_per_day`.

## Roadmap

- Learn the consumption profile from the inverter history instead of a fixed
  configuration value.
- Meteocat (Catalan weather service) as an additional source: lightning detection
  (XDDE), measured rain at nearby stations (XEMA) and its own forecast, to confirm
  storms that are actually happening.

## Credits

- Register information: Deye Modbus protocol V118 and the `deye_hybrid.yaml`
  definition from [ha-solarman](https://github.com/davidrapan/ha-solarman).
- Logger communication: [pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5).
- Weather forecast: [Open-Meteo](https://open-meteo.com/).

## Disclaimer

Writing wrong values to an inverter can affect its operation or the battery.
Use at your own risk: always run without `--apply` first, check the changes, and
keep a backup of your configuration (`deye_export.py`).

