#!/usr/bin/env python3
"""
Reads the full configuration of a Deye inverter through the Solarman data
logger (Modbus TCP), using a register map file (maps/ folder).
Only READS registers. Also saves a JSON backup with all raw registers of
the configuration range.

Usage:
    python deye_read_config.py                  # show and save backup
    python deye_read_config.py --no-backup
    python deye_read_config.py --raw            # also show id, register and raw value
    python deye_read_config.py --config other.toml
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from deye_common import DEFAULT_CONFIG, connect, load_config, read_block, read_inverter_serial
from deye_map import format_tou_charge, format_value, hhmm, load_map

BACKUP_DIR = Path(__file__).with_name("backups")


def read_registers(conf: dict, regmap: dict) -> tuple[str, dict[int, int]]:
    start, end = regmap["info"]["backup_range"]
    inv = connect(conf)
    try:
        return read_inverter_serial(inv), read_block(inv, start, end - start + 1)
    except Exception as e:
        sys.exit(f"ERROR reading registers: {e}")
    finally:
        inv.disconnect()


def show(regmap: dict, r: dict[int, int], raw: bool) -> None:
    group = None
    width = max(len(p["name"]) for p in regmap["param"])
    for p in regmap["param"]:
        if p["group"] != group:
            group = p["group"]
            print(f"\n=== {group} ===")
        extra = ""
        if raw:
            ro = "" if p.get("writable") else " (read-only)"
            extra = f"   [{p['id']}{ro} | reg {p['reg']} = {r[p['reg']]}]"
        print(f"  {p['name']:<{width}} : {format_value(p, r)}{extra}")

    t = regmap.get("tou")
    if t:
        print("\n=== Time Of Use slots ===")
        print("  Slot  Start   Power      Voltage   SOC   Charge")
        for i in range(t["slots"]):
            print(f"   {i + 1}    {hhmm(r[t['time'] + i])}   {r[t['power'] + i]:>5} W  "
                  f"{r[t['voltage'] + i] / 100:>6.2f} V  {r[t['soc'] + i]:>3} %   "
                  f"{format_tou_charge(r[t['charge'] + i])}")


def save_backup(serial: str, map_path: Path, r: dict[int, int], reason: str = "read") -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"config_{serial}_{datetime.now():%Y%m%d_%H%M%S}.json"
    data = {
        "inverter": serial,
        "date": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "map": map_path.name,
        "registers": {str(k): v for k, v in sorted(r.items())},
    }
    path.write_text(json.dumps(data, indent=2))
    return path


def main():
    parser = argparse.ArgumentParser(description="Read the full configuration of a Deye inverter")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    parser.add_argument("--no-backup", action="store_true", help="Do not save a JSON backup")
    parser.add_argument("--raw", action="store_true",
                        help="Also show parameter id, register number and raw value")
    args = parser.parse_args()

    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    serial, registers = read_registers(conf, regmap)

    print(f"Inverter {serial}  |  Map: {regmap['info']['model']}")
    show(regmap, registers, args.raw)

    if not args.no_backup:
        print(f"\nBackup saved to: {save_backup(serial, map_path, registers)}")


if __name__ == "__main__":
    main()

