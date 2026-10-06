#!/usr/bin/env python3
"""
Prueba de conexión con un inversor Deye a través del datalogger Solarman
(Modbus TCP). Solo LEE registros, no escribe nada en el inversor.

Uso:
    python deye_test_connection.py                 # usa ./config.toml
    python deye_test_connection.py --config otro.toml
"""

import argparse
import sys
from pathlib import Path

from deye_comun import DEFAULT_CONFIG, cargar_config, conectar, leer_serie_inversor


def main():
    parser = argparse.ArgumentParser(description="Prueba de conexión con inversor Deye")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Ruta al fichero de configuración (por defecto: config.toml)")
    args = parser.parse_args()

    inv = conectar(cargar_config(args.config))
    try:
        serie = leer_serie_inversor(inv)
        soc = inv.read_holding_registers(register_addr=184, quantity=1)[0]
        voltaje = inv.read_holding_registers(register_addr=183, quantity=1)[0] / 100

        print("Conexión correcta.")
        print(f"  Nº serie inversor : {serie}")
        print(f"  SOC batería       : {soc} %")
        print(f"  Tensión batería   : {voltaje:.2f} V")
    except Exception as e:
        sys.exit(f"ERROR al leer registros: {e}")
    finally:
        inv.disconnect()


if __name__ == "__main__":
    main()

