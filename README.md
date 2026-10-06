# Deyeconfig

**English** | [Español](README.es.md)

Read and change the configuration of Deye hybrid inverters through the Solarman
data logger on your local network (Modbus TCP, port 8899), without relying on the
cloud. Includes a storm mode service that pre-charges the battery from the grid
when thunderstorms are forecast, so the house is ready for a power outage.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Project structure](#project-structure)
- [Connection test](#connection-test)
- [Reading the inverter configuration](#reading-the-inverter-configuration)
- [Profiles](#profiles)
- [Exporting the current configuration](#exporting-the-current-configuration)
- [Storm mode](#storm-mode)
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
- **Storm mode service**: when storms are forecast, charges the battery from the
  grid in the cheapest tariff periods, keeps a reserve during the storm and makes the
  whole battery available during an outage.
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
- For storm mode: Internet access (weather forecast) and, for outage protection, the
  machine and network equipment powered during outages (inverter backup output or UPS).

## Installation

```bash
git clone https://github.com/rafabolivar/deyeconfig.git
cd deyeconfig
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml`: at least `[logger] ip` and `serial`, and for storm mode
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
deye_storm.py             Storm mode (single check or service)
deye_common.py            Shared: configuration loading, connection, register reads
deye_map.py               Shared: register map handling and value conversions
deye_tariff.py            Shared: tariff periods (off-peak, mid, peak), weekends and holidays
maps/                     Register maps per inverter model (TOML)
profiles/examples/        Example profiles (autumn, winter, summer)
profiles/                 Your own profiles (ignored by git)
systemd/                  systemd unit for the storm mode service
config.example.toml       Configuration template
config.toml               Your configuration (ignored by git)
backups/                  JSON backups of the inverter registers (ignored by git)
state/                    Storm mode state (ignored by git)
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
| 1 | 00:00 | Off-peak | 50 % autumn / 60 % winter | Yes | Charge at off-peak price; the battery does not go below this level at night |
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
- Gentle grid charge at 25 A (about 3.5 h), and grid draw limited to 4000 W
  (peak shaving) to stay below the contracted power.
- The storm mode service is compatible with these profiles: it saves the
  configuration when it starts and restores it when it ends.

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

## Storm mode

`deye_storm.py` is a service that keeps the battery charged when thunderstorms or
very heavy rain are forecast ([Open-Meteo](https://open-meteo.com/), free, no API
key), so the house is ready for a possible power outage. It charges from the grid
in the cheapest tariff periods and never in peak hours, except in an emergency.

Outside storm mode the service only checks the forecast and never touches the
inverter. All values are configurable in the `[storm]` and `[tariff]` sections of
`config.toml` (defaults in brackets).

### When storm mode starts

1. **Off-peak period**: a storm is forecast within the next `forecast_hours` (24).
   This is the main case: the battery is charged at off-peak prices, even for a
   storm expected in the afternoon.
2. **Storm happening now** (the forecast for the current hour meets the criteria),
   at any time.
3. **Backup**, mid period: a storm is forecast, the SOC is below
   `backup_trigger_soc` (50 %) and this is the last cheap period (off-peak or mid)
   before the storm. Example: storm at 21:00 on a weekday, storm mode starts at
   14:00 to use the whole 14-18 mid period before the 18-22 peak.
4. **Emergency**, peak period: a storm is happening or due within
   `emergency_hours` (2) and the SOC is below `emergency_soc` (50 %).

### Time Of Use layout in storm mode

The 6 Time Of Use slots follow the tariff periods. With the default Spanish 2.0TD
tariff on a weekday:

| Slot | Start | Period | SOC | Grid charge | Effect |
|---|---|---|---|---|---|
| 1 | 00:00 | Off-peak | 100 % | Yes | Charge to the maximum at the cheapest price |
| 2 | 08:00 | Mid | 80 % | Yes | Only charges if the battery drops below 80 % |
| 3 | 10:00 | Peak | 80 % | No | Never charges; the battery does not go below 80 % |
| 4 | 14:00 | Mid | 80 % | Yes | Only charges if below 80 % |
| 5 | 18:00 | Peak | 80 % | No | Never charges; keeps the reserve |
| 6 | 22:00 | Mid | 80 % | Yes | Only charges if below 80 % |

- `charge_soc` (100 %): charge level in off-peak periods.
- `hold_soc` (80 %): reserve kept until the storm has passed. The battery can be
  used from 100 % down to 80 % in the morning and in peak hours, when grid energy
  is most expensive, while the reserve for the outage stays intact.
- **Weekends and holidays** are off-peak all day: every slot charges to 100 %.
  The inverter uses the same table every day, so the service rewrites it when the
  type of day changes (only in storm mode).
- **Emergency charge** in a peak period: the peak slots charge up to
  `emergency_soc` (50 %) until that peak period ends. The rest is completed in the
  next mid or off-peak period.
- **Charge current**: the grid charge current is set to `charge_current` (40 A)
  to charge quickly, even if your daily profile uses a lower one.
- **Grid power limit**: the inverter's grid peak shaving is set to
  `grid_power_limit` (4000 W) so that the battery charge plus the house load never
  exceed the contracted power; the inverter reduces the battery charge first.
- **Outage protection**: the grid status is checked every `grid_check_interval`
  seconds (60). If the grid goes down, all slots are lowered to `outage_soc` (15 %)
  so the whole battery is available, whatever the inverter does with the slots in
  off-grid mode. When the grid returns, the layout above is resumed.

### When storm mode ends

The configuration saved when storm mode started (`state/pre_storm.toml`) is restored:

- `grace_hours` (1) after the last forecast storm hour, if no more storms are forecast;
- if the storm is no longer forecast (two checks in a row);
- outside an off-peak period, if the next storm comes after the next off-peak
  period (it will be handled then, charging at off-peak prices);
- after `max_hours` (36), as a safety limit.

What counts as a storm: weather codes in `storm_codes` (WMO 95, 96, 99 =
thunderstorm) or rain of at least `heavy_rain_mm` (10 mm/h) with a probability of
at least `min_probability` (50 %).

### Writes to the inverter

The service compares the desired configuration with the inverter on every check
and only writes when something has to change (typically when storm mode starts,
at tariff period changes that need it, on grid loss and return, and when it ends).
Registers are written in contiguous blocks (the whole Time Of Use table in one
operation) and read back a few seconds later to verify them. Every write is
preceded by a backup in `backups/`, and there is a daily limit
(`max_writes_per_day`, 20). If the inverter changes a value on its own, the next
check corrects it.

### Running it

```bash
.venv/bin/python deye_storm.py                    # single check, show what it would do
.venv/bin/python deye_storm.py --apply            # single check, act on the inverter
.venv/bin/python deye_storm.py --daemon --apply   # run as a service (used by systemd)
```

Testing options (single dry run only, nothing is written or saved):

```bash
.venv/bin/python deye_storm.py --assume-storm 6                           # pretend a storm in 6 h
.venv/bin/python deye_storm.py --assume-outage --assume-storm 0           # storm now and grid down
.venv/bin/python deye_storm.py --at '2026-10-07 14:30' --assume-storm 6.5 # pretend it is 14:30
```

### Installing the service

Adjust `User` and the paths in `systemd/deye-storm.service` if needed, then:

```bash
sudo cp systemd/deye-storm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deye-storm.service
```

Useful commands:

```bash
systemctl status deye-storm.service                     # is it running?
journalctl -u deye-storm.service -f                     # follow the log live (Ctrl+C to exit)
journalctl -u deye-storm.service --since today          # what it did today
sudo systemctl restart deye-storm.service               # after changing config.toml or the code
sudo systemctl stop deye-storm.service                  # stop it (state is kept)
```

The state is saved in `state/`, so the service resumes where it was after a restart
or a reboot. After changing `config.toml`, restart the service.

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
| `latitude`, `longitude` | | Location for the weather forecast (decimal degrees) |
| `timezone` | `Europe/Madrid` | Local time zone |
| `forecast_interval` | 900 | Seconds between forecast checks (also at every tariff period change) |
| `grid_check_interval` | 60 | Seconds between grid checks in storm mode |
| `forecast_hours` | 24 | Hours ahead to look for storms |
| `charge_soc` | 100 | Grid charge level in off-peak periods (%) |
| `hold_soc` | 80 | Reserve kept in storm mode; mid periods only charge up to here (%) |
| `outage_soc` | 15 | Minimum SOC while the grid is down in storm mode (%) |
| `backup_enabled` | true | Enable the backup trigger in mid periods |
| `backup_trigger_soc` | 50 | Backup: SOC below this (%) |
| `emergency_soc` | 50 | Emergency in peak periods: SOC below this, charge up to this (%) |
| `emergency_hours` | 2 | Emergency: storm happening or due within this many hours |
| `grid_power_limit` | 4000 | Maximum grid draw in storm mode (W, inverter peak shaving); 0 = do not change |
| `charge_current` | 40 | Grid charge current in storm mode (A); 0 = do not change |
| `storm_codes` | [95, 96, 99] | WMO weather codes considered a thunderstorm |
| `heavy_rain_mm` | 10.0 | Rain considered very heavy (mm/h) |
| `min_probability` | 50 | Minimum probability for heavy rain to count (%) |
| `grace_hours` | 1 | Restore this many hours after the last storm hour |
| `max_hours` | 36 | Safety limit: maximum hours in storm mode |
| `max_writes_per_day` | 20 | Safety limit of inverter writes per day |

### `[tariff]`

| Key | Default | Description |
|---|---|---|
| `periods` | Spanish 2.0TD | Weekday periods: `["HH:MM", "off-peak" \| "mid" \| "peak"]`, first at 00:00, at most 6 |
| `weekend_off_peak` | true | Saturdays and Sundays are off-peak all day |
| `holidays` | Spanish fixed national holidays | Off-peak days: `"MM-DD"` (every year) or `"YYYY-MM-DD"` |

Default 2.0TD periods on weekdays: 00-08 off-peak (valle), 08-10 mid (llano),
10-14 peak (punta), 14-18 mid, 18-22 peak, 22-24 mid. In 2.0TD only national
holidays count as off-peak, not regional or local ones.

## Backups, state and rolling back

- **`backups/`**: JSON files with all raw configuration registers, created by
  `deye_read_config.py` and before every write by `deye_apply.py` and
  `deye_storm.py`. Each file records the date and the reason.
- **`state/storm.json`**: storm mode state (active or not, storm end, emergency
  charge, outage, daily write count).
- **`state/pre_storm.toml`**: profile with the configuration saved when storm mode
  started; it is restored when storm mode ends.

To go back to a known configuration manually:

```bash
sudo systemctl stop deye-storm.service                    # if storm mode is running
.venv/bin/python deye_apply.py profiles/original.toml --apply
```

If storm mode was active, `state/pre_storm.toml` is the configuration you had
before it started and can be applied the same way. Delete `state/storm.json`
before starting the service again so it starts in normal mode.

## Register maps

The registers of each model are defined in `maps/` (TOML), separate from the code.
The map is selected in `config.toml` (`[inverter] map = ...`). To add parameters or
support another model, edit or create a map. The file header documents the format
(`group`, `id`, `writable`, `min`/`max`, `reg`, `name`, `type`, `scale`, `unit`,
`options`, `bit`).

| Map | Models | Tested with |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye single-phase LV hybrid (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

The live data registers used by storm mode (battery voltage 183, SOC 184, grid
status 194) are defined in `deye_storm.py`.

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
- **Storm mode does nothing**: run `deye_storm.py` without options to see the
  current decision, and check the log with `journalctl -u deye-storm.service`.
- **"daily write limit reached"**: something is changing values repeatedly. Check
  the log before raising `max_writes_per_day`.

## Roadmap

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

