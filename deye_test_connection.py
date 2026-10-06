#!/usr/bin/env python3
"""
Connection test with a Deye inverter through the Solarman data logger
(Modbus TCP). Only READS registers, never writes to the inverter.

Usage:
    python deye_test_connection.py                  # uses ./config.toml
    python deye_test_connection.py --config other.toml
"""

import argparse
import sys
from pathlib import Path

from deye_common import DEFAULT_CONFIG, connect, load_config, read_inverter_serial


def main():
    parser = argparse.ArgumentParser(description="Connection test with a Deye inverter")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Path to the configuration file (default: config.toml)")
    args = parser.parse_args()

    inv = connect(load_config(args.config))
    try:
        serial = read_inverter_serial(inv)
        soc = inv.read_holding_registers(register_addr=184, quantity=1)[0]
        voltage = inv.read_holding_registers(register_addr=183, quantity=1)[0] / 100

        print("Connection OK.")
        print(f"  Inverter serial : {serial}")
        print(f"  Battery SOC     : {soc} %")
        print(f"  Battery voltage : {voltage:.2f} V")
    except Exception as e:
        sys.exit(f"ERROR reading registers: {e}")
    finally:
        inv.disconnect()


if __name__ == "__main__":
    main()

