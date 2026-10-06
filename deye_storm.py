#!/usr/bin/env python3
"""
Storm mode service: when thunderstorms or very heavy rain are forecast, keeps
the battery charged so the house is ready for a possible power outage,
charging from the grid in the cheapest tariff periods.

Settings: [storm] and [tariff] sections of config.toml.

When storm mode starts (forecast from Open-Meteo):
  - Off-peak period and a storm forecast within 'forecast_hours'.
  - A storm happening now (forecast for the current hour meets the criteria).
  - Backup, mid period: storm forecast, SOC below 'backup_trigger_soc' and this is
    the last cheap period (off-peak or mid) before the storm.
  - Emergency, peak period: storm now or within 'emergency_hours' and SOC below
    'emergency_soc'.

In storm mode the Time Of Use slots follow the tariff periods:
  off-peak  grid charge up to 'charge_soc' (100 %)
  mid       grid charge only up to 'hold_soc' (80 %)
  peak      no grid charge, battery kept at 'hold_soc' (emergency: charge up to 'emergency_soc')
Grid draw is limited to 'grid_power_limit' (inverter peak shaving) and the grid
charge current is set to 'charge_current'.
If the grid goes down, all slots are lowered to 'outage_soc' so the whole battery
is available. Storm mode ends 'grace_hours' after the last storm hour, when the
storm is no longer forecast, when the next storm comes after the next off-peak
period, or after 'max_hours'; the previous configuration is then restored.

Usage:
    python deye_storm.py                       # single check, show what it would do
    python deye_storm.py --apply               # single check, act on the inverter
    python deye_storm.py --daemon --apply      # run as a service
    python deye_storm.py --assume-storm 18     # pretend a storm in 18 h (testing, dry run only)
    python deye_storm.py --assume-outage       # pretend the grid is down (testing, dry run only)
    python deye_storm.py --at '2026-10-08 15:00'   # pretend it is this time (testing, dry run only)
"""

import argparse
import copy
import json
import signal
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deye_apply import compute_changes, write
from deye_common import DEFAULT_CONFIG, connect, load_config, read_block
from deye_export import build_profile
from deye_map import load_map
from deye_read_config import read_registers, save_backup
from deye_tariff import Tariff

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
STATE_FILE = STATE_DIR / "storm.json"
PRE_STORM_PROFILE = STATE_DIR / "pre_storm.toml"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

# Live data registers (Deye single-phase LV hybrid)
REG_SOC = 184             # %
REG_GRID_STATUS = 194     # 1 = grid connected

STATE_KEYS = ("since", "reason", "storm_until", "emergency", "emergency_period", "outage", "clear_checks")

DEFAULTS = {
    "timezone": "Europe/Madrid",
    "forecast_interval": 900,
    "grid_check_interval": 60,
    "forecast_hours": 24,
    "charge_soc": 100,
    "hold_soc": 80,
    "outage_soc": 15,
    "backup_enabled": True,
    "backup_trigger_soc": 50,
    "emergency_soc": 50,
    "emergency_hours": 2,
    "grid_power_limit": 4000,
    "charge_current": 40,
    "storm_codes": [95, 96, 99],
    "heavy_rain_mm": 10.0,
    "min_probability": 50,
    "grace_hours": 1,
    "max_hours": 36,
    "max_writes_per_day": 20,
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ settings and state

def load_settings(config_path: Path) -> tuple[dict, Tariff]:
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    s = {**DEFAULTS, **cfg.get("storm", {})}
    errors = [f"missing [storm] {k}" for k in ("latitude", "longitude")
              if not isinstance(s.get(k), (int, float))]
    try:
        s["tz"] = ZoneInfo(s["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"[storm] unknown timezone {s['timezone']!r}")
    for key, minimum in (("forecast_interval", 60), ("grid_check_interval", 10)):
        if not isinstance(s[key], int) or s[key] < minimum:
            errors.append(f"[storm] {key} must be a number of seconds, at least {minimum}")
    for key in ("charge_soc", "hold_soc", "outage_soc", "backup_trigger_soc", "emergency_soc"):
        if not isinstance(s[key], int) or not 5 <= s[key] <= 100:
            errors.append(f"[storm] {key} must be a percentage between 5 and 100")
    if not errors and not s["outage_soc"] <= s["hold_soc"] <= s["charge_soc"]:
        errors.append("[storm] requires outage_soc <= hold_soc <= charge_soc")
    if not isinstance(s["grid_power_limit"], int) or not 0 <= s["grid_power_limit"] <= 8000:
        errors.append("[storm] grid_power_limit must be between 0 (disabled) and 8000 W")
    if not isinstance(s["charge_current"], int) or not 0 <= s["charge_current"] <= 120:
        errors.append("[storm] charge_current must be between 0 (do not change) and 120 A")
    if errors:
        sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))
    return s, Tariff(cfg.get("tariff", {}), s["tz"])


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"mode": "normal"}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------------ forecast

