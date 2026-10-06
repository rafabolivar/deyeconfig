#!/usr/bin/env python3
"""
Storm mode: checks the weather forecast (Open-Meteo) and, if a thunderstorm or
very heavy rain is expected and the battery is low, charges it from the grid
in advance so it is ready for a possible power outage. When the storm has
passed, the previous configuration is restored.

Designed to run periodically (e.g. every 15 minutes) from a systemd timer.
Settings live in the [storm] section of config.toml.

Usage:
    python deye_storm.py                  # show what it would do (dry run)
    python deye_storm.py --show           # same, explicitly
    python deye_storm.py --apply          # act on the inverter (used by the timer)
    python deye_storm.py --assume-storm   # pretend a storm is forecast (for testing)

Logic:
  - Not in storm mode, storm forecast within 'lookahead_hours' and battery SOC
    below 'trigger_soc'  ->  save the current configuration as a profile and
    enable grid charging with all Time Of Use slots at 'target_soc'.
  - In storm mode  ->  stay while storms are forecast; restore the saved
    configuration 'grace_hours' after the last forecast storm hour, or after
    'max_hours' as a safety limit.

During a power outage the inverter runs off-grid and uses the battery down to
its shutdown / low battery SOC settings.
"""

import argparse
import json
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deye_apply import compute_changes, write
from deye_common import DEFAULT_CONFIG, load_config
from deye_export import build_profile
from deye_map import load_map
from deye_read_config import read_registers, save_backup

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
STATE_FILE = STATE_DIR / "storm.json"
PRE_STORM_PROFILE = STATE_DIR / "pre_storm.toml"
SOC_REGISTER = 184
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

DEFAULTS = {
    "lookahead_hours": 6,
    "trigger_soc": 60,
    "target_soc": 70,
    "storm_codes": [95, 96, 99],
    "heavy_rain_mm": 10.0,
    "min_probability": 50,
    "grace_hours": 1,
    "max_hours": 24,
}


# ------------------------------------------------------------------ settings and state

def load_storm_settings(config_path: Path) -> dict:
    with config_path.open("rb") as f:
        s = {**DEFAULTS, **tomllib.load(f).get("storm", {})}
    errors = [f"missing [storm] {k}" for k in ("latitude", "longitude")
              if not isinstance(s.get(k), (int, float))]
    if not 0 < s["trigger_soc"] <= s["target_soc"] <= 100:
        errors.append("[storm] requires 0 < trigger_soc <= target_soc <= 100")
    if errors:
        sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))
    return s


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"active": False}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------------ forecast

def fetch_forecast(s: dict) -> tuple[timezone, list[dict]]:
    params = {
        "latitude": s["latitude"],
        "longitude": s["longitude"],
        "hourly": "weather_code,precipitation,precipitation_probability",
        "forecast_hours": s["lookahead_hours"] + 1,
        "timezone": "auto",
    }
    url = f"{FORECAST_API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.load(resp)
    tz = timezone(timedelta(seconds=data.get("utc_offset_seconds", 0)))
    h = data["hourly"]
    return tz, [{
        "time": datetime.fromisoformat(t).replace(tzinfo=tz),
        "code": h["weather_code"][i],
        "rain": h["precipitation"][i] or 0,
        "prob": h["precipitation_probability"][i],
    } for i, t in enumerate(h["time"])]


def find_storms(hours: list[dict], s: dict) -> list[tuple[datetime, str]]:
    """Return (hour, reason) for each forecast hour with a thunderstorm or heavy rain."""
    events = []
    for x in hours:
        reasons = []
        if x["code"] in s["storm_codes"]:
            reasons.append(f"thunderstorm (code {x['code']})")
        prob = x["prob"] if x["prob"] is not None else 100
        if x["rain"] >= s["heavy_rain_mm"] and prob >= s["min_probability"]:
            reasons.append(f"heavy rain {x['rain']} mm/h ({prob} %)")
        if reasons:
            events.append((x["time"], ", ".join(reasons)))
    return sorted(events)


# ------------------------------------------------------------------ inverter actions

def storm_profile(s: dict, regmap: dict) -> dict:
    """Profile used during storm mode: grid charging, all slots holding target_soc."""
    return {
        "description": f"Storm mode: grid charging up to {s['target_soc']} %",
        "parameters": {"grid_charge": True, "time_of_use": "MTWTFSS"},
        "tou": [{"slot": i + 1, "soc": s["target_soc"], "grid_charge": True}
                for i in range(regmap["tou"]["slots"])],
    }


