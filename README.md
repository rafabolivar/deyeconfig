# deyeconfig

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
- **Storm mode service**: plans an off-peak grid charge when storms are forecast,
  keeps the battery charged during the storm and makes the whole battery available
  during an outage.
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
maps/                     Register maps per inverter model (TOML)
profiles/examples/        Example profiles (winter, summer)
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
3. Only the registers that change are written, and they are read back to verify.

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
  periods. Copy them to `profiles/` and adjust them: files directly in `profiles/`
  are excluded from the repository, so your personal settings are never committed.

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

`deye_storm.py` pre-charges the battery from the grid when thunderstorms or very
heavy rain are forecast ([Open-Meteo](https://open-meteo.com/), free, no API key),
so the house is ready for a possible power outage. It runs as a service.

### How it works

All values are configurable in the `[storm]` section of `config.toml` (defaults in
brackets).

1. **Planning window** (`planning_time` to `ready_by`, 00:00-07:30, the off-peak
   period): on every forecast check, if a storm is forecast within the next
   `forecast_hours` (24), it calculates how long it takes to charge from the current
   SOC to `target_soc` (80 %) and schedules the grid charge to finish at `ready_by`,
   or `storm_margin_minutes` (30) before the storm if it comes earlier. If there is
   not enough time, it charges immediately.
2. **Storm happening now** (the forecast for the current hour meets the criteria):
   charges immediately while the grid is available.
3. **Backup** outside the planning window: if a storm is forecast within
   `backup_hours` (4) and the SOC is below `backup_trigger_soc` (50 %), charges
   immediately.
4. **Holding**: while in storm mode, `target_soc` is kept as the minimum battery level.
5. **Next storm**: if the storm the charge was planned for passes and another one is
   forecast, it stops charging and plans the charge for the next storm. The plan is
   only recalculated when the target storm changes, so charging does not switch on
   and off as the SOC rises.
6. **Outage protection**: in storm mode the grid status is checked every
   `grid_check_interval` seconds (60). If the grid goes down, all Time Of Use slots
   are lowered to `outage_soc` (15 %) so the whole battery is available, whatever
   the inverter does with the slots in off-grid mode. When the grid returns, the
   battery is recharged to `target_soc` (`recharge_after_outage`).
7. **End**: `grace_hours` (1) after the last forecast storm hour, if the storm is no
   longer forecast (two checks in a row), or after `max_hours` (36) as a safety
   limit, the configuration saved when storm mode started is restored.

What counts as a storm: weather codes in `storm_codes` (WMO 95, 96, 99 =
thunderstorm) or rain of at least `heavy_rain_mm` (10 mm/h) with a probability of
at least `min_probability` (50 %).

The charging time is estimated from the battery capacity (read from the inverter),
`battery_nominal_voltage`, the grid charge current configured in the inverter, the
battery voltage and `charge_efficiency`. If the estimate falls short, the charge
simply continues a little after `ready_by` (the off-peak period lasts until 08:00).

### Time Of Use layout

The service calculates the Time Of Use slots itself. Example: battery at 15 %,
storm forecast at 18:00, planning at 00:05:

| Slot | Start | SOC | Grid charge | Purpose |
|---|---|---|---|---|
| 1 | 00:00 | 15 % | No | Normal use until charging starts |
| 2 | 03:55 | 80 % | Yes | Charge to 80 % by 07:30 |
| 3-6 | 08:00, 12:00, 16:00, 20:00 | 80 % | Yes | Keep 80 % until the storm has passed |

Slot 1 uses the minimum SOC of your normal configuration. Once charging has
started, all slots are set to 80 % with grid charging.

### Writes to the inverter

The service compares the desired configuration with the inverter on every check
and only writes when something has to change (typically when storm mode starts,
on grid loss and return, and when it ends). Every write is preceded by a backup
in `backups/` and verified, and there is a daily limit (`max_writes_per_day`, 20).
If the inverter changes a value on its own, the next check corrects it.

### Running it

```bash
.venv/bin/python deye_storm.py                    # single check, show what it would do
.venv/bin/python deye_storm.py --apply            # single check, act on the inverter
.venv/bin/python deye_storm.py --daemon --apply   # run as a service (used by systemd)
.venv/bin/python deye_storm.py --plan-now         # run the planning logic now
```

Testing options (single dry run only, nothing is written or saved):

```bash
.venv/bin/python deye_storm.py --assume-storm 18                          # pretend a storm in 18 h
.venv/bin/python deye_storm.py --assume-outage                            # pretend the grid is down
.venv/bin/python deye_storm.py --at '2026-10-08 00:05' --assume-storm 18  # pretend it is 00:05
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
journalctl -u deye-storm.service --since "today 00:00"  # what it did tonight
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
| `forecast_interval` | 900 | Seconds between forecast checks |
| `grid_check_interval` | 60 | Seconds between grid checks in storm mode |
| `planning_time` | `"00:00"` | Start of the planning window (off-peak start) |
| `ready_by` | `"07:30"` | End of the planning window; the charge must finish by then |
| `forecast_hours` | 24 | Hours ahead to look for storms |
| `storm_margin_minutes` | 30 | Finish charging this long before the storm, if it comes before `ready_by` |
| `target_soc` | 80 | SOC to charge to and hold in storm mode (%) |
| `outage_soc` | 15 | Minimum SOC while the grid is down in storm mode (%) |
| `recharge_after_outage` | true | Recharge as soon as the grid returns (false: wait for the next planning window) |
| `backup_enabled` | true | Enable the backup trigger outside the planning window |
| `backup_hours` | 4 | Backup: storm within this many hours |
| `backup_trigger_soc` | 50 | Backup: SOC below this (%) |
| `storm_codes` | [95, 96, 99] | WMO weather codes considered a thunderstorm |
| `heavy_rain_mm` | 10.0 | Rain considered very heavy (mm/h) |
| `min_probability` | 50 | Minimum probability for heavy rain to count (%) |
| `grace_hours` | 1 | Restore this many hours after the last storm hour |
| `max_hours` | 36 | Safety limit: maximum hours in storm mode |
| `battery_nominal_voltage` | 51.2 | Used to estimate the charging time |
| `charge_efficiency` | 0.9 | Used to estimate the charging time |
| `max_writes_per_day` | 20 | Safety limit of inverter writes per day |

## Backups, state and rolling back

- **`backups/`**: JSON files with all raw configuration registers, created by
  `deye_read_config.py` and before every write by `deye_apply.py` and
  `deye_storm.py`. Each file records the date and the reason.
- **`state/storm.json`**: storm mode state (active or not, planned charge, target
  storm, outage, daily write count).
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

