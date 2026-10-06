#!/usr/bin/env python3
"""
Lee la configuración de batería, carga desde red y Time Of Use (TOU) de un
inversor Deye híbrido monofásico (SUN-xK-SG03LP1 y similares).
Solo LEE registros. Guarda además una copia de seguridad en JSON.

Uso:
    python deye_leer_config.py                  # muestra y guarda backup
    python deye_leer_config.py --no-backup
    python deye_leer_config.py --config otro.toml

AVISO: el mapa de registros procede de la documentación comunitaria para los
híbridos monofásicos de baja tensión. Verifica los valores con la pantalla o la
app antes de usar estos registros para escribir.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from deye_comun import DEFAULT_CONFIG, cargar_config, conectar, leer_bloque, leer_serie_inversor

INICIO, FIN = 200, 280         # rango de registros de configuración que se lee
BACKUP_DIR = Path(__file__).with_name("backups")

MODOS_TRABAJO = {0: "Selling first", 1: "Zero export to load", 2: "Zero export to CT"}


def hhmm(v: int) -> str:
    return f"{v // 100:02d}:{v % 100:02d}"


def mostrar(r: dict[int, int]) -> None:
    # Registros marcados con (?) están pendientes de verificar contra la app/pantalla.
    print("\n=== Batería ===")
    print(f"  Corriente máx. de carga      : {r[210]} A")
    print(f"  Corriente máx. de descarga   : {r[211]} A")
    print(f"  SOC apagado (shutdown)       : {r[217]} %")
    print(f"  SOC rearranque (restart)     : {r[218]} %")
    print(f"  SOC bajo (low batt)          : {r[219]} %")

    print("\n=== Carga desde red (?) ===")
    print(f"  Corriente de carga desde red : {r[230]} A (?)")

    print("\n=== Modo de trabajo ===")
    print(f"  Modo                         : {MODOS_TRABAJO.get(r[244], 'desconocido')} (raw {r[244]}) (?)")
    print(f"  Time Of Use                  : {'Activo' if r[248] & 1 else 'Inactivo'} (raw {r[248]})")

    print("\n=== Franjas Time Of Use ===")
    print("  Franja  Inicio  Potencia  Tensión  SOC   Carga (raw)")
    for i in range(6):
        print(f"   {i + 1}      {hhmm(r[250 + i])}   {r[256 + i]:>5} W  {r[262 + i] / 100:>6.2f} V  "
              f"{r[268 + i]:>3} %   {r[274 + i]}")


def guardar_backup(serie: str, r: dict[int, int]) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    ruta = BACKUP_DIR / f"config_{serie}_{datetime.now():%Y%m%d_%H%M%S}.json"
    datos = {
        "inversor": serie,
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "registros": {str(k): v for k, v in sorted(r.items())},
    }
    ruta.write_text(json.dumps(datos, indent=2))
    return ruta


def main():
    parser = argparse.ArgumentParser(description="Lee la configuración del inversor Deye")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Ruta al fichero de configuración (por defecto: config.toml)")
    parser.add_argument("--no-backup", action="store_true", help="No guardar copia de seguridad JSON")
    args = parser.parse_args()

    inv = conectar(cargar_config(args.config))
    try:
        serie = leer_serie_inversor(inv)
        registros = leer_bloque(inv, INICIO, FIN - INICIO + 1)
    except Exception as e:
        sys.exit(f"ERROR al leer registros: {e}")
    finally:
        inv.disconnect()

    print(f"Inversor {serie}")
    mostrar(registros)

    if not args.no_backup:
        print(f"\nCopia de seguridad guardada en: {guardar_backup(serie, registros)}")


if __name__ == "__main__":
    main()