def fetch_forecast(s: dict) -> list[dict]:
    params = {
        "latitude": s["latitude"],
        "longitude": s["longitude"],
        "hourly": "weather_code,precipitation,precipitation_probability",
        "forecast_hours": s["forecast_hours"] + 1,
        "timezone": s["timezone"],
    }
    url = f"{FORECAST_API}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.load(resp)
    h = data["hourly"]
    return [{
        "time": datetime.fromisoformat(t).replace(tzinfo=s["tz"]),
        "code": h["weather_code"][i],
        "rain": h["precipitation"][i] or 0,
        "prob": h["precipitation_probability"][i],
    } for i, t in enumerate(h["time"])]


def find_storms(hours: list[dict], s: dict, now: datetime) -> list[tuple[datetime, str]]:
    """(hour start, reason) for each current or future hour with a thunderstorm or heavy rain."""
    events = []
    for x in hours:
        if x["time"] + timedelta(hours=1) <= now:
            continue
        reasons = []
        if x["code"] in s["storm_codes"]:
            reasons.append(f"thunderstorm (code {x['code']})")
        prob = x["prob"] if x["prob"] is not None else 100
        if x["rain"] >= s["heavy_rain_mm"] and prob >= s["min_probability"]:
            reasons.append(f"heavy rain {x['rain']} mm/h ({prob} %)")
        if reasons:
            events.append((x["time"], ", ".join(reasons)))
    return sorted(events)


# ------------------------------------------------------------------ inverter

def read_live(conf: dict) -> dict[int, int]:
    """Read the live data and configuration registers needed by the service."""
    inv = connect(conf, quiet=True)
    try:
        r = read_block(inv, 150, 50)        # live data: SOC, grid status
        r.update(read_block(inv, 200, 81))  # configuration: grid charge, TOU, peak shaving
        r.update(read_block(inv, 280, 20))
        return r
    finally:
        inv.disconnect()


# ------------------------------------------------------------------ the service

