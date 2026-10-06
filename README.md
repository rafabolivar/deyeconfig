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

`deye_storm.py` checks the weather forecast ([Open-Meteo](https://open-meteo.com/), free,
no API key) and, if a thunderstorm or very heavy rain is expected and the battery is
low, charges it from the grid in advance so it is ready for a possible power outage.

```bash
.venv/bin/python deye_storm.py                  # show what it would do (dry run)
.venv/bin/python deye_storm.py --apply          # act on the inverter (no confirmation)
.venv/bin/python deye_storm.py --assume-storm   # pretend a storm is forecast (testing, dry run only)
```

How it works:

1. If a storm is forecast within `lookahead_hours` and the battery SOC is below
   `trigger_soc`, it saves the current configuration as a profile in
   `state/pre_storm.toml` and enables grid charging with all Time Of Use slots
   at `target_soc`. The inverter charges up to that level and holds it.
2. While storms keep being forecast, storm mode stays active.
3. `grace_hours` after the last forecast storm hour (or after `max_hours` as a
   safety limit), the saved configuration is restored.

The inverter is only written when the state changes. During a power outage the
inverter runs off-grid and uses the battery down to its shutdown / low battery SOC.

Settings are in the `[storm]` section of `config.toml` (location, look-ahead hours,
SOC thresholds, which weather codes and how much rain count as a storm).

### Running it automatically

The `systemd/` folder contains a service and a timer that run the check every
15 minutes. Adjust `User` and the paths in `deye-storm.service` if needed, then:

```bash
sudo cp systemd/deye-storm.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deye-storm.timer
journalctl -u deye-storm.service    # logs
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
