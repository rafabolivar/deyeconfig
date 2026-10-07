#!/usr/bin/env python3
"""
Price optimizer: plans grid charging from the real hourly PVPC prices (REData),
the solar forecast (Open-Meteo), the expected consumption and the battery SOC,
and builds the Time Of Use slots that minimise the electricity cost.

Every 'interval' seconds:
  1. Read SOC, battery capacity and voltage from the inverter.
  2. Get the PVPC prices for today and tomorrow (REData, cached), the solar
     forecast on the plane of the panels and the consumption profile.
  3. Simulate the battery hour by hour and search the hours to charge from the
     grid and the hours to keep (not use) the battery that minimise the cost.
  4. Build the 6 Time Of Use slots and the grid charge current.
  5. Dry run (default): log the plan and the changes it would make.
     With --apply: write them (never while storm mode is active).

Settings: [optimizer] section of config.toml.

Usage:
    python deye_optimizer.py                     # single plan, dry run
    python deye_optimizer.py --daemon            # service, dry run (simulation)
    python deye_optimizer.py --daemon --apply    # service, writes to the inverter
"""

import argparse
import csv
import json
import math
import signal
import sys
import threading
import tomllib
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from deye_apply import compute_changes, write
from deye_common import DEFAULT_CONFIG, connect, load_config, read_block
from deye_map import load_map
from deye_read_config import read_registers, save_backup

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
PRICE_CACHE = STATE_DIR / "prices.json"
PLAN_FILE = STATE_DIR / "optimizer_plan.json"
PLAN_LOG = STATE_DIR / "optimizer_log.csv"
STORM_STATE = STATE_DIR / "storm.json"
REDATA_API = "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

REG_BATTERY_VOLTAGE = 183
REG_SOC = 184
REG_GRID_STATUS = 194
REG_BATTERY_CAPACITY = 204

# kW per hour of the day (00..23), about 12.7 kWh/day
DEFAULT_LOAD_PROFILE = [0.36] * 8 + [0.5] * 10 + [0.9] * 4 + [0.6] * 2

DEFAULTS = {
    "interval": 900,
    "pv_kwp": 3.535,
    "pv_performance": 0.54,
    "panel_tilt": 35,
    "panel_azimuth": -45,
    "min_soc": 15,
    "max_soc": 100,
    "battery_nominal_voltage": 51.2,
    "charge_efficiency": 0.95,
    "discharge_efficiency": 0.95,
    "cycle_cost": 0.01,
    "export_price": 0.04,
    "grid_power_limit": 4000,
    "max_grid_charge_current": 65,
    "max_charge_current": 90,
    "max_discharge_current": 95,
    "horizon_hours": 36,
    "load_profile": DEFAULT_LOAD_PROFILE,
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ settings

def load_settings(config_path: Path) -> dict:
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    storm = cfg.get("storm", {})
    s = {**DEFAULTS, **cfg.get("optimizer", {})}
    for key in ("latitude", "longitude", "timezone"):  # shared location with storm mode
        s.setdefault(key, storm.get(key))
    errors = [f"missing [optimizer] or [storm] {k}" for k in ("latitude", "longitude")
              if not isinstance(s.get(k), (int, float))]
    s["tz"] = ZoneInfo(s.get("timezone") or "Europe/Madrid")
    if len(s["load_profile"]) != 24:
        errors.append("[optimizer] load_profile must have 24 values (kW for each hour)")
    if not 5 <= s["min_soc"] < s["max_soc"] <= 100:
        errors.append("[optimizer] requires 5 <= min_soc < max_soc <= 100")
    if errors:
        sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))
    return s


# ------------------------------------------------------------------ prices (REData)