class StormService:
    def __init__(self, s, tariff, conf, regmap, map_path, serial, apply,
                 assume_storm=None, assume_outage=False, at=None):
        self.s, self.tariff, self.conf, self.regmap = s, tariff, conf, regmap
        self.map_path, self.serial, self.apply = map_path, serial, apply
        self.assume_storm, self.assume_outage, self.at = assume_storm, assume_outage, at
        self.state = load_state()
        self.events: list[tuple[datetime, str]] = []

    # -- helpers
    def now(self) -> datetime:
        return self.at or datetime.now(self.s["tz"])

    def persist(self) -> None:
        if self.apply:
            save_state(self.state)

    def storm_now(self, now: datetime) -> bool:
        return any(t <= now < t + timedelta(hours=1) for t, _ in self.events)

    def storm_within(self, now: datetime, hours: float) -> bool:
        return any(t + timedelta(hours=1) > now and t <= now + timedelta(hours=hours) for t, _ in self.events)

    def last_cheap_chance(self, now: datetime, storm: datetime) -> bool:
        """True if no other off-peak or mid period starts between the current period and the storm."""
        _, current_start, _ = self.tariff.period_at(now)
        for name, start, _ in self.tariff.iter_periods(now, storm):
            if start > current_start and name in ("off-peak", "mid"):
                return False
        return True

    def write_allowed(self) -> bool:
        today = self.now().date().isoformat()
        writes = self.state.setdefault("writes", {"date": today, "count": 0})
        if writes["date"] != today:
            writes.update(date=today, count=0)
        if writes["count"] >= self.s["max_writes_per_day"]:
            log(f"WARNING: daily write limit reached ({self.s['max_writes_per_day']}), not writing.")
            return False
        return True

    def apply_profile(self, profile: dict, label: str, r: dict[int, int]) -> bool:
        proposed, changes, errors, warnings = compute_changes(profile, self.regmap, r)
        if errors:
            log(f"ERRORS in {label} (nothing written):")
            for e in errors:
                log(f"  - {e}")
            return False
        if not changes:
            return True
        log(f"Changes for {label} ({len(changes)}):")
        for name, before, after in changes:
            log(f"  {name} : {before}  ->  {after}")
        for w in warnings:
            log(f"WARNING: {w}")
        if not self.apply:
            log("Dry run: nothing has been changed.")
            return True
        if not self.write_allowed():
            return False
        save_backup(self.serial, self.map_path, r, reason=label)
        failures = write(self.conf, r, proposed)
        self.state["writes"]["count"] += 1
        self.persist()
        if failures:
            log("WARNING, verification found problems:")
            for f in failures:
                log(f"  - {f}")
            return False
        log("Changes applied and verified.")
        return True

    # -- storm mode transitions
    def enter_storm(self, reason: str, now: datetime) -> None:
        log(f"Entering storm mode: {reason}.")
        if self.apply:
            serial, full = read_registers(self.conf, self.regmap)
            STATE_DIR.mkdir(exist_ok=True)
            PRE_STORM_PROFILE.write_text(build_profile(serial, self.map_path, self.regmap, full))
            log(f"Current configuration saved to {PRE_STORM_PROFILE}")
        self.state.update(mode="storm", since=now.isoformat(), reason=reason,
                          storm_until=self.events[-1][0].isoformat(),
                          emergency=False, outage=False, clear_checks=0)
        self.persist()

    def exit_storm(self, why: str) -> None:
        log(f"Leaving storm mode: {why}. Restoring the previous configuration.")
        if PRE_STORM_PROFILE.exists():
            with PRE_STORM_PROFILE.open("rb") as f:
                previous = tomllib.load(f)
            serial, full = read_registers(self.conf, self.regmap)
            if not self.apply_profile(previous, "pre-storm configuration", full):
                return  # keep storm mode and try again next check
        else:
            log(f"ERROR: {PRE_STORM_PROFILE} not found. Restore manually with deye_apply.py.")
        for key in STATE_KEYS + ("charge_start", "target_storm", "deferred"):  # also legacy keys
            self.state.pop(key, None)
        self.state["mode"] = "normal"
        self.persist()
        log("Storm mode finished.")

    # -- layout
    def desired_layout(self, now: datetime, grid_ok: bool) -> tuple[str, dict]:
        s, n = self.s, self.regmap["tou"]["slots"]
        if not grid_ok:
            return (f"grid down: battery available down to {s['outage_soc']} %",
                    {"tou": [{"slot": i + 1, "soc": s["outage_soc"]} for i in range(n)]})
        emergency = self.state.get("emergency", False)
        tou = []
        for i, (start, period) in enumerate(self.tariff.slot_layout(now.date(), n)):
            if period == "off-peak":
                soc, grid = s["charge_soc"], True
            elif period == "mid":
                soc, grid = s["hold_soc"], True
            elif emergency:
                soc, grid = s["emergency_soc"], True
            else:
                soc, grid = s["hold_soc"], False
            tou.append({"slot": i + 1, "time": start, "soc": soc, "grid_charge": grid})
        params = {"grid_charge": True, "time_of_use": "MTWTFSS"}
        if s["grid_power_limit"]:
            params.update(grid_peak_shaving=True, grid_peak_shaving_power=s["grid_power_limit"])
        if s["charge_current"]:
            params["grid_charge_current"] = s["charge_current"]
        period = self.tariff.period_at(now)[0]
        return (f"{period} period" + (", emergency charge" if emergency else ""),
                {"parameters": params, "tou": tou})

    # -- periodic checks
    def forecast_check(self) -> None:
        s, now = self.s, self.now()
        self.events = find_storms(fetch_forecast(s), s, now)
        if self.assume_storm is not None:
            self.events = sorted(self.events + [(now + timedelta(hours=self.assume_storm),
                                                 "simulated storm (--assume-storm)")])
        r = read_live(self.conf)
        soc = r[REG_SOC]
        period = self.tariff.period_at(now)[0]
        storm_now = self.storm_now(now)
        first = self.events[0][0] if self.events else None
        storm = self.state["mode"] == "storm"

        log(f"[{now:%Y-%m-%d %H:%M}] {period} | SOC {soc} % | grid "
            f"{'OK' if r[REG_GRID_STATUS] == 1 and not self.assume_outage else 'DOWN'} | storm mode {'ACTIVE' if storm else 'inactive'} | "
            f"next {s['forecast_hours']} h: "
            + (f"{len(self.events)} storm hour(s), first at {first:%d/%m %H:%M}" if self.events else "no storms"))

        if not storm:
            if not self.events:
                return
            if storm_now:
                reason = "storm happening now"
            elif period == "off-peak":
                reason = f"storm forecast at {first:%d/%m %H:%M}, charging in the off-peak period"
            elif (period == "peak" and soc < s["emergency_soc"]
                  and self.storm_within(now, s["emergency_hours"])):
                reason = f"emergency: storm at {first:%H:%M} and SOC {soc} % < {s['emergency_soc']} %"
            elif (period == "mid" and s["backup_enabled"] and soc < s["backup_trigger_soc"]
                  and self.last_cheap_chance(now, first)):
                reason = (f"backup: storm at {first:%d/%m %H:%M}, SOC {soc} % < {s['backup_trigger_soc']} % "
                          "and this is the last cheap period before it")
            else:
                log("Storm forecast later; waiting for a cheaper period or for the storm.")
                return
            self.enter_storm(reason, now)
            self.grid_check(r)
            return

        # ---- already in storm mode
        until = datetime.fromisoformat(self.state["storm_until"]).astimezone(s["tz"])
        if self.events:
            self.state["clear_checks"] = 0
            if self.events[-1][0] > until:
                until = self.events[-1][0]
                self.state["storm_until"] = until.isoformat()
        else:
            self.state["clear_checks"] = self.state.get("clear_checks", 0) + 1
        self.persist()

        restore_at = until + timedelta(hours=s["grace_hours"])
        since = datetime.fromisoformat(self.state["since"]).astimezone(s["tz"])
        next_off_peak = self.tariff.next_start_of("off-peak", now, first) if first else None
        if now > since + timedelta(hours=s["max_hours"]):
            self.exit_storm(f"active for more than {s['max_hours']} h (safety limit)")
        elif not self.events and now >= restore_at:
            self.exit_storm("the storm has passed")
        elif not self.events and until > now and self.state["clear_checks"] >= 2:
            self.exit_storm("the storm is no longer forecast")
        elif self.events and not storm_now and period != "off-peak" and next_off_peak:
            self.exit_storm(f"the next storm ({first:%d/%m %H:%M}) comes after the next off-peak period "
                            f"({next_off_peak:%d/%m %H:%M}), it will be handled then")
        else:
            log(f"Storm mode active (since {since:%d/%m %H:%M}); restoring after {restore_at:%d/%m %H:%M} "
                "if no more storms are forecast.")
            self.grid_check(r)

    def grid_check(self, r: dict[int, int] | None = None) -> None:
        """In storm mode: outage detection, emergency charge and Time Of Use layout."""
        if self.state["mode"] != "storm":
            return
        s, now = self.s, self.now()
        if r is None:
            r = read_live(self.conf)
        grid_ok = r[REG_GRID_STATUS] == 1 and not self.assume_outage
        if not grid_ok and not self.state.get("outage"):
            log(f"[{now:%H:%M}] Grid DOWN: making the battery available down to {s['outage_soc']} %.")
            self.state["outage"] = True
        elif grid_ok and self.state.get("outage"):
            log(f"[{now:%H:%M}] Grid back: resuming the storm mode layout.")
            self.state["outage"] = False

        # Emergency charge in peak periods, latched until the period ends
        period, start, _ = self.tariff.period_at(now)
        if self.state.get("emergency") and self.state.get("emergency_period") != start.isoformat():
            self.state["emergency"] = False
            log(f"[{now:%H:%M}] Emergency charge finished (end of the peak period).")
        if (grid_ok and period == "peak" and not self.state.get("emergency")
                and r[REG_SOC] < s["emergency_soc"] and self.storm_within(now, s["emergency_hours"])):
            self.state.update(emergency=True, emergency_period=start.isoformat())
            log(f"[{now:%H:%M}] Emergency: SOC {r[REG_SOC]} % < {s['emergency_soc']} % with a storm "
                f"within {s['emergency_hours']} h, charging up to {s['emergency_soc']} % in the peak period.")
        self.persist()

        phase, layout = self.desired_layout(now, grid_ok)
        self.apply_profile(layout, f"storm mode ({phase})", r)

    def run_forever(self) -> None:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        s = self.s
        log(f"Storm mode service started: forecast every {s['forecast_interval']} s, grid every "
            f"{s['grid_check_interval']} s in storm mode, {'APPLY' if self.apply else 'dry run'} mode.")
        next_forecast, last_period = 0.0, None
        while not stop.is_set():
            try:
                period_start = self.tariff.period_at(self.now())[1]
                if time.monotonic() >= next_forecast or period_start != last_period:
                    self.forecast_check()  # also at every tariff period change
                    next_forecast, last_period = time.monotonic() + s["forecast_interval"], period_start
                else:
                    self.grid_check()
            except (Exception, SystemExit) as e:  # never stop the service because of one failed check
                log(f"ERROR during check, will retry: {e}")
            stop.wait(s["grid_check_interval"])
        log("Storm mode service stopped.")


