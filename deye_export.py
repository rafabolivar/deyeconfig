#!/usr/bin/env python3
"""
Exports the current inverter configuration as a profile (TOML) that can be
edited and applied later with deye_apply.py. Only READS registers.

Writable parameters and Time Of Use slots are exported as profile values.
Read-only parameters are included as comments, for reference.

Usage:
    python deye_export.py                          # print the profile to the screen
    python deye_export.py -o profiles/current.toml # save it to a file
    python deye_export.py -o profiles/current.toml --force   # overwrite if it exists
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from deye_common import DEFAULT_CONFIG, load_config
from deye_map import format_value, from_register, hhmm, load_map
from deye_read_config import read_registers


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    return str(v)


def build_profile(serial: str, map_path: Path, regmap: dict, r: dict[int, int]) -> str:
    now = datetime.now()
    out = [
        f"# Profile exported from inverter {serial} on {now:%Y-%m-%d %H:%M:%S}",
        f"# Map: {map_path.name} ({regmap['info']['model']})",
        "#",
        "# Writable parameters are active values. Read-only parameters are shown",
        "# as comments for reference and are never applied.",
        "# Remove any line you do not want this profile to change.",
        "",
        f'description = "Configuration exported from inverter {serial} on {now:%Y-%m-%d}"',
        "",
        "[parameters]",
    ]
    group = None
    for p in regmap["param"]:
        if p["group"] != group:
            group = p["group"]
            out.append(f"\n# --- {group}")
        unit = f"  # {p['unit']}" if p.get("unit") and p["type"] == "num" else ""
        if p.get("writable"):
            out.append(f"{p['id']} = {toml_value(from_register(p, r))}{unit}")
        else:
            out.append(f"# {p['id']} = {format_value(p, r)}  (read-only)")

    t = regmap.get("tou")
    if t:
        out.append("\n# --- Time Of Use slots")
        for i in range(t["slots"]):
            charge = r[t["charge"] + i]
            out += [
                "",
                "[[tou]]",
                f"slot = {i + 1}",
                f'time = "{hhmm(r[t["time"] + i])}"',
                f"power = {r[t['power'] + i]}  # W",
                f"voltage = {r[t['voltage'] + i] / 100:.2f}  # V",
                f"soc = {r[t['soc'] + i]}  # %",
                f"grid_charge = {toml_value(bool(charge & 1))}",
                f"gen_charge = {toml_value(bool(charge & 2))}",
            ]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Export the current inverter configuration as a profile")
    parser.add_argument("-o", "--output", type=Path, help="Output file (default: print to the screen)")
    parser.add_argument("--force", action="store_true", help="Overwrite the output file if it exists")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()

    if args.output and args.output.suffix.lower() != ".toml":
        fixed = args.output.with_suffix(".toml")
        print(f"Note: profiles are TOML files, saving as {fixed} instead of {args.output}", file=sys.stderr)
        args.output = fixed

    if args.output and args.output.exists() and not args.force:
        sys.exit(f"ERROR: {args.output} already exists. Use --force to overwrite it.")

    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    serial, r = read_registers(conf, regmap)
    profile = build_profile(serial, map_path, regmap, r)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(profile)
        print(f"Profile saved to: {args.output}")
    else:
        print(profile, end="")


if __name__ == "__main__":
    main()

