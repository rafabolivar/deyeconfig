#!/usr/bin/env python3
"""
Lee toda la configuración de un inversor Deye a través del datalogger Solarman
(Modbus TCP), usando un fichero de mapa de registros (carpeta mapas/).
Solo LEE registros. Guarda además una copia de seguridad en JSON con todos
los registros en bruto del rango de configuración.

Uso:
    python deye_leer_config.py                  # muestra y guarda backup
    python deye_leer_config.py --no-backup
    python deye_leer_config.py --raw            # añade id, registro y valor en bruto
    python deye_leer_config.py --config otro.toml
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from deye_comun import DEFAULT_CONFIG, cargar_config, conectar, leer_bloque, leer_serie_inversor
from deye_mapa import cargar_mapa, formatear, formatear_carga_tou, hhmm

BACKUP_DIR = Path(__file__).with_name("backups")


def leer_registros(conf: dict, mapa: dict) -> tuple[str, dict[int, int]]:
    desde, hasta = mapa["info"]["rango_backup"]
    inv = conectar(conf)
    try:
        return leer_serie_inversor(inv), leer_bloque(inv, desde, hasta - desde + 1)
    except Exception as e:
        sys.exit(f"ERROR al leer registros: {e}")
    finally:
        inv.disconnect()


def mostrar(mapa: dict, r: dict[int, int], raw: bool) -> None:
    grupo_actual = None
    ancho = max(len(p["nombre"]) for p in mapa["param"])
    for p in mapa["param"]:
        if p["grupo"] != grupo_actual:
            grupo_actual = p["grupo"]
            print(f"\n=== {grupo_actual} ===")
        extra = ""
        if raw:
            marca = "" if p.get("escribible") else " (solo lectura)"
            extra = f"   [{p['id']}{marca} | reg {p['reg']} = {r[p['reg']]}]"
        print(f"  {p['nombre']:<{ancho}} : {formatear(p, r)}{extra}")

    t = mapa.get("tou")
    if t:
        print("\n=== Franjas Time Of Use ===")
        print("  Franja  Inicio  Potencia   Tensión  SOC   Carga")
        for i in range(t["franjas"]):
            print(f"   {i + 1}      {hhmm(r[t['hora'] + i])}   {r[t['potencia'] + i]:>5} W  "
                  f"{r[t['tension'] + i] / 100:>6.2f} V  {r[t['soc'] + i]:>3} %   "
                  f"{formatear_carga_tou(r[t['carga'] + i])}")


def guardar_backup(serie: str, mapa_ruta: Path, r: dict[int, int], motivo: str = "lectura") -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    ruta = BACKUP_DIR / f"config_{serie}_{datetime.now():%Y%m%d_%H%M%S}.json"
    datos = {
        "inversor": serie,
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "motivo": motivo,
        "mapa": mapa_ruta.name,
        "registros": {str(k): v for k, v in sorted(r.items())},
    }
    ruta.write_text(json.dumps(datos, indent=2))
    return ruta


def main():
    parser = argparse.ArgumentParser(description="Lee la configuración completa del inversor Deye")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Ruta al fichero de configuración (por defecto: config.toml)")
    parser.add_argument("--no-backup", action="store_true", help="No guardar copia de seguridad JSON")
    parser.add_argument("--raw", action="store_true",
                        help="Mostrar id del parámetro, registro y valor en bruto")
    args = parser.parse_args()

    conf = cargar_config(args.config)
    mapa_ruta, mapa = cargar_mapa(args.config)
    serie, registros = leer_registros(conf, mapa)

    print(f"Inversor {serie}  |  Mapa: {mapa['info']['modelo']}")
    mostrar(mapa, registros, args.raw)

    if not args.no_backup:
        print(f"\nCopia de seguridad guardada en: {guardar_backup(serie, mapa_ruta, registros)}")


if __name__ == "__main__":
    main()