# ------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description="Storm mode: keep the battery charged when storms are forecast")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="Only show what it would do (default)")
    mode.add_argument("--apply", action="store_true", help="Act on the inverter (no confirmation)")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as a service")
    parser.add_argument("--assume-storm", nargs="?", const=2.0, type=float, metavar="HOURS",
                        help="Pretend a storm is forecast in HOURS hours, default 2 (testing, dry run only)")
    parser.add_argument("--assume-outage", action="store_true",
                        help="Pretend the grid is down (testing, dry run only)")
    parser.add_argument("--at", metavar="'YYYY-MM-DD HH:MM'",
                        help="Pretend it is this local time (testing, dry run only)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()

    if (args.assume_storm is not None or args.assume_outage or args.at) and (args.apply or args.daemon):
        sys.exit("ERROR: --assume-storm, --assume-outage and --at are for testing (single dry run only)")

    s, tariff = load_settings(args.config)
    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    if "tou" not in regmap:
        sys.exit("ERROR: this inverter's map does not define Time Of Use slots")
    at = None
    if args.at:
        try:
            at = datetime.strptime(args.at, "%Y-%m-%d %H:%M").replace(tzinfo=s["tz"])
        except ValueError:
            sys.exit("ERROR: --at must be like '2026-10-08 15:00'")
    serial, _ = read_registers(conf, regmap)

    service = StormService(s, tariff, conf, regmap, map_path, serial, args.apply,
                           args.assume_storm, args.assume_outage, at)
    if not args.apply:
        service.state = copy.deepcopy(service.state)  # dry run: never persisted

    if args.daemon:
        service.run_forever()
        return
    try:
        service.forecast_check()
        if args.assume_outage and service.state["mode"] == "storm" and not service.state.get("outage"):
            service.grid_check()
    except Exception as e:
        sys.exit(f"ERROR during check: {e}")


if __name__ == "__main__":
    main()

