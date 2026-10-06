#!/usr/bin/env python3
"""
Applies a configuration profile (profiles/*.toml) to a Deye inverter.

Usage:
    python deye_apply.py profiles/examples/winter.toml                # show what would change
    python deye_apply.py profiles/examples/winter.toml --show         # same, explicitly
    python deye_apply.py profiles/examples/winter.toml --apply        # write (asks for confirmation)
    python deye_apply.py profiles/examples/winter.toml --apply --yes  # write without asking

Process:
  1. Validates the whole profile (parameters exist, are writable and in range).
     If there is any error, nothing is written.
  2. Reads the current configuration and shows only what changes (current -> new).
  3. With --apply: saves a backup, writes the modified registers and reads
     them back to verify.
"""

import argparse
import sys
import tomllib
from pathlib import Path

from deye_common import DEFAULT_CONFIG, connect, load_config
from deye_map import (InvalidValue, format_tou_charge, format_value, hhmm, load_map,
                      time_to_register, to_register)
from deye_read_config import read_registers, save_backup

TOU_KEYS = {"slot", "time", "power", "voltage", "soc", "grid_charge", "gen_charge"}


def compute_changes(profile: dict, regmap: dict, r: dict[int, int]):
    """Return (proposed, changes, errors, warnings). Does not touch the inverter."""
    proposed = dict(r)
    changes, errors, warnings = [], [], []

    # --- general parameters
    for ident, value in profile.get("parameters", {}).items():
        p = regmap["by_id"].get(ident)
        if p is None:
            errors.append(f"{ident}: unknown parameter (list ids with deye_read_config.py --raw)")
            continue
        if not p.get("writable"):
            errors.append(f"{ident}: read-only parameter, cannot be changed from a profile")
            continue
        try:
            new = to_register(p, value, proposed[p["reg"]])
        except InvalidValue as e:
            errors.append(str(e))
            continue
        before = format_value(p, proposed)
        proposed[p["reg"]] = new
        changes.append((p["name"], before, format_value(p, proposed)))

    # --- Time Of Use slots
    t = regmap.get("tou")
    for entry in profile.get("tou", []):
        n = entry.get("slot")
        if not t:
            errors.append("this inverter's map does not define Time Of Use slots")
            break
        if not isinstance(n, int) or not 1 <= n <= t["slots"]:
            errors.append(f"tou: 'slot' must be a number from 1 to {t['slots']} (got {n!r})")
            continue
        unknown = set(entry) - TOU_KEYS
        if unknown:
            errors.append(f"slot {n}: unknown keys {sorted(unknown)}. Valid: {sorted(TOU_KEYS - {'slot'})}")
            continue
        i, label = n - 1, f"Slot {n}"
        try:
            if "time" in entry:
                reg = t["time"] + i
                before, proposed[reg] = hhmm(proposed[reg]), time_to_register(entry["time"], f"slot {n} time")
                changes.append((f"{label}: start", before, hhmm(proposed[reg])))
            if "power" in entry:
                v = entry["power"]
                if not isinstance(v, int) or not 0 <= v <= t["power_max"]:
                    raise InvalidValue(f"slot {n} power: {v!r} out of range (0-{t['power_max']} W)")
                reg = t["power"] + i
                changes.append((f"{label}: power", f"{proposed[reg]} W", f"{v} W"))
                proposed[reg] = v
            if "voltage" in entry:
                v = entry["voltage"]
                if not isinstance(v, (int, float)) or not t["voltage_min"] <= v <= t["voltage_max"]:
                    raise InvalidValue(f"slot {n} voltage: {v!r} out of range ({t['voltage_min']}-{t['voltage_max']} V)")
                reg = t["voltage"] + i
                changes.append((f"{label}: voltage", f"{proposed[reg] / 100:.2f} V", f"{v:.2f} V"))
                proposed[reg] = round(v * 100)
            if "soc" in entry:
                v = entry["soc"]
                if not isinstance(v, int) or not 0 <= v <= 100:
                    raise InvalidValue(f"slot {n} soc: {v!r} out of range (0-100 %)")
                reg = t["soc"] + i
                changes.append((f"{label}: SOC", f"{proposed[reg]} %", f"{v} %"))
                proposed[reg] = v
            for key, bit in (("grid_charge", 0), ("gen_charge", 1)):
                if key in entry:
                    v = entry[key]
                    if not isinstance(v, bool):
                        raise InvalidValue(f"slot {n} {key}: expected true/false, got {v!r}")
                    reg = t["charge"] + i
                    before = format_tou_charge(proposed[reg])
                    proposed[reg] = proposed[reg] | (1 << bit) if v else proposed[reg] & ~(1 << bit)
                    changes.append((f"{label}: charge", before, format_tou_charge(proposed[reg])))
        except InvalidValue as e:
            errors.append(str(e))

    if t:
        # Start times must be in increasing order
        times = [proposed[t["time"] + i] for i in range(t["slots"])]
        if any(a >= b for a, b in zip(times, times[1:])):
            errors.append("TOU slots: start times must be increasing "
                          f"({', '.join(hhmm(h) for h in times)})")
        # Consistency warnings
        tou_grid = any(proposed[t["charge"] + i] & 1 for i in range(t["slots"]))
        p_grid, p_tou = regmap["by_id"].get("grid_charge"), regmap["by_id"].get("time_of_use")
        if tou_grid and p_grid and not proposed[p_grid["reg"]]:
            warnings.append("some slots have grid charging, but 'grid_charge' is disabled: it will not charge")
        if tou_grid and p_tou and not proposed[p_tou["reg"]] & 1:
            warnings.append("some slots have grid charging, but 'time_of_use' is disabled: slots will not apply")

    # Drop entries with no real change (e.g. same value it already had)
    changes = [c for c in changes if c[1] != c[2]]
    return proposed, changes, errors, warnings