def apply_profile(profile: dict, label: str, ctx: dict, do_apply: bool) -> bool:
    """Apply a profile dict. Returns True if the inverter matches (or would match) the profile."""
    proposed, changes, errors, warnings = compute_changes(profile, ctx["regmap"], ctx["r"])
    if errors:
        print(f"ERRORS in {label} (nothing written):")
        for e in errors:
            print(f"  - {e}")
        return False
    if not changes:
        print(f"Inverter already matches {label}.")
        return True
    print(f"Changes for {label} ({len(changes)}):")
    for name, before, after in changes:
        print(f"  {name} : {before}  ->  {after}")
    for w in warnings:
        print(f"WARNING: {w}")
    if not do_apply:
        print("Dry run: nothing has been changed. Use --apply to act on the inverter.")
        return True
    backup = save_backup(ctx["serial"], ctx["map_path"], ctx["r"], reason=label)
    print(f"Backup before changes: {backup}")
    failures = write(ctx["conf"], ctx["r"], proposed)
    if failures:
        print("WARNING, verification found problems:")
        for f in failures:
            print(f"  - {f}")
        return False
    print("Changes applied and verified.")
    return True


# ------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description="Storm mode: pre-charge the battery when storms are forecast")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="Only show what it would do (default)")
    mode.add_argument("--apply", action="store_true", help="Act on the inverter (no confirmation, for the timer)")
    parser.add_argument("--assume-storm", action="store_true",
                        help="Pretend a storm is forecast in 2 hours (for testing)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()

    if args.assume_storm and args.apply:
        sys.exit("ERROR: --assume-storm is for testing and cannot be combined with --apply")

    s = load_storm_settings(args.config)
    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    if "tou" not in regmap:
        sys.exit("ERROR: this inverter's map does not define Time Of Use slots")

    try:
        tz, hours = fetch_forecast(s)
    except Exception as e:
        sys.exit(f"ERROR fetching the weather forecast: {e}")
    now = datetime.now(tz)  # local time of the forecast location
    events = find_storms(hours, s)
    if args.assume_storm:
        events = sorted(events + [(now + timedelta(hours=2), "simulated storm (--assume-storm)")])

    serial, r = read_registers(conf, regmap)
    soc = r[SOC_REGISTER]
    ctx = {"conf": conf, "regmap": regmap, "map_path": map_path, "serial": serial, "r": r}
    state = load_state()

    print(f"Battery SOC: {soc} %  |  Storm mode: {'ACTIVE' if state['active'] else 'inactive'}"
          f"  |  Next {s['lookahead_hours']} h: "
          + (f"{len(events)} storm hour(s)" if events else "no storms"))
    for t, reason in events:
        print(f"  {t:%Y-%m-%d %H:%M}  {reason}")

    # ---------------------------------------------------------- not in storm mode
    if not state["active"]:
        if not events:
            print("Nothing to do.")
            return
        if soc >= s["trigger_soc"]:
            print(f"Storm forecast, but SOC {soc} % >= trigger {s['trigger_soc']} %: no action.")
            return
        print(f"Storm forecast and SOC {soc} % < {s['trigger_soc']} %: activating storm mode.")
        if args.apply:
            STATE_DIR.mkdir(exist_ok=True)
            PRE_STORM_PROFILE.write_text(build_profile(serial, map_path, regmap, r))
            print(f"Current configuration saved to {PRE_STORM_PROFILE}")
        else:
            print(f"Would save the current configuration to {PRE_STORM_PROFILE}")
        if apply_profile(storm_profile(s, regmap), "storm mode", ctx, args.apply) and args.apply:
            save_state({"active": True, "since": now.isoformat(),
                        "storm_until": events[-1][0].isoformat(), "reason": events[0][1]})
        return

    # ---------------------------------------------------------- in storm mode
    since = datetime.fromisoformat(state["since"]).astimezone(tz)
    until = datetime.fromisoformat(state["storm_until"]).astimezone(tz)
    if events and events[-1][0] > until:
        until = events[-1][0]
        if args.apply:
            save_state({**state, "storm_until": until.isoformat()})
    restore_at = until + timedelta(hours=s["grace_hours"])
    too_long = now > since + timedelta(hours=s["max_hours"])

    if not too_long and (events or now < restore_at):
        print(f"Storm mode active since {since:%Y-%m-%d %H:%M}, "
              f"restoring after {restore_at:%Y-%m-%d %H:%M} if no more storms are forecast.")
        return

    if too_long:
        print(f"Storm mode has been active for more than {s['max_hours']} h: restoring (safety limit).")
    else:
        print("Storm has passed: restoring the previous configuration.")
    if not PRE_STORM_PROFILE.exists():
        sys.exit(f"ERROR: {PRE_STORM_PROFILE} not found, cannot restore. Restore manually with deye_apply.py.")
    with PRE_STORM_PROFILE.open("rb") as f:
        previous = tomllib.load(f)
    if apply_profile(previous, "pre-storm configuration", ctx, args.apply) and args.apply:
        STATE_FILE.unlink(missing_ok=True)
        print("Storm mode finished.")


if __name__ == "__main__":
    main()