def fetch_day_prices(d: date, tz) -> dict[str, float]:
    url = f"{REDATA_API}?start_date={d}T00:00&end_date={d}T23:59&time_trunc=hour"
    req = urllib.request.Request(url, headers={"User-Agent": "deyeconfig", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    series = next(x for x in data["included"] if x["attributes"]["title"] == "PVPC")
    prices = {}
    for v in series["attributes"]["values"]:
        t = datetime.fromisoformat(v["datetime"]).astimezone(tz)
        if t.date() == d:
            prices[t.replace(minute=0, second=0, microsecond=0).isoformat()] = v["value"] / 1000
    return prices


class PriceCache:
    def __init__(self, tz):
        self.tz = tz
        self.prices: dict[str, float] = json.loads(PRICE_CACHE.read_text()) if PRICE_CACHE.exists() else {}

    def has_day(self, d: date) -> bool:
        return sum(1 for k in self.prices if k.startswith(d.isoformat())) >= 23  # 23-25 h (DST days)

    def update(self, now: datetime) -> None:
        days = [now.date()]
        if now.hour >= 20:  # tomorrow's PVPC is published around 20:15
            days.append(now.date() + timedelta(days=1))
        changed = False
        for d in days:
            if not self.has_day(d):
                try:
                    self.prices.update(fetch_day_prices(d, self.tz))
                    changed = True
                except Exception as e:
                    log(f"Prices for {d} not available yet ({e.__class__.__name__}).")
        if changed:
            cutoff = (now.date() - timedelta(days=7)).isoformat()
            self.prices = {k: v for k, v in self.prices.items() if k >= cutoff}
            STATE_DIR.mkdir(exist_ok=True)
            PRICE_CACHE.write_text(json.dumps(self.prices, indent=1, sort_keys=True))

    def get(self, t: datetime) -> tuple[float | None, bool]:
        """(price, estimated). Missing hours use the same hour of the previous day."""
        p = self.prices.get(t.isoformat())
        if p is not None:
            return p, False
        p = self.prices.get((t - timedelta(days=1)).isoformat())
        return p, True


# ------------------------------------------------------------------ solar forecast (Open-Meteo)

def fetch_solar(s: dict) -> dict[str, float]:
    """kWh expected in each hour (keyed by the hour start)."""
    params = {"latitude": s["latitude"], "longitude": s["longitude"], "timezone": str(s["tz"]),
              "tilt": s["panel_tilt"], "azimuth": s["panel_azimuth"],
              "hourly": "global_tilted_irradiance", "forecast_days": 3}
    with urllib.request.urlopen(f"{FORECAST_API}?{urllib.parse.urlencode(params)}", timeout=30) as resp:
        data = json.load(resp)
    h = data["hourly"]
    factor = s["pv_kwp"] * s["pv_performance"] / 1000
    out = {}
    for t, g in zip(h["time"], h["global_tilted_irradiance"]):
        # Open-Meteo radiation is the mean of the preceding hour: value at 13:00 = 12:00-13:00
        start = datetime.fromisoformat(t).replace(tzinfo=s["tz"]) - timedelta(hours=1)
        out[start.isoformat()] = (g or 0) * factor
    return out


# ------------------------------------------------------------------ simulation and optimization

class Model:
    def __init__(self, s: dict, capacity_kwh: float, voltage: float):
        self.s = s
        self.cap = capacity_kwh
        self.min = capacity_kwh * s["min_soc"] / 100
        self.max = capacity_kwh * s["max_soc"] / 100
        self.eff_c, self.eff_d = s["charge_efficiency"], s["discharge_efficiency"]
        self.max_charge_kw = s["max_charge_current"] * voltage / 1000
        self.max_discharge_kw = s["max_discharge_current"] * voltage / 1000
        self.grid_kw = s["grid_power_limit"] / 1000
        self.max_grid_charge_kw = s["max_grid_charge_current"] * voltage / 1000

    def simulate(self, hours: list[dict], soc0: float, gc: list[float], hold: list[bool], detail=False):
        soc, cost, rows = soc0, 0.0, []
        for i, h in enumerate(hours):
            imp = exp = pv_charge = grid_charge = discharge = 0.0
            start = soc
            net = h["pv"] - h["load"]
            if net >= 0:
                pv_charge = min(net, (self.max - soc) / self.eff_c, self.max_charge_kw)
                soc += pv_charge * self.eff_c
                exp = net - pv_charge
            else:
                need = -net
                if not hold[i]:
                    discharge = min(need, max(0.0, (soc - self.min) * self.eff_d), self.max_discharge_kw)
                    soc -= discharge / self.eff_d
                imp = need - discharge
            if gc[i] > 0:
                grid_charge = min(gc[i], max(0.0, (self.max - soc) / self.eff_c),
                                  max(0.0, self.max_charge_kw - pv_charge), max(0.0, self.grid_kw - imp))
                soc += grid_charge * self.eff_c
                imp += grid_charge
            cost += imp * h["price"] - exp * self.s["export_price"] + discharge * self.s["cycle_cost"]
            if detail:
                rows.append({"start_soc": start, "end_soc": soc, "import": imp, "export": exp,
                             "grid_charge": grid_charge, "discharge": discharge})
        # Energy left in the battery is worth what it would save later
        terminal = sorted(h["price"] for h in hours)[len(hours) // 2]
        cost -= (soc - self.min) * self.eff_d * terminal
        return (cost, rows) if detail else cost

    def optimize(self, hours: list[dict], soc0: float, step: float = 0.25):
        """Two-phase local search: single-hour moves first, then blocks of consecutive
        hours starting from that result (only improvements are accepted)."""
        n = len(hours)
        gc, hold = [0.0] * n, [False] * n
        best = self.simulate(hours, soc0, gc, hold)
        for max_run in (1, 8):
            gc, hold, best = self._search(hours, soc0, gc, hold, best, step, max_run)
        return gc, hold, best

    def _search(self, hours, soc0, gc, hold, best, step, max_run):
        n = len(hours)
        for _ in range(600):
            move, move_cost = None, best - 1e-4
            for i in range(n):
                if gc[i] + step <= self.max_grid_charge_kw + 1e-9:
                    gc[i] += step
                    c = self.simulate(hours, soc0, gc, hold)
                    gc[i] -= step
                    if c < move_cost:
                        move, move_cost = ("add", i), c
                if gc[i] >= step:
                    gc[i] -= step
                    c = self.simulate(hours, soc0, gc, hold)
                    gc[i] += step
                    if c < move_cost:
                        move, move_cost = ("remove", i), c
                # Keep (hold) or release the battery over runs of consecutive hours: holding a
                # single hour only shifts its use to the next hour, so blocks are needed
                for length in range(1, max_run + 1):
                    if i + length > n:
                        break
                    for value in (True, False):
                        if all(hold[k] == value for k in range(i, i + length)):
                            continue
                        saved = hold[i:i + length]
                        hold[i:i + length] = [value] * length
                        c = self.simulate(hours, soc0, gc, hold)
                        hold[i:i + length] = saved
                        if c < move_cost:
                            move, move_cost = ("hold", i, length, value), c
            if move is None:
                break
            if move[0] == "add":
                gc[move[1]] += step
            elif move[0] == "remove":
                gc[move[1]] -= step
            else:
                _, i, length, value = move
                hold[i:i + length] = [value] * length
            best = move_cost
        return gc, hold, best


# ------------------------------------------------------------------ Time Of Use layout

def build_slots(hours, rows, gc, hold, model, s, n_slots=6):
    """Map the next 24 hours to a daily table of n_slots Time Of Use slots."""
    pct = lambda kwh: round(kwh / model.cap * 100)
    by_hour = {}
    for k, h in enumerate(hours[:24]):
        if rows[k]["grid_charge"] > 0.01:
            entry = ("charge", min(s["max_soc"], max(pct(rows[k]["end_soc"]), s["min_soc"])))
        elif hold[k]:
            entry = ("hold", max(s["min_soc"], pct(rows[k]["start_soc"])))
        else:
            entry = ("use", s["min_soc"])
        by_hour[h["time"].hour] = entry
    seq = [by_hour.get(hr, ("use", s["min_soc"])) for hr in range(24)]

    segs = []  # [start_hour, action, soc, length]
    for hr, (action, soc) in enumerate(seq):
        if segs and segs[-1][1] == action and abs(segs[-1][2] - soc) <= 5:
            segs[-1][3] += 1
            if action == "charge":
                segs[-1][2] = max(segs[-1][2], soc)
        else:
            segs.append([hr, action, soc, 1])
    while len(segs) > n_slots:  # merge the shortest segment into a neighbour
        i = min(range(len(segs)), key=lambda j: (segs[j][3], segs[j][1] == "charge"))
        j = i - 1 if i > 0 else 1
        keep, drop = (segs[j], segs[i]) if segs[j][1] == "charge" or segs[i][1] != "charge" else (segs[i], segs[j])
        start = min(segs[i][0], segs[j][0])
        merged = [start, keep[1], keep[2], segs[i][3] + segs[j][3]]
        lo = min(i, j)
        segs[lo:lo + 2] = [merged]
    while len(segs) < n_slots:  # split the longest segment (same action)
        i = max(range(len(segs)), key=lambda j: segs[j][3])
        a = segs[i]
        half = a[3] // 2
        segs[i:i + 1] = [[a[0], a[1], a[2], half], [a[0] + half, a[1], a[2], a[3] - half]]
    return [{"slot": k + 1, "time": f"{seg[0]:02d}:00", "soc": seg[2], "grid_charge": seg[1] == "charge",
             "action": seg[1]} for k, seg in enumerate(segs)]


# ------------------------------------------------------------------ the optimizer

class Optimizer:
    def __init__(self, s, conf, regmap, map_path, apply):
        self.s, self.conf, self.regmap, self.map_path, self.apply = s, conf, regmap, map_path, apply
        self.prices = PriceCache(s["tz"])
        self.last_slots = None

    def read_live(self) -> dict[int, int]:
        inv = connect(self.conf, quiet=True)
        try:
            r = read_block(inv, 150, 50)
            r.update(read_block(inv, 200, 81))
            r.update(read_block(inv, 280, 20))
            return r
        finally:
            inv.disconnect()

    def plan(self) -> None:
        s = self.s
        now = datetime.now(s["tz"])
        start = now.replace(minute=0, second=0, microsecond=0)
        self.prices.update(now)
        solar = fetch_solar(s)
        r = self.read_live()
        voltage = r[REG_BATTERY_VOLTAGE] / 100 or s["battery_nominal_voltage"]
        model = Model(s, r[REG_BATTERY_CAPACITY] * s["battery_nominal_voltage"] / 1000, voltage)
        soc0 = model.cap * r[REG_SOC] / 100

        hours, estimated = [], 0
        for k in range(s["horizon_hours"]):
            t = start + timedelta(hours=k)
            price, est = self.prices.get(t)
            if price is None:
                break
            estimated += est
            frac = 1 - now.minute / 60 if k == 0 else 1  # remaining part of the current hour
            hours.append({"time": t, "price": price, "pv": solar.get(t.isoformat(), 0.0) * frac,
                          "load": s["load_profile"][t.hour] * frac, "estimated": est})
        if len(hours) < 6:
            log("Not enough price data to plan; skipping this cycle.")
            return

        gc, hold, cost = model.optimize(hours, soc0)
        cost, rows = model.simulate(hours, soc0, gc, hold, detail=True)
        baseline = model.simulate(hours, soc0, [0.0] * len(hours), [False] * len(hours))
        slots = build_slots(hours, rows, gc, hold, model, s)
        need_kw = max([rows[k]["grid_charge"] for k in range(min(24, len(rows)))] + [0])
        current = min(s["max_grid_charge_current"], max(10, math.ceil(need_kw * 1000 / voltage / 5) * 5))

        changed = slots != self.last_slots
        log(f"[{now:%Y-%m-%d %H:%M}] SOC {r[REG_SOC]} % | horizon {len(hours)} h"
            + (f" ({estimated} h with estimated prices)" if estimated else "")
            + f" | cost {cost:.2f} EUR vs {baseline:.2f} without plan (saving {baseline - cost:.2f})"
            + ("" if changed else " | plan unchanged"))
        if changed:
            log("  hour  EUR/kWh  solar  load  action   SOC")
            for k, h in enumerate(hours[:24]):
                act = ("charge" if rows[k]["grid_charge"] > 0.01 else "hold" if hold[k] else "use")
                extra = f" +{rows[k]['grid_charge']:.1f} kWh" if act == "charge" else ""
                log(f"  {h['time']:%d %H}h  {h['price']:.3f}  {h['pv']:4.1f}  {h['load']:4.1f}  {act:7} "
                    f"{round(rows[k]['end_soc'] / model.cap * 100):3d} %{extra}")
            log("  Time Of Use: " + " | ".join(f"{x['time']} {x['action']} {x['soc']}%" for x in slots)
                + f" | grid charge current {current} A")
        self.last_slots = slots

        profile = {"parameters": {"grid_charge": True, "time_of_use": "MTWTFSS",
                                  "grid_charge_current": current,
                                  "grid_peak_shaving": True, "grid_peak_shaving_power": s["grid_power_limit"]},
                   "tou": [{k: v for k, v in x.items() if k != "action"} for x in slots]}
        proposed, changes, errors, _ = compute_changes(profile, self.regmap, r)
        if errors:
            log("ERRORS in the plan (nothing written): " + "; ".join(errors))
            return
        storm = json.loads(STORM_STATE.read_text()).get("mode") == "storm" if STORM_STATE.exists() else False
        if changes and changed:
            log(f"  {len(changes)} change(s) vs the inverter" + (": " + "; ".join(
                f"{n} {a}->{b}" for n, a, b in changes[:8]) + (" ..." if len(changes) > 8 else "")))
        if changes and self.apply and not storm and r[REG_GRID_STATUS] == 1:
            save_backup("optimizer", self.map_path, r, reason="optimizer plan")
            failures = write(self.conf, r, proposed)
            log("  Plan written." if not failures else "  WARNING: " + "; ".join(failures))
        elif changes and self.apply and storm:
            log("  Storm mode active: not writing.")

        STATE_DIR.mkdir(exist_ok=True)
        PLAN_FILE.write_text(json.dumps({
            "time": now.isoformat(), "soc": r[REG_SOC], "cost": cost, "baseline": baseline,
            "grid_charge_current": current, "slots": slots,
            "hours": [{"time": h["time"].isoformat(), "price": h["price"], "pv": round(h["pv"], 2),
                       "load": round(h["load"], 2), "grid_charge": round(rows[k]["grid_charge"], 2),
                       "hold": hold[k], "soc": round(rows[k]["end_soc"] / model.cap * 100)}
                      for k, h in enumerate(hours)]}, indent=1))
        new_file = not PLAN_LOG.exists()
        with PLAN_LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "soc", "cost", "baseline", "saving", "grid_charge_current", "slots", "changes"])
            w.writerow([now.isoformat(timespec="minutes"), r[REG_SOC], round(cost, 3), round(baseline, 3),
                        round(baseline - cost, 3), current,
                        " ".join(f"{x['time']}/{x['action']}/{x['soc']}" for x in slots), len(changes)])

    def run_forever(self) -> None:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        log(f"Price optimizer started: every {self.s['interval']} s, "
            f"{'APPLY' if self.apply else 'dry run (simulation)'} mode.")
        while not stop.is_set():
            try:
                self.plan()
            except (Exception, SystemExit) as e:
                log(f"ERROR during planning, will retry: {e}")
            stop.wait(self.s["interval"])
        log("Price optimizer stopped.")


def main():
    parser = argparse.ArgumentParser(description="Plan grid charging from real PVPC prices, solar and SOC")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="Only show the plan (default)")
    mode.add_argument("--apply", action="store_true", help="Write the plan to the inverter")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as a service")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()

    s = load_settings(args.config)
    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    opt = Optimizer(s, conf, regmap, map_path, args.apply)
    if args.daemon:
        opt.run_forever()
    else:
        try:
            opt.plan()
        except Exception as e:
            sys.exit(f"ERROR during planning: {e}")


if __name__ == "__main__":
    main()