def write(conf: dict, r: dict[int, int], proposed: dict[int, int]) -> list[str]:
    """Write the modified registers and verify by reading them back. Returns failures."""
    modified = sorted(reg for reg in proposed if proposed[reg] != r[reg])
    failures = []
    inv = connect(conf)
    try:
        for reg in modified:
            try:
                inv.write_multiple_holding_registers(register_addr=reg, values=[proposed[reg]])
            except Exception as e:
                failures.append(f"register {reg}: write error ({e})")
        for reg in modified:
            read = inv.read_holding_registers(register_addr=reg, quantity=1)[0]
            if read != proposed[reg]:
                failures.append(f"register {reg}: wrote {proposed[reg]} but the inverter has {read}")
    finally:
        inv.disconnect()
    print(f"Registers written: {len(modified)}")
    return failures


def main():
    parser = argparse.ArgumentParser(description="Apply a configuration profile to a Deye inverter")
    parser.add_argument("profile", type=Path, help="Profile file (e.g. profiles/examples/winter.toml)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="Only show what would change (default)")
    mode.add_argument("--apply", action="store_true", help="Write the changes to the inverter")
    parser.add_argument("--yes", action="store_true", help="With --apply, do not ask for confirmation")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()

    if not args.profile.exists():
        sys.exit(f"ERROR: profile {args.profile} not found")
    try:
        with args.profile.open("rb") as f:
            profile = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"ERROR: profile {args.profile} is not valid TOML: {e}")

    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)

    print(f"Profile: {args.profile.name}" + (f" — {profile['description']}" if "description" in profile else ""))
    serial, r = read_registers(conf, regmap)
    proposed, changes, errors, warnings = compute_changes(profile, regmap, r)

    if errors:
        print("\nERRORS in profile (nothing has been written):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    if not changes:
        print("\nThe inverter configuration already matches the profile. Nothing to change.")
        return

    width = max(len(c[0]) for c in changes)
    print(f"\nChanges ({len(changes)}):")
    for name, before, after in changes:
        print(f"  {name:<{width}} : {before}  ->  {after}")
    for w in warnings:
        print(f"\nWARNING: {w}")

    if not args.apply:
        print("\nDry run: nothing has been changed. Use --apply to write the changes.")
        return

    if not args.yes:
        if input("\nApply these changes to the inverter? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing has been changed.")
            return

    backup = save_backup(serial, map_path, r, reason=f"before applying {args.profile.name}")
    print(f"\nBackup before changes: {backup}")
    failures = write(conf, r, proposed)
    if failures:
        print("\nWARNING, verification found problems:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(2)
    print("Changes applied and verified successfully.")


if __name__ == "__main__":
    main()

