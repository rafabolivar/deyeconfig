# deyeconfig

Tool to read and change the configuration of Deye inverters through the Solarman
data logger on the local network (Modbus TCP, port 8899), without relying on the cloud.

Typical use: switching between configuration profiles (e.g. winter / summer) to
control battery charging from the grid, Time Of Use slots, SOC limits, etc.

## Installation

Requires Python 3.11 or later.

```bash
git clone https://github.com/rafabolivar/deyeconfig.git
cd deyeconfig
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edit `config.toml` with your logger's IP address and serial number.

## Connection test

```bash
.venv/bin/python deye_test_connection.py
```

Reads the inverter serial number, battery SOC and battery voltage. Read-only.

## Reading the inverter configuration

```bash
.venv/bin/python deye_read_config.py          # show everything and save a backup
.venv/bin/python deye_read_config.py --raw    # also show id, register and raw value
.venv/bin/python deye_read_config.py --no-backup
```

Shows the whole configuration: battery, grid and generator charging, work mode,
grid protections, GEN port / SmartLoad and the 6 Time Of Use slots. It also saves
a JSON backup with all raw registers in `backups/` (excluded from the repository).
Read-only.

## Applying a configuration profile

A profile (`profiles/*.toml`) lists only the parameters to change, in normal units
(A, %, V, "HH:MM", true/false). Everything else is left untouched.

```bash
.venv/bin/python deye_apply.py profiles/examples/winter.toml                # show what would change
.venv/bin/python deye_apply.py profiles/examples/winter.toml --show         # same, explicitly
.venv/bin/python deye_apply.py profiles/examples/winter.toml --apply        # write (asks for confirmation)
.venv/bin/python deye_apply.py profiles/examples/winter.toml --apply --yes  # no confirmation (cron)
```

Nothing is ever written without `--apply`. With `--apply`:

1. The whole profile is validated (parameters exist, are writable and in range).
   If there is any error, nothing is written.
2. A backup of the current configuration is saved in `backups/`.
3. Only the registers that change are written, and they are read back to verify.

Example profile:

```toml
description = "Winter: grid charging during off-peak hours up to 80 %"

[parameters]
grid_charge = true
grid_charge_current = 40

[[tou]]
slot = 1
time = "00:00"
soc = 80
grid_charge = true
```

- `[parameters]`: any writable parameter of the map, by its `id`.
  List the ids with `deye_read_config.py --raw`.
- `[[tou]]`: Time Of Use slots (1 to 6) with `time`, `power`, `voltage`, `soc`,
  `grid_charge` and `gen_charge`. Start times must be increasing.

For safety, some parameters are read-only (grid protections, battery type, charging
voltages...). This is controlled by `writable` in the map.

The profiles in `profiles/examples/` are examples: review them and adjust them to
your installation before applying them. Keep your own profiles directly in
`profiles/` (e.g. `profiles/winter.toml`): they are excluded from the repository by
`.gitignore`, so your personal settings are never committed.

## Exporting the current configuration as a profile

```bash
.venv/bin/python deye_export.py                            # print the profile to the screen
.venv/bin/python deye_export.py -o profiles/current.toml   # save it to a file
.venv/bin/python deye_export.py -o profiles/current.toml --force   # overwrite if it exists
```

Creates a profile with the inverter's current values: all writable parameters and
the 6 Time Of Use slots, ready to be applied with `deye_apply.py`. Read-only
parameters are included as comments, for reference. The `.toml` extension is added
automatically if missing. Read-only.

Useful as a starting point for your own profiles (export, remove the lines you do
not want to change, adjust the rest) or to save a configuration you want to go
back to later.

## Storm mode

`deye_storm.py` is a service that pre-charges the battery from the grid when
thunderstorms or very heavy rain are forecast ([Open-Meteo](https://open-meteo.com/),
free, no API key), so the house is ready for a possible power outage.

How it works (all values configurable in the `[storm]` section of `config.toml`):

1. **Planning** at `planning_time` (default 00:00, start of the off-peak period): if
   a storm is forecast within the next `forecast_hours` (24), it calculates how long
   it takes to charge from the current SOC to `target_soc` (80 %) and schedules the
   grid charge to finish at `ready_by` (07:30), or `storm_margin_minutes` before the
   storm if it comes earlier. If there is not enough time, it charges immediately.
2. **Storm already happening** (the forecast for the current hour meets the
   criteria): charges immediately while the grid is available.
3. **Backup** outside planning: if a storm is forecast within `backup_hours` and
   the SOC is below `backup_trigger_soc`, charges immediately.
4. While in storm mode, `target_soc` is kept as the minimum battery level.
5. **Outage protection**: the grid status is checked every `grid_check_interval`
   seconds. If the grid goes down, all Time Of Use slots are lowered to
   `outage_soc` (15 %) so the whole battery is available. When the grid returns,
   the battery is recharged (`recharge_after_outage`).
6. `grace_hours` after the last forecast storm hour, if the storm is no longer
   forecast, or after `max_hours`, the configuration saved before storm mode
   (`state/pre_storm.toml`) is restored.

The Time Of Use slots are calculated by the service. Example: battery at 15 %,
storm forecast at 18:00:

| Slot | Start | SOC | Grid charge | Purpose |
|---|---|---|---|---|
| 1 | 00:00 | 15 % | No | Normal use until charging starts |
| 2 | 03:55 | 80 % | Yes | Charge to 80 % by 07:30 |
| 3-6 | 08:00, 12:00, 16:00, 20:00 | 80 % | Yes | Keep 80 % until the storm has passed |

The inverter is only written when its configuration has to change, with a daily
limit (`max_writes_per_day`). Every change is preceded by a backup in `backups/`.

```bash
.venv/bin/python deye_storm.py                    # single check, show what it would do
.venv/bin/python deye_storm.py --apply            # single check, act on the inverter
.venv/bin/python deye_storm.py --daemon --apply   # run as a service
.venv/bin/python deye_storm.py --plan-now         # run the planning logic now
```

Testing options (dry run only):

```bash
.venv/bin/python deye_storm.py --assume-storm 18             # pretend a storm in 18 h
.venv/bin/python deye_storm.py --assume-outage               # pretend the grid is down
.venv/bin/python deye_storm.py --at '2026-10-08 00:05' --assume-storm 18   # pretend it is 00:05
```

**Requirement for outage protection:** the machine running the service and the
network equipment must stay powered during an outage (e.g. connected to the
inverter's backup output or a UPS).

### Running it as a service

Adjust `User` and the paths in `systemd/deye-storm.service` if needed, then:

```bash
sudo cp systemd/deye-storm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deye-storm.service
journalctl -u deye-storm.service -f    # logs
```

## Register maps

The registers of each model are defined in `maps/` (TOML format), separate from the
code. The map is selected in `config.toml` (`[inverter] map = ...`). To add parameters
or support another model, just edit or create a map.

| Map | Models | Tested with |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye single-phase LV hybrid (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

Sources: Deye Modbus protocol V118 and the `deye_hybrid.yaml` definition from
[ha-solarman](https://github.com/davidrapan/ha-solarman).

## Disclaimer

Writing wrong values to an inverter can affect its operation or the battery.
Use at your own risk, always run without `--apply` first and check the changes.
