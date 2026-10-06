"""
Shared helpers: configuration loading and connection to a Deye inverter
through the Solarman data logger (Modbus TCP).
"""

import sys
import tomllib
from pathlib import Path

from pysolarmanv5 import PySolarmanV5

DEFAULT_CONFIG = Path(__file__).with_name("config.toml")


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    if not path.exists():
        sys.exit(f"ERROR: {path} not found. Copy config.example.toml to config.toml and fill it in.")
    with path.open("rb") as f:
        cfg = tomllib.load(f)

    logger = cfg.get("logger", {})
    modbus = cfg.get("modbus", {})
    conf = {
        "ip": logger.get("ip"),
        "serial": logger.get("serial"),
        "port": logger.get("port", 8899),
        "slave_id": modbus.get("slave_id", 1),
        "timeout": modbus.get("timeout", 10),
    }

    errors = []
    if not conf["ip"]:
        errors.append("missing [logger] ip")
    if not isinstance(conf["serial"], int) or conf["serial"] <= 0:
        errors.append("[logger] serial must be the logger serial number (a number, without quotes)")
    if errors:
        sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))
    return conf


def connect(conf: dict) -> PySolarmanV5:
    print(f"Connecting to {conf['ip']}:{conf['port']} (logger {conf['serial']})...", file=sys.stderr)
    try:
        return PySolarmanV5(conf["ip"], conf["serial"], port=conf["port"],
                            mb_slave_id=conf["slave_id"], socket_timeout=conf["timeout"],
                            verbose=False)
    except Exception as e:
        sys.exit(f"ERROR: could not open connection: {e}")


def read_block(inv: PySolarmanV5, start: int, count: int, chunk: int = 40) -> dict[int, int]:
    """Read 'count' holding registers from 'start' in small chunks. Returns {register: value}."""
    data = {}
    for first in range(start, start + count, chunk):
        n = min(chunk, start + count - first)
        values = inv.read_holding_registers(register_addr=first, quantity=n)
        data.update({first + i: v for i, v in enumerate(values)})
    return data


def read_inverter_serial(inv: PySolarmanV5) -> str:
    """Registers 3-7: inverter serial number in ASCII (2 characters per register)."""
    regs = inv.read_holding_registers(register_addr=3, quantity=5)
    return "".join(chr(r >> 8) + chr(r & 0xFF) for r in regs).strip("\x00 ")

