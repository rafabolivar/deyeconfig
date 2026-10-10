#!/usr/bin/env python3
"""
Automatic mode: plans the battery from the real hourly PVPC prices (REData), the
solar forecast, the expected consumption and the SOC, with storm protection, and
writes the Time Of Use slots to the inverter.

Loop:
  - every [storm] grid_check_interval seconds (60): grid check. If the grid goes
    down, all slots are lowered to outage_soc at once (whole battery available);
    when it returns, the plan is recalculated immediately.
  - every [optimizer] interval seconds (900): full plan.
      1. Read SOC, battery capacity and voltage.
      2. PVPC prices for today and tomorrow (cached; missing hours use the previous
         day's price, then the [tariff] fallback prices), and one Open-Meteo call for
         the solar forecast on the panels and the storm forecast.
      3. Storm protection: several weather models are compared to rate each storm
         hour (high / medium confidence). From reserve_lead_hours before it until
         grace_hours after it, the battery must stay at or above reserve_soc (high) or
         reserve_soc_medium (medium). Being below has a high cost (shortfall_penalty),
         so the plan charges in the cheapest hours before the storm.
      4. Linear programming finds the optimal hours to charge from the grid and to
         keep the battery (losses, wear and value of the energy left included).
      5. Build the 6 Time Of Use slots (SOC in steps of soc_step %) and the grid
         charge current, and write them only if they differ from the inverter.

Usage:
    python deye_optimizer.py                    # single plan, dry run
    python deye_optimizer.py --apply            # single plan, write it
    python deye_optimizer.py --daemon --apply   # service
    python deye_optimizer.py --assume-storm 6   # dry run with a storm in 6 h (testing)
    python deye_optimizer.py --assume-outage    # dry run with the grid down (testing)
    python deye_optimizer.py --test-mode 4h     # relax the plan write limits for 4 hours (development)
    python deye_optimizer.py --reset-write-count
"""

import argparse
import csv
import json
import math
import signal
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import linprog

from deye_apply import compute_changes, write
from deye_common import DEFAULT_CONFIG, connect, load_config, read_block
from deye_map import load_map
from deye_read_config import save_backup
from deye_tariff import Tariff

BASE = Path(__file__).parent
STATE_DIR = BASE / "state"
STATE_FILE = STATE_DIR / "optimizer.json"
TEST_MODE_FILE = STATE_DIR / "test_mode.json"      # written by --test-mode, read by the service
RESET_WRITES_FILE = STATE_DIR / "reset_writes"     # written by --reset-write-count
PRICE_CACHE = STATE_DIR / "prices.json"
SPOT_CACHE = STATE_DIR / "spot.json"  # wholesale market price per hour (value of exported energy)
PLAN_FILE = STATE_DIR / "optimizer_plan.json"
PLAN_LOG = STATE_DIR / "optimizer_log.csv"
REDATA_API = "https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"

REG_LOAD_POWER = 178
REG_BATTERY_VOLTAGE = 183
REG_PV1_POWER = 186
REG_PV2_POWER = 187
REG_GRID_CHARGE_CURRENT = 230
REG_SOC = 184
REG_GRID_STATUS = 194
REG_BATTERY_CAPACITY = 204

DEFAULT_LOAD_PROFILE = [0.36] * 8 + [0.5] * 10 + [0.9] * 4 + [0.6] * 2  # kW, about 12.7 kWh/day

OPTIMIZER_DEFAULTS = {
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
    "soc_step": 5,
    "min_saving": 0.03,
    "free_charge_price": 0.01,
    "plan_writes_per_hour": 4,
    "plan_writes_per_day": 30,
    "charge_writes_per_hour": 4,
    "charge_writes_per_day": 40,
    "test_writes_per_day": 100,
    "loop_max_repeats": 3,
    "loop_window_hours": 2,
    "storm_priority_hours": 6,
    "charge_control_minutes": 10,
    "late_charge_preference": 0.0001,
    "pv_correction_min_hours": 2,
    "price_warning_hour": 23,
    "load_profile": DEFAULT_LOAD_PROFILE,
}
STORM_DEFAULTS = {
    "enabled": True,
    "timezone": "Europe/Madrid",
    "grid_check_interval": 60,
    "models": ["ecmwf_ifs025", "icon_seamless", "meteofrance_seamless", "gfs_seamless", "ukmo_seamless"],
    "storm_codes": [95, 96, 99],
    "heavy_rain_mm": 10.0,
    "min_models": 2,
    "cape_high": 1000,
    "cape_medium": 500,
    "reserve_soc": 80,
    "reserve_soc_medium": 50,
    "reserve_lead_hours": 0.5,
    "grace_hours": 1,
    "outage_soc": 15,
    "shortfall_penalty": 2.0,
}
DEFAULT_FALLBACK_PRICES = {"off-peak": 0.18, "mid": 0.15, "peak": 0.25}


def s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ settings and state

def load_settings(config_path: Path) -> tuple[dict, dict, Tariff, dict]:
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    s = {**OPTIMIZER_DEFAULTS, **cfg.get("optimizer", {})}
    if "max_writes_per_day" in cfg.get("optimizer", {}) and "plan_writes_per_day" not in cfg.get("optimizer", {}):
        s["plan_writes_per_day"] = cfg["optimizer"]["max_writes_per_day"]  # older configurations
    st = {**STORM_DEFAULTS, **cfg.get("storm", {})}
    for key in ("latitude", "longitude", "timezone"):  # location: [optimizer] or [storm]
        s.setdefault(key, st.get(key))
    errors = [f"missing [storm] {k}" for k in ("latitude", "longitude")
              if not isinstance(s.get(k), (int, float))]
    s["tz"] = ZoneInfo(s["timezone"])
    if len(s["load_profile"]) != 24:
        errors.append("[optimizer] load_profile must have 24 values (kW for each hour)")
    if not 5 <= s["min_soc"] < s["max_soc"] <= 100:
        errors.append("[optimizer] requires 5 <= min_soc < max_soc <= 100")
    for key in ("reserve_soc", "reserve_soc_medium"):
        if not s["min_soc"] <= st[key] <= s["max_soc"]:
            errors.append(f"[storm] {key} must be between [optimizer] min_soc and max_soc")
    if not isinstance(st["grid_check_interval"], int) or st["grid_check_interval"] < 10:
        errors.append("[storm] grid_check_interval must be at least 10 seconds")
    if errors:
        sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))
    tariff_cfg = cfg.get("tariff", {})
    fallback = {**DEFAULT_FALLBACK_PRICES, **tariff_cfg.get("fallback_prices", {})}
    return s, st, Tariff(tariff_cfg, s["tz"]), fallback


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------------ prices (REData)

