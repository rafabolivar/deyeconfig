#!/usr/bin/env python3
"""
Storm mode service: pre-charges the battery from the grid when thunderstorms or
very heavy rain are forecast, so the house is ready for a possible power outage.

Strategy (all values configurable in the [storm] section of config.toml):

  1. Planning at 'planning_time' (default 00:00, start of the off-peak period):
     if a storm is forecast within the next 'forecast_hours', plan a grid charge
     from the current SOC up to 'target_soc', finishing at 'ready_by' (07:30) or
     'storm_margin_minutes' before the storm, whichever comes first.
  2. Storm already happening (forecast for the current hour meets the criteria):
     charge immediately while the grid is available.
  3. Backup outside planning: if a storm is forecast within 'backup_hours' and
     the SOC is below 'backup_trigger_soc', charge immediately.
  4. While in storm mode, 'target_soc' is kept as the minimum battery level.
  5. If the grid goes down in storm mode, all Time Of Use slots are lowered to
     'outage_soc' so the whole battery is available. When the grid returns, the
     battery is recharged ('recharge_after_outage').
  6. 'grace_hours' after the last forecast storm hour (or if the storm is no
     longer forecast, or after 'max_hours'), the previous configuration is restored.

The inverter is only written when its configuration has to change, with a
daily limit ('max_writes_per_day').

Usage:
    python deye_storm.py                     # single check, show what it would do
    python deye_storm.py --apply             # single check, act on the inverter
    python deye_storm.py --daemon --apply    # run as a service
    python deye_storm.py --plan-now          # run the planning logic now, as if it were planning_time
    python deye_storm.py --assume-storm 18   # pretend a storm is forecast in 18 h (testing, dry run only)
    python deye_storm.py --at '2026-10-08 00:05'   # pretend it is this time (testing, dry run only)
    python deye_storm.py --assume-outage     # pretend the grid is down (testing, dry run only)
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
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deye_apply import compute_changes, write
from deye_common import DEFAULT_CONFIG, connect, load_config, read_block
from deye_export import build_profile
from deye_map import hhmm, load_map
from deye_read_config import read_registers, save_backup

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
STATE_FILE = STATE_DIR / "storm.json"
PRE_STORM_PROFILE = STATE_DIR / "pre_storm.toml"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

# Live data registers (Deye single-phase LV hybrid)
REG_BATTERY_VOLTAGE = 183   # x0.01 V
REG_SOC = 184               # %
REG_GRID_STATUS = 194       # 1 = grid connected
REG_BATTERY_CAPACITY = 204  # Ah
REG_MAX_CHARGE_CURRENT = 210
REG_GRID_CHARGE_CURRENT = 230

DEFAULTS = {
    "timezone": "Europe/Madrid",
    "forecast_interval": 900,
    "grid_check_interval": 60,
    "forecast_hours": 24,
    "planning_time": "00:00",
    "ready_by": "07:30",
    "storm_margin_minutes": 30,
    "target_soc": 80,
    "outage_soc": 15,
    "battery_nominal_voltage": 51.2,
    "charge_efficiency": 0.9,
    "storm_codes": [95, 96, 99],
    "heavy_rain_mm": 10.0,
    "min_probability": 50,
    "grace_hours": 1,
    "max_hours": 36,
    "backup_enabled": True,
    "backup_hours": 4,
    "backup_trigger_soc": 50,
    "recharge_after_outage": True,
    "max_writes_per_day": 20,
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ settings and state

def parse_hhmm(value: str, name: str, errors: list) -> dtime | None:
    try:
        h, m = (int(x) for x in str(value).split(":"))
        return dtime(h, m)
    except ValueError:
        errors.append(f"[storm] {name} must be a time like 'HH:MM' (got {value!r})")
        return None


def load_storm_settings(config_path: Path) -> dict:
    with config_path.open("rb") as f:
        s = {**DEFAULTS, **tomllib.load(f).get("storm", {})}
    errors = [f"missing [storm] {k}" for k in ("latitude", "longitude")
              if not isinstance(s.get(k), (int, float))]
    try:
        s["tz"] = ZoneInfo(s["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"[storm] unknown timezone {s['timezone']!r}")
    s["planning_t"] = parse_hhmm(s["planning_time"], "planning_time", errors)
    s["ready_by_t"] = parse_hhmm(s["ready_by"], "ready_by", errors)
    for key, minimum in (("forecast_interval", 60), ("grid_check_interval", 10)):
        if not isinstance(s[key], int) or s[key] < minimum:
            errors.append(f"[storm] {key} must be a number of seconds, at least {minimum}")
    for key in ("target_soc", "outage_soc", "backup_trigger_soc"):
        if not isinstance(s[key], int) or not 5 <= s[key] <= 100:
            errors.append(f"[storm] {key} must be a percentage between 5 and 100")
    if not 0.5 <= s["charge_efficiency"] <= 1:
        errors.append("[storm] charge_efficiency must be between 0.5 and 1")
    if errors:
        sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))
    return s


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"mode": "normal"}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def dt(value: str, tz) -> datetime:
    return datetime.fromisoformat(value).astimezone(tz)


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
        r = read_block(inv, 150, 50)        # live data: battery, SOC, grid status
        r.update(read_block(inv, 200, 81))  # configuration: battery, grid charge, TOU
        return r
    finally:
        inv.disconnect()


def charge_plan(s: dict, r: dict[int, int], now: datetime, storm_start: datetime) -> tuple[datetime, str]:
    """Return (charge start, explanation) so the battery reaches target_soc by the deadline."""
    ready = datetime.combine(now.date(), s["ready_by_t"], s["tz"])
    deadline = min(max(ready, now), storm_start - timedelta(minutes=s["storm_margin_minutes"]))
    soc = r[REG_SOC]
    capacity_kwh = r[REG_BATTERY_CAPACITY] * s["battery_nominal_voltage"] / 1000
    needed_kwh = max(0, s["target_soc"] - soc) / 100 * capacity_kwh
    current = min(r[REG_GRID_CHARGE_CURRENT], r[REG_MAX_CHARGE_CURRENT])
    power_kw = current * r[REG_BATTERY_VOLTAGE] / 100 / 1000 * s["charge_efficiency"]
    if needed_kwh == 0:
        return now, f"SOC {soc} % already at or above {s['target_soc']} %: holding"
    if power_kw <= 0:
        return now, "charge power unknown (grid charge current is 0?): charging now"
    hours = needed_kwh / power_kw
    start = deadline - timedelta(hours=hours)
    start = start.replace(minute=start.minute - start.minute % 5, second=0, microsecond=0)
    info = (f"{needed_kwh:.1f} kWh needed ({soc} % -> {s['target_soc']} %) at ~{power_kw:.1f} kW "
            f"= {hours:.1f} h, ready by {deadline:%H:%M}")
    return (start, info) if start > now else (now, info + " (starting now)")


def slot_times(start: dtime) -> list[str]:
    """6 increasing slot start times: 00:00, the charge start, then 4 more after it."""
    times = ["00:00", f"{start:%H:%M}"]
    for candidate in ("08:00", "12:00", "16:00", "20:00", "22:00", "23:00"):
        if len(times) == 6:
            break
        if candidate > times[-1]:
            times.append(candidate)
    t = datetime.combine(date.today(), start)
    while len(times) < 6:  # charge start very late in the day: fill with 5-minute steps
        t += timedelta(minutes=5)
        if f"{t:%H:%M}" > times[-1]:
            times.append(f"{t:%H:%M}")
    return times


def base_soc(previous: dict | None, now: datetime, default: int) -> int:
    """SOC of the pre-storm slot active at 'now' (normal minimum battery level)."""
    slots = (previous or {}).get("tou", [])
    active = None
    for slot in sorted(slots, key=lambda x: x["time"]):
        if slot["time"] <= f"{now:%H:%M}":
            active = slot
    if active is None and slots:
        active = max(slots, key=lambda x: x["time"])  # before the first slot: the last one wraps
    return active["soc"] if active else default


def desired_layout(s: dict, state: dict, regmap: dict, now: datetime, grid_ok: bool,
                   previous: dict | None) -> tuple[str, dict]:
    """Return (phase, profile) the inverter should have now, in storm mode."""
    n = regmap["tou"]["slots"]
    if not grid_ok or state.get("deferred"):
        phase = "outage: battery available down to outage_soc" if not grid_ok else \
                "after outage: waiting for next planning"
        return phase, {"tou": [{"slot": i + 1, "soc": s["outage_soc"]} for i in range(n)]}
    params = {"grid_charge": True, "time_of_use": "MTWTFSS"}
    start = dt(state["charge_start"], s["tz"])
    if start > now and start.date() == now.date() and start.time() > dtime(0, 0):
        times = slot_times(start.time())
        tou = [{"slot": 1, "time": times[0], "soc": base_soc(previous, now, s["outage_soc"]),
                "grid_charge": False}]
        tou += [{"slot": i + 1, "time": times[i], "soc": s["target_soc"], "grid_charge": True}
                for i in range(1, n)]
        return f"waiting: grid charge starts at {start:%H:%M}", {"parameters": params, "tou": tou}
    tou = [{"slot": i + 1, "soc": s["target_soc"], "grid_charge": True} for i in range(n)]
    return f"charging / holding at {s['target_soc']} %", {"parameters": params, "tou": tou}


# ------------------------------------------------------------------ the service

class StormService:
    def __init__(self, s, conf, regmap, map_path, serial, apply, assume_storm, assume_outage, at=None):
        self.s, self.conf, self.regmap, self.map_path, self.serial = s, conf, regmap, map_path, serial
        self.apply, self.assume_storm, self.assume_outage = apply, assume_storm, assume_outage
        self.at = at  # simulated current time (testing)
        self.state = load_state()
        self.events: list[tuple[datetime, str]] = []

    # -- helpers
    def now(self) -> datetime:
        return self.at or datetime.now(self.s["tz"])

    def persist(self) -> None:
        if self.apply:
            save_state(self.state)

    def previous_profile(self) -> dict | None:
        if PRE_STORM_PROFILE.exists():
            with PRE_STORM_PROFILE.open("rb") as f:
                return tomllib.load(f)
        return None

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
    def enter_storm(self, reason: str, charge_start: datetime, info: str, now: datetime) -> None:
        log(f"Entering storm mode: {reason}. Charge plan: {info}.")
        if self.apply:
            serial, full = read_registers(self.conf, self.regmap)
            STATE_DIR.mkdir(exist_ok=True)
            PRE_STORM_PROFILE.write_text(build_profile(serial, self.map_path, self.regmap, full))
            log(f"Current configuration saved to {PRE_STORM_PROFILE}")
        self.state.update(mode="storm", since=now.isoformat(), reason=reason,
                          storm_until=self.events[-1][0].isoformat(),
                          charge_start=charge_start.isoformat(),
                          deferred=False, outage=False, clear_checks=0)
        self.persist()

    def exit_storm(self, why: str) -> None:
        log(f"Leaving storm mode: {why}. Restoring the previous configuration.")
        previous = self.previous_profile()
        if previous is None:
            log(f"ERROR: {PRE_STORM_PROFILE} not found. Restore manually with deye_apply.py.")
        else:
            serial, full = read_registers(self.conf, self.regmap)
            if not self.apply_profile(previous, "pre-storm configuration", full):
                return  # keep storm mode and try again next check
        for key in ("since", "reason", "storm_until", "charge_start", "deferred", "outage", "clear_checks"):
            self.state.pop(key, None)
        self.state["mode"] = "normal"
        self.persist()
        log("Storm mode finished.")

    # -- periodic checks
    def forecast_check(self, plan_now: bool = False) -> None:
        s, now = self.s, self.now()
        self.events = find_storms(fetch_forecast(s), s, now)
        if self.assume_storm:
            simulated = now + timedelta(hours=self.assume_storm)
            self.events = sorted(self.events + [(simulated, "simulated storm (--assume-storm)")])
        r = read_live(self.conf)
        soc = r[REG_SOC]
        today = now.date().isoformat()
        planning_due = plan_now or (s["planning_t"] <= now.time() < s["ready_by_t"]
                                    and self.state.get("last_planning") != today)
        storm_now = any(t <= now < t + timedelta(hours=1) for t, _ in self.events)
        storm = self.state["mode"] == "storm"

        log(f"[{now:%Y-%m-%d %H:%M}] SOC {soc} % | grid {'OK' if r[REG_GRID_STATUS] == 1 else 'DOWN'} | "
            f"storm mode {'ACTIVE' if storm else 'inactive'} | next {s['forecast_hours']} h: "
            + (f"{len(self.events)} storm hour(s), first at {self.events[0][0]:%d/%m %H:%M}"
               if self.events else "no storms") + (" | planning" if planning_due else ""))
        if planning_due:
            self.state["last_planning"] = today
            self.persist()

        if not storm:
            if not self.events:
                return
            if storm_now:
                _, info = charge_plan(s, r, now, self.events[0][0])
                self.enter_storm("storm happening now", now, info.split(", ready by")[0] + ", starting now", now)
            elif planning_due:
                start, info = charge_plan(s, r, now, self.events[0][0])
                self.enter_storm(f"storm forecast at {self.events[0][0]:%d/%m %H:%M}", start, info, now)
            elif (s["backup_enabled"] and soc < s["backup_trigger_soc"]
                  and self.events[0][0] - now <= timedelta(hours=s["backup_hours"])):
                _, info = charge_plan(s, r, now, self.events[0][0])
                info = info.split(", ready by")[0] + ", starting now"
                self.enter_storm(f"backup: storm at {self.events[0][0]:%H:%M} and SOC {soc} % "
                                 f"< {s['backup_trigger_soc']} %", now, info, now)
            else:
                log("Storm forecast later; waiting for the next planning run.")
                return
            self.grid_check(r)
            return

        # ---- already in storm mode
        until = dt(self.state["storm_until"], s["tz"])
        if self.events:
            self.state["clear_checks"] = 0
            if self.events[-1][0] > until:
                until = self.events[-1][0]
                self.state["storm_until"] = until.isoformat()
            if planning_due:
                start, info = charge_plan(s, r, now, self.events[0][0])
                self.state.update(charge_start=start.isoformat(), deferred=False)
                log(f"Replanning for the next storm: {info}.")
            elif storm_now and dt(self.state["charge_start"], s["tz"]) > now:
                self.state["charge_start"] = now.isoformat()
                log("Storm arrived earlier than planned: charging now.")
        else:
            self.state["clear_checks"] = self.state.get("clear_checks", 0) + 1
        self.persist()

        restore_at = until + timedelta(hours=s["grace_hours"])
        since = dt(self.state["since"], s["tz"])
        if now > since + timedelta(hours=s["max_hours"]):
            self.exit_storm(f"active for more than {s['max_hours']} h (safety limit)")
        elif not self.events and now >= restore_at:
            self.exit_storm("the storm has passed")
        elif not self.events and until > now and self.state["clear_checks"] >= 2:
            self.exit_storm("the storm is no longer forecast")
        else:
            log(f"Storm mode active (since {since:%d/%m %H:%M}); restoring after {restore_at:%d/%m %H:%M} "
                "if no more storms are forecast.")
            self.grid_check(r)

    def grid_check(self, r: dict[int, int] | None = None) -> None:
        """In storm mode: make sure the inverter has the right Time Of Use layout."""
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
            self.state["outage"] = False
            if s["recharge_after_outage"]:
                self.state.update(charge_start=now.isoformat(), deferred=False)
                log(f"[{now:%H:%M}] Grid back: recharging to {s['target_soc']} %.")
            else:
                self.state["deferred"] = True
                log(f"[{now:%H:%M}] Grid back: recharge deferred to the next planning run.")
        self.persist()
        phase, layout = desired_layout(s, self.state, self.regmap, now, grid_ok, self.previous_profile())
        self.apply_profile(layout, f"storm mode ({phase})", r)

    def run_forever(self) -> None:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        s = self.s
        log(f"Storm mode service started: forecast every {s['forecast_interval']} s, grid every "
            f"{s['grid_check_interval']} s in storm mode, planning at {s['planning_time']}, "
            f"{'APPLY' if self.apply else 'dry run'} mode.")
        next_forecast = 0.0
        while not stop.is_set():
            try:
                planning_due = (s["planning_t"] <= self.now().time() < s["ready_by_t"]
                                and self.state.get("last_planning") != self.now().date().isoformat())
                if time.monotonic() >= next_forecast or planning_due:
                    self.forecast_check()
                    next_forecast = time.monotonic() + s["forecast_interval"]
                else:
                    self.grid_check()
            except (Exception, SystemExit) as e:  # never stop the service because of one failed check
                log(f"ERROR during check, will retry: {e}")
            stop.wait(s["grid_check_interval"])
        log("Storm mode service stopped.")


# ------------------------------------------------------------------ main

def main():
    parser = argparse.ArgumentParser(description="Storm mode: pre-charge the battery when storms are forecast")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="Only show what it would do (default)")
    mode.add_argument("--apply", action="store_true", help="Act on the inverter (no confirmation)")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as a service")
    parser.add_argument("--plan-now", action="store_true",
                        help="Run the planning logic now, as if it were planning_time")
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

    s = load_storm_settings(args.config)
    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    if "tou" not in regmap:
        sys.exit("ERROR: this inverter's map does not define Time Of Use slots")
    serial, _ = read_registers(conf, regmap)

    at = None
    if args.at:
        try:
            at = datetime.strptime(args.at, "%Y-%m-%d %H:%M").replace(tzinfo=s["tz"])
        except ValueError:
            sys.exit("ERROR: --at must be like '2026-10-07 00:05'")
    service = StormService(s, conf, regmap, map_path, serial, args.apply,
                           args.assume_storm, args.assume_outage, at)
    if not args.apply:
        service.state = copy.deepcopy(service.state)  # dry run: never persisted

    if args.daemon:
        service.run_forever()
        return
    try:
        service.forecast_check(plan_now=args.plan_now)
    except Exception as e:
        sys.exit(f"ERROR during check: {e}")


if __name__ == "__main__":
    main()

