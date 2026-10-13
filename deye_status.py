#!/usr/bin/env python3
"""
Live status of the Deye inverter: solar production, battery, grid and house load,
plus the active Time Of Use slot and the BMS limits. Only READS registers.

Usage:
    python deye_status.py              # show the status once
    python deye_status.py --watch      # refresh every 10 seconds (Ctrl+C to exit)
    python deye_status.py --watch 5    # refresh every 5 seconds
"""

import argparse
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from deye_common import DEFAULT_CONFIG, connect, load_config, read_block

# Live data registers (Deye single-phase LV hybrid)
REG = {
    "grid_power": 169,      # W, positive = buying from the grid, negative = exporting
    "load_power": 178,      # W, house consumption
    "battery_voltage": 183,  # x0.01 V
    "soc": 184,             # %
    "pv1_power": 186,       # W
    "pv2_power": 187,       # W
    "battery_power": 190,   # W, positive = discharging, negative = charging
    "grid_status": 194,     # 1 = grid connected
}
TOU_TIME, TOU_SOC, TOU_CHARGE = 250, 268, 274
REG_GRID_CHARGE, REG_GRID_CHARGE_CURRENT, REG_MAX_CHARGE = 232, 230, 210
REG_BMS_CHARGE_LIMIT, REG_BMS_DISCHARGE_LIMIT = 314, 315


def s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def read_status(conf: dict) -> dict[int, int]:
    inv = connect(conf, quiet=True)
    try:
        r = read_block(inv, 150, 50)
        r.update(read_block(inv, 200, 81))
        r.update(read_block(inv, 310, 10))
        return r
    finally:
        inv.disconnect()


def active_slot(r: dict[int, int], now: datetime) -> int:
    minute = now.hour * 60 + now.minute
    starts = [r[TOU_TIME + i] // 100 * 60 + r[TOU_TIME + i] % 100 for i in range(6)]
    return max((i for i, st in enumerate(starts) if st <= minute), default=5)


def show(r: dict[int, int], tz) -> None:
    now = datetime.now(tz)
    pv1, pv2 = r[REG["pv1_power"]], r[REG["pv2_power"]]
    grid = s16(r[REG["grid_power"]])
    battery = s16(r[REG["battery_power"]])
    load = s16(r[REG["load_power"]])
    grid_txt = f"buying {grid} W" if grid > 0 else f"exporting {-grid} W" if grid < 0 else "0 W"
    bat_txt = f"discharging {battery} W" if battery > 0 else f"charging {-battery} W" if battery < 0 else "idle"
    k = active_slot(r, now)
    t = r[TOU_TIME + k]
    slot_charge = "grid charge" if r[TOU_CHARGE + k] & 1 else "no grid charge"

    print(f"Deye status  {now:%Y-%m-%d %H:%M:%S}")
    print(f"  Solar    : {pv1 + pv2:5d} W   (PV1 {pv1} W, PV2 {pv2} W)")
    print(f"  Battery  : {r[REG['soc']]:5d} %   {bat_txt}, {r[REG['battery_voltage']] / 100:.2f} V")
    print(f"  Grid     : {'connected' if r[REG['grid_status']] == 1 else 'DOWN':>9}   {grid_txt}")
    print(f"  House    : {load:5d} W")
    print(f"  TOU slot : {k + 1} (from {t // 100:02d}:{t % 100:02d}), SOC {r[TOU_SOC + k]} %, {slot_charge}")
    print(f"  Limits   : grid charging {'on' if r[REG_GRID_CHARGE] else 'off'} at {r[REG_GRID_CHARGE_CURRENT]} A, "
          f"max charge {r[REG_MAX_CHARGE]} A | BMS allows charge {r[REG_BMS_CHARGE_LIMIT]} A, "
          f"discharge {r[REG_BMS_DISCHARGE_LIMIT]} A")


def main():
    parser = argparse.ArgumentParser(description="Live status of the Deye inverter")
    parser.add_argument("--watch", nargs="?", const=10, type=int, metavar="SECONDS",
                        help="Refresh every SECONDS seconds (default 10)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()
    conf = load_config(args.config)
    with args.config.open("rb") as f:  # local time zone (the inverter's slots are local time)
        tz = ZoneInfo(tomllib.load(f).get("storm", {}).get("timezone", "Europe/Madrid"))
    try:
        while True:
            try:
                r = read_status(conf)
            except (Exception, SystemExit) as e:
                if not args.watch:
                    sys.exit(f"ERROR reading the inverter: {e}")
                print(f"ERROR reading the inverter, retrying: {e}")
            else:
                if args.watch:
                    print("\033[2J\033[H", end="")  # clear the screen
                show(r, tz)
            if not args.watch:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