def fetch_day_prices(d: date, tz) -> tuple[dict[str, float], dict[str, float]]:
    """PVPC price (tolls and charges included) and wholesale market price (spot, used to value
    exported energy), in EUR/kWh per hour. The spot price comes in 15-minute steps: hourly mean."""
    url = f"{REDATA_API}?start_date={d}T00:00&end_date={d}T23:59&time_trunc=hour"
    req = urllib.request.Request(url, headers={"User-Agent": "deyeconfig", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    series = {x["attributes"]["title"]: x["attributes"]["values"] for x in data["included"]}
    def hourly(values):
        sums: dict[str, list[float]] = {}
        for v in values:
            t = datetime.fromisoformat(v["datetime"]).astimezone(tz)
            if t.date() == d:
                sums.setdefault(t.replace(minute=0, second=0, microsecond=0).isoformat(), []).append(v["value"] / 1000)
        return {k: sum(x) / len(x) for k, x in sums.items()}
    spot_key = next((k for k in series if k.lower().startswith("precio mercado spot")), None)
    return hourly(series["PVPC"]), hourly(series[spot_key]) if spot_key else {}


def valid_day(prices: dict[str, float]) -> bool:
    """A day is valid with 23-25 hourly prices (DST days) that are not a placeholder.
    REData sometimes publishes a day filled with zeros before the real prices. Real prices
    can be close to zero or even slightly negative (sunny weekends), so low values are
    accepted; what is rejected is the placeholder pattern: several exact zeros or a flat day."""
    values = list(prices.values())
    if not 23 <= len(values) <= 25:
        return False
    exact_zeros = sum(1 for v in values if v == 0)
    return exact_zeros < 6 and max(values) - min(values) > 1e-6


class PriceCache:
    def __init__(self, tz, tariff: Tariff, fallback: dict, warning_hour: int = 23):
        self.tz, self.tariff, self.fallback = tz, tariff, fallback
        self.warning_hour = warning_hour
        self.warned: set[str] = set()  # days already warned about
        self.prices: dict[str, float] = json.loads(PRICE_CACHE.read_text()) if PRICE_CACHE.exists() else {}
        self.spot: dict[str, float] = json.loads(SPOT_CACHE.read_text()) if SPOT_CACHE.exists() else {}

    def day(self, d: date) -> dict[str, float]:
        return {k: v for k, v in self.prices.items() if k.startswith(d.isoformat())}

    def has_day(self, d: date) -> bool:
        return valid_day(self.day(d))

    def update(self, now: datetime) -> None:
        days = [now.date()] + ([now.date() + timedelta(days=1)] if now.hour >= 20 else [])
        changed = False
        for d in days:  # tomorrow's PVPC is published around 20:15
            if not self.has_day(d):
                for k in self.day(d):  # drop invalid or incomplete data for that day
                    self.prices.pop(k)
                    changed = True
                try:
                    fetched, spot = fetch_day_prices(d, self.tz)
                except Exception as e:
                    log(f"Prices for {d} not available yet ({e.__class__.__name__}).")
                    continue
                if valid_day(fetched):
                    self.prices.update(fetched)
                    self.spot.update(spot)
                    changed = True
                    log(f"PVPC prices for {d} received.")
                else:
                    log(f"Prices for {d} not valid yet (REData returned {len(fetched)} values, "
                        f"min {min(fetched.values(), default=0):.3f}); using estimates.")
        # Traceability: warn once if tomorrow's prices are still missing late in the evening
        tomorrow = now.date() + timedelta(days=1)
        if now.hour >= self.warning_hour and not self.has_day(tomorrow) and tomorrow.isoformat() not in self.warned:
            log(f"WARNING: PVPC prices for {tomorrow} still not available at {now:%H:%M}; "
                f"the plan uses estimates (previous day / tariff) until they arrive.")
            self.warned.add(tomorrow.isoformat())
        if changed:
            cutoff = (now.date() - timedelta(days=7)).isoformat()
            self.prices = {k: v for k, v in self.prices.items() if k >= cutoff}
            self.spot = {k: v for k, v in self.spot.items() if k >= cutoff}
            STATE_DIR.mkdir(exist_ok=True)
            PRICE_CACHE.write_text(json.dumps(self.prices, indent=1, sort_keys=True))
            SPOT_CACHE.write_text(json.dumps(self.spot, indent=1, sort_keys=True))

    def export_value(self, t: datetime, price: float, max_value: float) -> float:
        """Value of 1 kWh exported in hour t: the market price (never negative); if unknown,
        the PVPC price capped at max_value."""
        spot = self.spot.get(t.isoformat())
        if spot is not None:
            return max(0.0, spot)
        return min(max_value, max(0.0, price))

    def get(self, t: datetime) -> tuple[float, str]:
        """(price, source): 'pvpc', 'previous day' or 'tariff'."""
        p = self.prices.get(t.isoformat())
        if p is not None:
            return p, "pvpc"
        p = self.prices.get((t - timedelta(days=1)).isoformat())
        if p is not None:
            return p, "previous day"
        return self.fallback[self.tariff.period_at(t)[0]], "tariff"


# ------------------------------------------------------------------ weather (Open-Meteo)

def fetch_solar(s: dict) -> dict[str, float]:
    """Expected solar kWh per hour start (irradiance on the plane of the panels)."""
    params = {"latitude": s["latitude"], "longitude": s["longitude"], "timezone": s["timezone"],
              "tilt": s["panel_tilt"], "azimuth": s["panel_azimuth"], "forecast_days": 3,
              "hourly": "global_tilted_irradiance"}
    with urllib.request.urlopen(f"{FORECAST_API}?{urllib.parse.urlencode(params)}", timeout=30) as resp:
        h = json.load(resp)["hourly"]
    factor = s["pv_kwp"] * s["pv_performance"] / 1000
    # Radiation is the mean of the preceding hour: value at 13:00 = 12:00-13:00
    return {(datetime.fromisoformat(t).replace(tzinfo=s["tz"]) - timedelta(hours=1)).isoformat(): (g or 0) * factor
            for t, g in zip(h["time"], h["global_tilted_irradiance"])}


def fetch_storm_models(s: dict, st: dict) -> dict[str, dict[str, dict]]:
    """Per hour start and model: weather code, rain (mm in that hour) and CAPE (J/kg)."""
    params = {"latitude": s["latitude"], "longitude": s["longitude"], "timezone": s["timezone"],
              "forecast_days": 3, "hourly": "weather_code,precipitation,cape", "models": ",".join(st["models"])}
    with urllib.request.urlopen(f"{FORECAST_API}?{urllib.parse.urlencode(params)}", timeout=30) as resp:
        h = json.load(resp)["hourly"]
    out: dict[str, dict[str, dict]] = {}
    for i, t in enumerate(h["time"]):
        ts = datetime.fromisoformat(t).replace(tzinfo=s["tz"])
        prev = (ts - timedelta(hours=1)).isoformat()  # rain is the sum of the preceding hour
        for m in st["models"]:
            code, rain, cape = (h.get(f"{k}_{m}", [None] * (i + 1))[i] for k in ("weather_code", "precipitation", "cape"))
            out.setdefault(ts.isoformat(), {}).setdefault(m, {}).update(code=code, cape=cape)
            out.setdefault(prev, {}).setdefault(m, {})["rain"] = rain
    return out


def storm_confidence(models: dict[str, dict[str, dict]], t: datetime, st: dict) -> tuple[str | None, str]:
    """('high' | 'medium' | None, explanation) for the hour starting at t, looking at t-1h..t+1h."""
    thunder, heavy, cape = set(), set(), {}
    for dt_ in (-1, 0, 1):
        for m, v in models.get((t + timedelta(hours=dt_)).isoformat(), {}).items():
            if v.get("code") in st["storm_codes"]:
                thunder.add(m)
                cape[m] = max(cape.get(m, 0), v.get("cape") or 0)
            if dt_ == 0 and (v.get("rain") or 0) >= st["heavy_rain_mm"]:
                heavy.add(m)
    max_cape = max(cape.values(), default=0)
    short = lambda ms: ",".join(sorted(x.split("_")[0] for x in ms))
    info = (f"thunderstorm in {short(thunder) or '-'}, heavy rain in {short(heavy) or '-'}, "
            f"CAPE {max_cape:.0f}")
    if len(thunder) >= st["min_models"] or len(heavy) >= st["min_models"] or (thunder and max_cape >= st["cape_high"]):
        return "high", info
    if (thunder and max_cape >= st["cape_medium"]) or heavy:
        return "medium", info
    return None, info


# ------------------------------------------------------------------ battery model and search

class Model:
    def __init__(self, s: dict, st: dict, capacity_kwh: float, voltage: float):
        self.s, self.st = s, st
        self.cap = capacity_kwh
        self.min = capacity_kwh * s["min_soc"] / 100
        self.max = capacity_kwh * s["max_soc"] / 100
        self.eff_c, self.eff_d = s["charge_efficiency"], s["discharge_efficiency"]
        self.max_charge_kw = s["max_charge_current"] * voltage / 1000
        self.max_discharge_kw = s["max_discharge_current"] * voltage / 1000
        self.grid_kw = s["grid_power_limit"] / 1000
        self.max_grid_charge_kw = s["max_grid_charge_current"] * voltage / 1000

    def simulate(self, hours, soc0, gc, hold, detail=False):
        soc, cost, rows = soc0, 0.0, []
        for i, h in enumerate(hours):
            floor = max(self.min, h["reserve"])
            imp = exp = pv_charge = grid_charge = discharge = 0.0
            start = soc
            net = h["pv"] - h["load"]
            if net >= 0:
                pv_charge = min(net, max(0.0, (self.max - soc) / self.eff_c), self.max_charge_kw * h["frac"])
                soc += pv_charge * self.eff_c
                exp = net - pv_charge
            else:
                need = -net
                if not hold[i]:
                    discharge = min(need, max(0.0, (soc - floor) * self.eff_d), self.max_discharge_kw * h["frac"])
                    soc -= discharge / self.eff_d
                imp = need - discharge
            if gc[i] > 0:
                grid_charge = min(gc[i] * h["frac"], max(0.0, (self.max - soc) / self.eff_c),
                                  max(0.0, self.max_charge_kw * h["frac"] - pv_charge),
                                  max(0.0, self.grid_kw * h["frac"] - imp))
                soc += grid_charge * self.eff_c
                imp += grid_charge
            cost += imp * h["price"] - exp * h.get("export", min(self.s["export_price"], max(h["price"], 0.0))) + discharge * self.s["cycle_cost"]
            if soc < h["reserve"] - 1e-6:  # storm protection: being below the reserve is expensive
                cost += (h["reserve"] - soc) * self.st["shortfall_penalty"]
            if detail:
                rows.append({"start_soc": start, "end_soc": soc, "import": imp, "export": exp,
                             "grid_charge": grid_charge, "discharge": discharge})
        terminal = sorted(h["price"] for h in hours)[len(hours) // 2]  # value of the energy left
        cost -= (soc - self.min) * self.eff_d * terminal
        return (cost, rows) if detail else cost

    def simulate_layout(self, hours, soc0, layout) -> float:
        """Cost of a Time Of Use layout as the inverter would apply it. layout: per hour
        (floor kWh, grid charge target kWh or None). Same cost terms as the plan."""
        soc, cost = soc0, 0.0
        for h, (floor, target) in zip(hours, layout):
            floor = max(self.min, floor)
            f = h["frac"]
            imp = exp = pv_charge = discharge = 0.0
            net = h["pv"] - h["load"]
            if net >= 0:
                pv_charge = min(net, max(0.0, (self.max - soc) / self.eff_c), self.max_charge_kw * f)
                soc += pv_charge * self.eff_c
                exp = net - pv_charge
            else:
                need = -net
                discharge = min(need, max(0.0, (soc - floor) * self.eff_d), self.max_discharge_kw * f)
                soc -= discharge / self.eff_d
                imp = need - discharge
            if target is not None and soc < target:
                g = min(self.max_grid_charge_kw * f, (target - soc) / self.eff_c,
                        max(0.0, self.max_charge_kw * f - pv_charge), max(0.0, self.grid_kw * f - imp))
                soc += g * self.eff_c
                imp += g
            cost += imp * h["price"] - exp * h.get("export", min(self.s["export_price"], max(h["price"], 0.0))) + discharge * self.s["cycle_cost"]
            if soc < h["reserve"] - 1e-6:
                cost += (h["reserve"] - soc) * self.st["shortfall_penalty"]
        terminal = sorted(h["price"] for h in hours)[len(hours) // 2]
        return cost - (soc - self.min) * self.eff_d * terminal

    def optimize(self, hours, soc0, no_discharge=()):
        """Optimal plan by linear programming (HiGHS). Returns per hour: grid charge (kWh),
        battery discharge (kWh) and SOC at the end of the hour (kWh)."""
        n = len(hours)
        price = np.array([h["price"] for h in hours])
        surplus = np.array([max(h["pv"] - h["load"], 0.0) for h in hours])
        deficit = np.array([max(h["load"] - h["pv"], 0.0) for h in hours])
        frac = np.array([h["frac"] for h in hours])
        reserve = np.array([h["reserve"] for h in hours])
        terminal = sorted(price)[n // 2]  # value of the energy left in the battery at the end
        G, D, C, S, SH = (k * n for k in range(5))  # grid charge, discharge, solar charge, SOC, shortfall
        N = 5 * n
        cost = np.zeros(N)
        # tiny preference for charging later at equal price: leaves room for the sun before
        cost[G:G + n] = price + self.s["late_charge_preference"] * (n - np.arange(n))
        cost[D:D + n] = -price + self.s["cycle_cost"]
        export = np.array([h.get("export", min(self.s["export_price"], max(h["price"], 0.0))) for h in hours])
        cost[C:C + n] = export  # solar stored instead of exported
        cost[SH:SH + n] = self.st["shortfall_penalty"]
        cost[S + n - 1] = -self.eff_d * terminal
        A_eq, b_eq, A_ub, b_ub = [], [], [], []
        for t in range(n):
            row = np.zeros(N)  # soc_t = soc_t-1 + eff_c (solar + grid) - discharge / eff_d
            row[S + t], row[C + t], row[G + t], row[D + t] = 1, -self.eff_c, -self.eff_c, 1 / self.eff_d
            if t:
                row[S + t - 1] = -1
            A_eq.append(row)
            b_eq.append(soc0 if t == 0 else 0.0)
            row = np.zeros(N)  # battery charge current limit
            row[C + t] = row[G + t] = 1
            A_ub.append(row)
            b_ub.append(self.max_charge_kw * frac[t])
            row = np.zeros(N)  # grid power limit: house import + grid charge
            row[G + t], row[D + t] = 1, -1
            A_ub.append(row)
            b_ub.append(max(0.0, self.grid_kw * frac[t] - deficit[t]))
            row = np.zeros(N)  # shortfall >= reserve - soc
            row[S + t], row[SH + t] = -1, -1
            A_ub.append(row)
            b_ub.append(-reserve[t])
        bounds = ([(0, self.max_grid_charge_kw * f) for f in frac]
                  + [(0, 0 if t in no_discharge else min(dd, self.max_discharge_kw * f))
                     for t, (dd, f) in enumerate(zip(deficit, frac))]
                  + [(0, min(ss, self.max_charge_kw * f)) for ss, f in zip(surplus, frac)]
                  # if the battery is already above max_soc (e.g. charged by the sun), allow it
                  + [(self.min, max(self.max, soc0))] * n + [(0, None)] * n)
        res = linprog(cost, A_ub=np.array(A_ub), b_ub=b_ub, A_eq=np.array(A_eq), b_eq=b_eq,
                      bounds=bounds, method="highs")
        if not res.success:
            raise RuntimeError(f"optimization failed: {res.message}")
        x = res.x
        return x[G:G + n], x[D:D + n], x[S:S + n], deficit


# ------------------------------------------------------------------ Time Of Use layout

def hour_actions(gc, dis, deficit, threshold=0.05):
    """charge / hold / use for each hour of the plan."""
    return ["charge" if g > threshold else "hold" if d_ > threshold and dc < d_ * 0.5 else "use"
            for g, dc, d_ in zip(gc, dis, deficit)]


def build_slots(hours, actions, soc, soc0, model, s, n_slots=6):
    """Map the next 24 hours to a daily table of n_slots Time Of Use slots."""
    step = s["soc_step"]
    up = lambda kwh: min(s["max_soc"], max(s["min_soc"], math.ceil(kwh / model.cap * 100 / step - 1e-9) * step))
    by_hour = {}
    for k, h in enumerate(hours[:24]):
        floor = up(max(model.min, h["reserve"]))
        start = soc0 if k == 0 else soc[k - 1]
        if actions[k] == "charge":
            entry = ("charge", max(up(soc[k]), floor))
        elif actions[k] == "hold":
            entry = ("hold", max(up(start), floor))
        else:
            entry = ("use", floor)
        by_hour[h["time"].hour] = entry
    seq = [by_hour.get(hr, ("use", s["min_soc"])) for hr in range(24)]

    segs = []  # [start_hour, action, soc, length]
    for hr, (action, soc) in enumerate(seq):
        if segs and segs[-1][1] == action and segs[-1][2] == soc:
            segs[-1][3] += 1
        elif segs and segs[-1][1] == action == "charge":
            segs[-1][2] = max(segs[-1][2], soc)
            segs[-1][3] += 1
        else:
            segs.append([hr, action, soc, 1])
    def merge_cost(A, B):
        """(damage, merged segment) of joining two neighbouring segments."""
        start, length = A[0], A[3] + B[3]
        if A[1] == B[1]:
            return 0, [start, A[1], max(A[2], B[2]), length]
        kinds = {A[1], B[1]}
        if kinds == {"hold", "use"}:  # drop the hold: the battery can simply be used
            hold_seg, use_seg = (A, B) if A[1] == "hold" else (B, A)
            return hold_seg[3], [start, "use", use_seg[2], length]
        charge, other = (A, B) if A[1] == "charge" else (B, A)
        # extending a charge over other hours buys energy at hours the plan did not choose
        return 3 * other[3], [start, "charge", charge[2], length]

    while len(segs) > n_slots:  # join the neighbouring pair that damages the plan least
        best = min(range(len(segs) - 1), key=lambda k: merge_cost(segs[k], segs[k + 1])[0])
        segs[best:best + 2] = [merge_cost(segs[best], segs[best + 1])[1]]
    while len(segs) < n_slots:  # the inverter needs n_slots increasing times: split the longest
        i = max(range(len(segs)), key=lambda j: segs[j][3])
        a = segs[i]
        half = a[3] // 2
        segs[i:i + 1] = [[a[0], a[1], a[2], half], [a[0] + half, a[1], a[2], a[3] - half]]
    return [{"slot": k + 1, "time": f"{seg[0]:02d}:00", "soc": seg[2], "grid_charge": seg[1] == "charge",
             "action": seg[1]} for k, seg in enumerate(segs)]


def inverter_slots(r: dict, tou: dict) -> list[dict]:
    """Time Of Use slots currently in the inverter, in the same format as build_slots."""
    out = []
    for i in range(tou["slots"]):
        hhmm = r[tou["time"] + i]
        charge = bool(r[tou["charge"] + i] & 1)
        out.append({"slot": i + 1, "time": f"{hhmm // 100:02d}:{hhmm % 100:02d}", "soc": r[tou["soc"] + i],
                    "grid_charge": charge, "action": "charge" if charge else "inverter"})
    return out


def slots_to_layout(slots: list[dict], hours: list[dict], cap: float) -> list[tuple[float, float | None]]:
    """Per hour of the plan: (floor kWh, grid charge target kWh or None) from a slot table."""
    starts = [int(x["time"][:2]) * 60 + int(x["time"][3:]) for x in slots]
    layout = []
    for h in hours:
        minute = h["time"].hour * 60 + h["time"].minute
        k = max((i for i, st_ in enumerate(starts) if st_ <= minute), default=len(slots) - 1)
        soc = cap * slots[k]["soc"] / 100
        layout.append((soc, soc if slots[k]["grid_charge"] else None))
    return layout


# ------------------------------------------------------------------ the service

class Optimizer:
    def __init__(self, s, st, tariff, fallback, conf, regmap, map_path, apply,
                 assume_storm=None, assume_outage=False):
        self.s, self.st, self.tariff = s, st, tariff
        self.conf, self.regmap, self.map_path, self.apply = conf, regmap, map_path, apply
        self.assume_storm, self.assume_outage = assume_storm, assume_outage
        self.prices = PriceCache(s["tz"], tariff, fallback, s["price_warning_hour"])
        self.state = load_state()
        self.last_slots = None
        self.inverter_layout: list[dict] | None = None  # slots actually in the inverter
        self.plan_hours: list[dict] = []          # hours of the last plan (corrected solar, load)
        self.solar_raw: dict[str, float] = {}     # last solar forecast, uncorrected (kWh per hour)
        self.pv_actual: dict[str, float] = {}     # measured solar kWh per hour (today)
        self.last_sample: tuple[datetime, float] | None = None
        self.pv_ratio = 1.0
        self.capacity_kwh = 0.0
        self.last_current_write = 0.0

    # -- helpers
    def persist(self) -> None:
        if self.apply:
            save_state(self.state)

    def read(self, full: bool) -> dict[int, int]:
        inv = connect(self.conf, quiet=True)
        try:
            r = read_block(inv, 150, 50)
            if full:
                r.update(read_block(inv, 200, 81))
                r.update(read_block(inv, 280, 20))
            return r
        finally:
            inv.disconnect()

    def grid_ok(self, r) -> bool:
        return r[REG_GRID_STATUS] == 1 and not self.assume_outage

    # -- write limits
    def test_mode_until(self) -> datetime | None:
        try:
            until = datetime.fromisoformat(json.loads(TEST_MODE_FILE.read_text())["until"])
        except (FileNotFoundError, KeyError, ValueError):
            return None
        return until if until > datetime.now(self.s["tz"]) else None

    def check_control_files(self, now: datetime) -> None:
        if RESET_WRITES_FILE.exists():
            RESET_WRITES_FILE.unlink(missing_ok=True)
            self.state["write_log"] = {}
            self.state.pop("last_write", None)
            self.persist()
            log(f"[{now:%H:%M}] Write counters reset (--reset-write-count).")
        until = self.test_mode_until()
        active = until is not None
        if active != self.state.get("test_mode", False):
            self.state["test_mode"] = active
            self.persist()
            log(f"[{now:%H:%M}] Test mode " + (f"ON until {until:%d/%m %H:%M}: no hourly limit, "
                f"{self.s['test_writes_per_day']} plan writes per day." if active else "OFF: normal write limits."))

    def write_allowed(self, kind: str, now: datetime) -> bool:
        """kind: 'plan' or 'charge'. Counts the writes of the last hour and of today."""
        wl = self.state.setdefault("write_log", {})
        today = now.date().isoformat()
        stamps = [t for t in wl.get(kind, []) if t.startswith(today)]
        wl[kind] = stamps
        last_hour = sum(1 for t in stamps if datetime.fromisoformat(t) > now - timedelta(hours=1))
        test = self.test_mode_until() is not None
        per_hour = None if test and kind == "plan" else self.s[f"{kind}_writes_per_hour"]
        per_day = self.s["test_writes_per_day"] if test and kind == "plan" else self.s[f"{kind}_writes_per_day"]
        if per_hour is not None and last_hour >= per_hour:
            log(f"  WARNING: {kind} write limit reached ({per_hour} per hour), not writing.")
            return False
        if len(stamps) >= per_day:
            log(f"  WARNING: {kind} write limit reached ({per_day} per day), not writing.")
            return False
        return True

    def is_loop(self, changes_key: str, now: datetime) -> bool:
        """True if this same change has been written loop_max_repeats times in a row within
        loop_window_hours: the inverter keeps reverting it, so insisting would only wear its
        memory. Only consecutive repeats count, so alternating writes (e.g. several outages
        with the normal plan in between) are never blocked."""
        last = self.state.get("last_write", {})
        if last.get("key") != changes_key:
            return False
        window_start = now - timedelta(hours=self.s["loop_window_hours"])
        return datetime.fromisoformat(last["first"]) > window_start and last["count"] >= self.s["loop_max_repeats"]

    def record_write(self, changes_key: str, stamp: str, now: datetime) -> None:
        last = self.state.get("last_write", {})
        window_start = now - timedelta(hours=self.s["loop_window_hours"])
        if last.get("key") == changes_key and datetime.fromisoformat(last["first"]) > window_start:
            last["count"] += 1
        else:
            last = {"key": changes_key, "count": 1, "first": stamp}
        self.state["last_write"] = last

    def write_profile(self, profile: dict, label: str, r: dict, kind: str = "plan", priority: bool = False) -> None:
        """Write a profile if it differs from the inverter. kind: 'plan' or 'charge' (separate
        limits); priority writes (outage, storm reserve) skip the limits but not loop detection."""
        proposed, changes, errors, _ = compute_changes(profile, self.regmap, r)
        if errors:
            log(f"ERRORS in {label} (nothing written): " + "; ".join(errors))
            return
        if not changes:
            return
        log(f"  {len(changes)} change(s) for {label}: " + "; ".join(f"{n} {a}->{b}" for n, a, b in changes[:10])
            + (" ..." if len(changes) > 10 else ""))
        if not self.apply:
            log("  Dry run: nothing written.")
            return
        now = datetime.now(self.s["tz"])
        key = ",".join(f"{reg}={val}" for reg, val in sorted(proposed.items()) if r.get(reg) != val)
        if self.is_loop(key, now):
            log(f"  WARNING: the same change has been written {self.s['loop_max_repeats']} times in "
                f"{self.s['loop_window_hours']} h and the inverter does not keep it; not insisting ({key}).")
            return
        if not priority and not self.write_allowed(kind, now):
            return
        save_backup("auto", self.map_path, r, reason=label)
        failures = write(self.conf, r, proposed)
        stamp = now.isoformat(timespec="seconds")
        self.state.setdefault("write_log", {}).setdefault(kind if not priority else "priority", []).append(stamp)
        self.record_write(key, stamp, now)
        self.persist()
        log("  Written and verified." if not failures else "  WARNING: " + "; ".join(failures))

    # -- measured solar production
    @staticmethod
    def pv_kw(r) -> float:
        return (r[REG_PV1_POWER] + r[REG_PV2_POWER]) / 1000

    def sample_pv(self, r, now: datetime) -> None:
        """Integrate the measured solar power into kWh per hour."""
        kw = self.pv_kw(r)
        if self.last_sample:
            t0, kw0 = self.last_sample
            dt = (now - t0).total_seconds() / 3600
            if 0 < dt < 0.25:  # ignore long gaps (restarts, read failures)
                key = now.replace(minute=0, second=0, microsecond=0).isoformat()
                self.pv_actual[key] = self.pv_actual.get(key, 0.0) + (kw0 + kw) / 2 * dt
        self.last_sample = (now, kw)
        today = now.date().isoformat()
        self.pv_actual = {k: v for k, v in self.pv_actual.items() if k.startswith(today)}

    def update_pv_ratio(self, now: datetime) -> None:
        """Ratio measured / forecast over today's completed daylight hours."""
        current = now.replace(minute=0, second=0, microsecond=0).isoformat()
        pairs = [(a, self.solar_raw.get(k, 0.0)) for k, a in self.pv_actual.items()
                 if k < current and self.solar_raw.get(k, 0.0) >= 0.15]
        if len(pairs) >= self.s["pv_correction_min_hours"]:
            actual, forecast = sum(a for a, _ in pairs), sum(f for _, f in pairs)
            self.pv_ratio = min(2.0, max(0.3, actual / forecast))
        else:
            self.pv_ratio = 1.0

    # -- real-time grid charge control
    def active_charge_slot(self, now: datetime):
        """(target SOC %, slot end) if a charge slot of the last plan is active now."""
        layout = self.inverter_layout or self.last_slots
        if not layout:
            return None
        starts = [int(x["time"][:2]) for x in layout]
        for k, x in enumerate(layout):
            start, end = starts[k], starts[k + 1] if k + 1 < len(starts) else 24
            if start <= now.hour < end:
                if not x["grid_charge"]:
                    return None
                slot_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end)
                return x["soc"], slot_end
        return None

    def charge_control(self, r, now: datetime, force: bool = False) -> None:
        """In a charge slot, ask the grid only for what the sun will not provide, so the
        target SOC is reached at the end of the slot instead of filling the battery early."""
        slot = self.active_charge_slot(now)
        if not slot or not self.capacity_kwh:
            return
        target, slot_end = slot
        remaining_h = max(0.25, (slot_end - now).total_seconds() / 3600)
        needed = max(0.0, (target - r[REG_SOC]) / 100 * self.capacity_kwh) / self.s["charge_efficiency"]
        # Solar surplus expected until the end of the slot: measured now for the current hour,
        # corrected forecast for the following hours
        surplus_now = max(0.0, self.pv_kw(r) - abs(s16(r[REG_LOAD_POWER])) / 1000)
        expected = surplus_now * (1 - now.minute / 60)
        for h in self.plan_hours:
            if now < h["time"] and h["time"] < slot_end:
                expected += max(0.0, h["pv"] - h["load"])
        grid_kw = max(0.0, needed - expected) / remaining_h
        voltage = r[REG_BATTERY_VOLTAGE] / 100 or self.s["battery_nominal_voltage"]
        amps = min(self.s["max_grid_charge_current"], math.ceil(grid_kw * 1000 / voltage / 5) * 5)
        current = r.get(REG_GRID_CHARGE_CURRENT)
        if current is None or abs(amps - current) < 5:
            return
        if not force and time.monotonic() - self.last_current_write < self.s["charge_control_minutes"] * 60:
            return
        log(f"[{now:%H:%M}] Charge control: {needed:.1f} kWh to {target} % in {remaining_h * 60:.0f} min, "
            f"solar surplus {surplus_now:.1f} kW now, {expected:.1f} kWh expected -> grid {grid_kw:.1f} kW "
            f"({current} A -> {amps} A)")
        self.write_profile({"parameters": {"grid_charge_current": amps}}, "charge control", r, kind="charge")
        self.last_current_write = time.monotonic()

    # -- outage protection
    def outage_profile(self) -> dict:
        n = self.regmap["tou"]["slots"]
        return {"tou": [{"slot": i + 1, "soc": self.st["outage_soc"]} for i in range(n)]}

    def grid_check(self) -> None:
        now = datetime.now(self.s["tz"])
        self.check_control_files(now)
        r = self.read(full=self.active_charge_slot(now) is not None)
        self.sample_pv(r, now)
        ok = self.grid_ok(r)
        if not ok and not self.state.get("outage"):
            log(f"[{datetime.now(self.s['tz']):%H:%M}] Grid DOWN: battery available down to "
                f"{self.st['outage_soc']} %.")
            self.state["outage"] = True
            self.persist()
            self.write_profile(self.outage_profile(), "outage", self.read(full=True), priority=True)
        elif ok and self.state.get("outage"):
            log(f"[{datetime.now(self.s['tz']):%H:%M}] Grid back: recalculating the plan.")
            self.state["outage"] = False
            self.persist()
            self.plan()
        elif ok:
            self.charge_control(r, now)

    # -- the plan
    def plan(self) -> None:
        s, st = self.s, self.st
        now = datetime.now(s["tz"])
        self.check_control_files(now)
        r = self.read(full=True)
        if not self.grid_ok(r):
            if not self.state.get("outage"):
                self.state["outage"] = True
                self.persist()
            log(f"[{now:%Y-%m-%d %H:%M}] Grid down: keeping the battery available, no plan.")
            self.write_profile(self.outage_profile(), "outage", r, priority=True)
            return

        self.prices.update(now)
        self.sample_pv(r, now)
        solar = fetch_solar(s)
        self.solar_raw.update({k: v for k, v in solar.items() if k.startswith(now.date().isoformat())})
        self.update_pv_ratio(now)
        voltage = r[REG_BATTERY_VOLTAGE] / 100 or s["battery_nominal_voltage"]
        model = Model(s, st, r[REG_BATTERY_CAPACITY] * s["battery_nominal_voltage"] / 1000, voltage)
        self.capacity_kwh = model.cap
        soc0 = model.cap * r[REG_SOC] / 100
        start = now.replace(minute=0, second=0, microsecond=0)

        # Storm hours with their confidence level and reserve
        storms = []  # (hour, level, explanation)
        if st["enabled"]:
            try:
                models = fetch_storm_models(s, st)
            except Exception as e:
                models = {}
                log(f"Storm forecast not available ({e.__class__.__name__}); no storm reserve this cycle.")
            for k in range(s["horizon_hours"]):
                t = start + timedelta(hours=k)
                level, info = storm_confidence(models, t, st)
                if level:
                    storms.append((t, level, info))
        if self.assume_storm is not None:
            t = start + timedelta(hours=math.ceil(self.assume_storm))
            storms += [(t, "high", "simulated storm"), (t + timedelta(hours=1), "high", "simulated storm")]
        levels = {"high": st["reserve_soc"], "medium": st["reserve_soc_medium"]}
        windows = [(t - timedelta(hours=st["reserve_lead_hours"]), t + timedelta(hours=1 + st["grace_hours"]),
                    levels[level]) for t, level, _ in storms]

        hours, sources = [], {}
        for k in range(s["horizon_hours"]):
            t = start + timedelta(hours=k)
            price, source = self.prices.get(t)
            sources[source] = sources.get(source, 0) + 1
            frac = 1 - now.minute / 60 if k == 0 else 1.0
            reserve_pct = max([pct for a, b, pct in windows if a < t + timedelta(hours=1) and t < b], default=0)
            ratio = self.pv_ratio if t.date() == now.date() else 1.0  # correct today's forecast with measurements
            hours.append({"time": t, "price": price, "frac": frac,
                          "export": self.prices.export_value(t, price, s["export_price"]),
                          "pv": solar.get(t.isoformat(), 0.0) * frac * ratio,
                          "load": s["load_profile"][t.hour] * frac, "reserve": model.cap * reserve_pct / 100})

        self.plan_hours = hours
        gc, dis, soc, deficit = model.optimize(hours, soc0)
        actions = hour_actions(gc, dis, deficit)
        # Free energy: in hours priced at or below free_charge_price, charge up to max_soc.
        # The sun keeps priority (the charge control only asks the grid for the rest); this
        # makes sure the battery fills up even if the sun turns out weaker than forecast.
        slot_soc = list(soc)
        for k, h in enumerate(hours):
            if h["price"] <= s["free_charge_price"]:
                actions[k] = "charge"
                slot_soc[k] = model.max
        hold = [a == "hold" for a in actions]
        cost, rows = model.simulate(hours, soc0, list(gc), hold, detail=True)
        exp_kwh = sum(rw["export"] for rw in rows[:24])
        exp_eur = sum(rw["export"] * h["export"] for rw, h in zip(rows[:24], hours[:24]))
        baseline = model.simulate(hours, soc0, [0.0] * len(hours), [False] * len(hours))
        slots = build_slots(hours, actions, slot_soc, soc0, model, s)
        need_kw = max([gc[k] / hours[k]["frac"] for k in range(min(24, len(gc)))] + [0])
        current = min(s["max_grid_charge_current"], max(10, math.ceil(need_kw * 1000 / voltage / 5) * 5))

        changed = slots != self.last_slots
        src = ", ".join(f"{v} h {k}" for k, v in sources.items())
        if storms:
            first = storms[0]
            n_high = sum(1 for x in storms if x[1] == "high")
            storm_txt = (f"STORM: {len(storms)} h ({n_high} high), first {first[0]:%d/%m %H:%M} {first[1]} "
                         f"[{first[2]}] | ")
        else:
            storm_txt = ""
        pv_txt = f" | solar {self.pv_kw(r):.1f} kW, forecast x{self.pv_ratio:.2f}" if self.pv_ratio != 1.0 or self.pv_kw(r) > 0 else ""
        log(f"[{now:%Y-%m-%d %H:%M}] SOC {r[REG_SOC]} %{pv_txt} | prices: {src} | {storm_txt}"
            f"cost {cost:.2f} EUR vs {baseline:.2f} without plan | export next 24 h {exp_kwh:.1f} kWh "
            f"({exp_eur:.2f} EUR)" + ("" if changed else " | plan unchanged"))
        if changed:
            log("  hour    EUR/kWh solar load  action   SOC  reserve")
            for k, h in enumerate(hours[:24]):
                extra = f" +{gc[k]:.1f} kWh" if actions[k] == "charge" else ""
                res_txt = f"{round(h['reserve'] / model.cap * 100)} %" if h["reserve"] else ""
                log(f"  {h['time']:%d %H}h  {h['price']:.3f}  {h['pv']:4.1f} {h['load']:4.1f}  {actions[k]:7} "
                    f"{round(soc[k] / model.cap * 100):3d} %  {res_txt:7}{extra}")
            log("  Time Of Use: " + " | ".join(f"{x['time']} {x['action']} {x['soc']}%" for x in slots)
                + f" | grid charge up to {current} A (adjusted in real time)")
        self.last_slots = slots

        # The grid charge current is set by the real-time charge control, not by the plan
        profile = {"parameters": {"grid_charge": True, "time_of_use": "MTWTFSS",
                                  "max_charge_current": s["max_charge_current"],
                                  "max_discharge_current": s["max_discharge_current"],
                                  "grid_peak_shaving": True, "grid_peak_shaving_power": s["grid_power_limit"]},
                   "tou": [{k: v for k, v in x.items() if k != "action"} for x in slots]}
        storm_soon = any(h["reserve"] and h["time"] < now + timedelta(hours=s_storm_hours)
                         for h in hours) if (s_storm_hours := self.s["storm_priority_hours"]) else False

        # Only write if it is worth it: compare the cost of keeping the inverter's current slots
        # with the cost of the new slots (both simulated with the same, updated data)
        tou = self.regmap["tou"]
        current_slots = inverter_slots(r, tou)
        cost_current = model.simulate_layout(hours, soc0, slots_to_layout(current_slots, hours, model.cap))
        cost_new = model.simulate_layout(hours, soc0, slots_to_layout(slots, hours, model.cap))
        saving = cost_current - cost_new
        _, param_changes, _, _ = compute_changes({"parameters": profile["parameters"]}, self.regmap, r)
        written_slots = current_slots
        if param_changes or saving >= s["min_saving"]:
            reason = "safety parameters" if param_changes and saving < s["min_saving"] else f"saves {saving:.3f} EUR"
            self.write_profile(profile, ("plan (storm reserve)" if storm_soon else "plan") + f", {reason}",
                               r, priority=storm_soon)
            written_slots = slots
        elif slots != current_slots and changed:
            log(f"  Plan not written: it would save only {saving:.3f} EUR (min_saving {s['min_saving']:.3f}).")
        self.inverter_layout = written_slots  # what the inverter has: used by the charge control
        self.charge_control(r, now, force=changed)

        STATE_DIR.mkdir(exist_ok=True)
        PLAN_FILE.write_text(json.dumps({
            "time": now.isoformat(), "soc": r[REG_SOC], "cost": cost, "baseline": baseline,
            "export_kwh_24h": round(exp_kwh, 2), "export_eur_24h": round(exp_eur, 3),
            "grid_charge_current": current,
            "storms": [(t.isoformat(), level, info) for t, level, info in storms], "slots": slots,
            "hours": [{"time": h["time"].isoformat(), "price": h["price"], "pv": round(h["pv"], 2),
                       "load": round(h["load"], 2), "reserve": round(h["reserve"] / model.cap * 100),
                       "grid_charge": round(float(gc[k]), 2), "action": actions[k],
                       "soc": round(soc[k] / model.cap * 100)} for k, h in enumerate(hours)]},
            indent=1))
        new_file = not PLAN_LOG.exists()
        with PLAN_LOG.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "soc", "cost", "baseline", "storm_hours", "grid_charge_current", "slots"])
            w.writerow([now.isoformat(timespec="minutes"), r[REG_SOC], round(cost, 3), round(baseline, 3),
                        len(storms), current, " ".join(f"{x['time']}/{x['action']}/{x['soc']}" for x in slots)])

    def run_forever(self) -> None:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        log(f"Automatic mode started: plan every {self.s['interval']} s, grid check every "
            f"{self.st['grid_check_interval']} s, storm protection {'on' if self.st['enabled'] else 'off'}, "
            f"{'APPLY' if self.apply else 'dry run'} mode.")
        next_plan = 0.0
        while not stop.is_set():
            try:
                if time.monotonic() >= next_plan:
                    self.plan()
                    next_plan = time.monotonic() + self.s["interval"]
                else:
                    self.grid_check()
            except (Exception, SystemExit) as e:  # never stop the service because of one failed check
                log(f"ERROR, will retry: {e}")
            stop.wait(self.st["grid_check_interval"])
        log("Automatic mode stopped.")


def main():
    parser = argparse.ArgumentParser(description="Automatic mode: price optimizer with storm protection")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show", action="store_true", help="Only show the plan (default)")
    mode.add_argument("--apply", action="store_true", help="Write the plan to the inverter")
    parser.add_argument("--daemon", action="store_true", help="Run continuously as a service")
    parser.add_argument("--assume-storm", type=float, metavar="HOURS",
                        help="Pretend a 2-hour storm starts in HOURS hours (testing, dry run only)")
    parser.add_argument("--assume-outage", action="store_true",
                        help="Pretend the grid is down (testing, dry run only)")
    parser.add_argument("--test-mode", metavar="DURATION",
                        help="Relax the plan write limits for a while, e.g. 4h or 30m ('off' to end it); "
                             "read by the running service")
    parser.add_argument("--reset-write-count", action="store_true",
                        help="Reset the write counters of the running service")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()
    if args.test_mode or args.reset_write_count:
        STATE_DIR.mkdir(exist_ok=True)
        if args.reset_write_count:
            RESET_WRITES_FILE.touch()
            print("Write counters will be reset by the service within a minute.")
        if args.test_mode:
            if args.test_mode.lower() == "off":
                TEST_MODE_FILE.unlink(missing_ok=True)
                print("Test mode off: normal write limits within a minute.")
            else:
                unit = args.test_mode[-1].lower()
                try:
                    amount = float(args.test_mode[:-1])
                    delta = {"h": timedelta(hours=amount), "m": timedelta(minutes=amount)}[unit]
                except (ValueError, KeyError):
                    sys.exit("ERROR: --test-mode must be like 4h, 30m or off")
                tz = ZoneInfo(load_settings(args.config)[0]["timezone"])
                until = datetime.now(tz) + delta
                TEST_MODE_FILE.write_text(json.dumps({"until": until.isoformat()}))
                print(f"Test mode until {until:%d/%m %H:%M}: no hourly limit for plan writes.")
        return
    if (args.assume_storm is not None or args.assume_outage) and (args.apply or args.daemon):
        sys.exit("ERROR: --assume-storm and --assume-outage are for testing (single dry run only)")

    s, st, tariff, fallback = load_settings(args.config)
    conf = load_config(args.config)
    map_path, regmap = load_map(args.config)
    if "tou" not in regmap:
        sys.exit("ERROR: this inverter's map does not define Time Of Use slots")
    opt = Optimizer(s, st, tariff, fallback, conf, regmap, map_path, args.apply,
                    args.assume_storm, args.assume_outage)
    if args.daemon:
        opt.run_forever()
        return
    try:
        opt.plan()
    except Exception as e:
        sys.exit(f"ERROR during planning: {e}")


if __name__ == "__main__":
    main()

