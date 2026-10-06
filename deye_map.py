"""
Register map handling (maps/*.toml): map loading, register -> human-readable
text, and human-readable value -> register (with validation).
"""

import sys
import tomllib
from pathlib import Path

BASE = Path(__file__).parent
DEFAULT_MAP = "maps/deye_sg0xlp1.toml"
DAYS = "MTWTFSS"


class InvalidValue(ValueError):
    """Invalid profile value for a parameter."""


def load_map(config_path: Path) -> tuple[Path, dict]:
    with config_path.open("rb") as f:
        cfg = tomllib.load(f)
    path = BASE / cfg.get("inverter", {}).get("map", DEFAULT_MAP)
    if not path.exists():
        sys.exit(f"ERROR: register map {path} not found")
    with path.open("rb") as f:
        regmap = tomllib.load(f)
    regmap["by_id"] = {p["id"]: p for p in regmap["param"]}
    return path, regmap


def hhmm(v: int) -> str:
    return f"{v // 100:02d}:{v % 100:02d}"


# ------------------------------------------------------------------ register -> text

def format_value(p: dict, r: dict[int, int]) -> str:
    v = r[p["reg"]]
    kind = p["type"]
    if kind == "num":
        scale = p.get("scale", 1)
        text = f"{v * scale:.2f}".rstrip("0").rstrip(".") if scale != 1 else str(v)
        return f"{text} {p.get('unit', '')}".strip()
    if kind == "enum":
        return p["options"].get(str(v), f"unknown ({v})")
    if kind == "switch":
        return "Yes" if v else "No"
    if kind == "bit":
        return "Yes" if v >> p["bit"] & 1 else "No"
    if kind == "days":
        if not v & 1:
            return "Disabled"
        return "Enabled (" + "".join(d if v >> (i + 1) & 1 else "-" for i, d in enumerate(DAYS)) + ")"
    if kind == "datetime":
        a, b, c = r[p["reg"]], r[p["reg"] + 1], r[p["reg"] + 2]
        return f"{2000 + (a >> 8)}-{a & 0xFF:02d}-{b >> 8:02d} {b & 0xFF:02d}:{c >> 8:02d}:{c & 0xFF:02d}"
    return f"unknown type '{kind}' ({v})"


def format_tou_charge(v: int) -> str:
    """Bit 0 = grid, bit 1 = generator. Other bits are shown raw."""
    parts = [n for b, n in ((0, "Grid"), (1, "Gen")) if v >> b & 1]
    other = v & ~0b11
    text = " + ".join(parts) if parts else "No"
    return f"{text} (other bits: {other})" if other else text


# ------------------------------------------------------------------ human value -> register

def _bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("yes", "no"):
        return value.strip().lower() == "yes"
    raise InvalidValue(f"{name}: expected true/false, got {value!r}")


def _check_range(value: float, lo, hi, name: str, unit: str = "") -> None:
    if lo is not None and value < lo or hi is not None and value > hi:
        raise InvalidValue(f"{name}: {value} {unit} out of range ({lo}-{hi} {unit})".replace("  ", " "))


def to_register(p: dict, value, current: int) -> int:
    """Convert a profile value to the register value. 'current' is used to preserve other bits."""
    name, kind = p["id"], p["type"]
    if kind == "num":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidValue(f"{name}: expected a number, got {value!r}")
        _check_range(value, p.get("min"), p.get("max"), name, p.get("unit", ""))
        return round(value / p.get("scale", 1))
    if kind == "enum":
        for key, text in p["options"].items():
            if str(value).strip().lower() in (text.lower(), key):
                return int(key)
        raise InvalidValue(f"{name}: {value!r} is not valid. Options: {', '.join(p['options'].values())}")
    if kind == "switch":
        return 1 if _bool(value, name) else 0
    if kind == "bit":
        mask = 1 << p["bit"]
        return current | mask if _bool(value, name) else current & ~mask
    if kind == "days":
        if isinstance(value, bool):
            return 0xFF if value else 0
        text = str(value).upper()
        if len(text) != 7 or any(c not in (d, "-") for c, d in zip(text, DAYS)):
            raise InvalidValue(f"{name}: use true/false or 7 characters like 'MTWTF--'")
        return 1 | sum(1 << (i + 1) for i, c in enumerate(text) if c != "-")
    raise InvalidValue(f"{name}: type '{kind}' cannot be written")


def time_to_register(value, name: str) -> int:
    try:
        h, m = (int(x) for x in str(value).split(":"))
    except ValueError:
        raise InvalidValue(f"{name}: invalid time {value!r}, use 'HH:MM'") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise InvalidValue(f"{name}: time {value!r} out of range")
    return h * 100 + m



# ------------------------------------------------------------------ register -> profile value

def from_register(p: dict, r: dict[int, int]):
    """Convert a register value to the value used in profiles (inverse of to_register)."""
    v = r[p["reg"]]
    kind = p["type"]
    if kind == "num":
        value = v * p.get("scale", 1)
        return int(value) if float(value).is_integer() else round(value, 4)
    if kind == "enum":
        return p["options"].get(str(v), v)
    if kind == "switch":
        return bool(v)
    if kind == "bit":
        return bool(v >> p["bit"] & 1)
    if kind == "days":
        if not v & 1:
            return False
        return "".join(d if v >> (i + 1) & 1 else "-" for i, d in enumerate(DAYS))
    return None
