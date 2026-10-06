#!/usr/bin/env python3
"""
Lee toda la configuración de un inversor Deye a través del datalogger Solarman
(Modbus TCP), usando un fichero de mapa de registros (carpeta mapas/).
Solo LEE registros. Guarda además una copia de seguridad en JSON con todos
los registros en bruto del rango de configuración.

Uso:
    python deye_leer_config.py                  # muestra y guarda backup
    python deye_leer_config.py --no-backup
    python deye_leer_config.py --raw            # añade el valor en bruto de cada registro
    python deye_leer_config.py --config otro.toml
"""

import argparse
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from deye_comun import DEFAULT_CONFIG, cargar_config, conectar, leer_bloque, leer_serie_inversor

BASE = Path(__file__).parent
BACKUP_DIR = BASE / "backups"
MAPA_POR_DEFECTO = "mapas/deye_sg0xlp1.toml"
DIAS = ["L", "M", "X", "J", "V", "S", "D"]


def cargar_mapa(ruta_config: Path) -> tuple[Path, dict]:
    with ruta_config.open("rb") as f:
        cfg = tomllib.load(f)
    ruta = BASE / cfg.get("inversor", {}).get("mapa", MAPA_POR_DEFECTO)
    if not ruta.exists():
        sys.exit(f"ERROR: no existe el mapa de registros {ruta}")
    with ruta.open("rb") as f:
        return ruta, tomllib.load(f)


def hhmm(v: int) -> str:
    return f"{v // 100:02d}:{v % 100:02d}"


def formatear(p: dict, r: dict[int, int]) -> str:
    v = r[p["reg"]]
    tipo = p["tipo"]
    if tipo == "num":
        escala = p.get("escala", 1)
        valor = v * escala
        texto = f"{valor:.2f}".rstrip("0").rstrip(".") if escala != 1 else str(v)
        return f"{texto} {p.get('unidad', '')}".strip()
    if tipo == "enum":
        return p["opciones"].get(str(v), f"desconocido ({v})")
    if tipo == "switch":
        return "Sí" if v else "No"
    if tipo == "bit":
        return "Sí" if v >> p["bit"] & 1 else "No"
    if tipo == "dias":
        if not v & 1:
            return "Inactivo"
        dias = "".join(d if v >> (i + 1) & 1 else "-" for i, d in enumerate(DIAS))
        return f"Activo ({dias})"
    if tipo == "fecha":
        a, b, c = r[p["reg"]], r[p["reg"] + 1], r[p["reg"] + 2]
        return f"{2000 + (a >> 8)}-{a & 0xFF:02d}-{b >> 8:02d} {b & 0xFF:02d}:{c >> 8:02d}:{c & 0xFF:02d}"
    return f"tipo desconocido '{tipo}' ({v})"


def mostrar(mapa: dict, r: dict[int, int], raw: bool) -> None:
    grupo_actual = None
    ancho = max(len(p["nombre"]) for p in mapa["param"])
    for p in mapa["param"]:
        if p["grupo"] != grupo_actual:
            grupo_actual = p["grupo"]
            print(f"\n=== {grupo_actual} ===")
        extra = f"   [reg {p['reg']} = {r[p['reg']]}]" if raw else ""
        print(f"  {p['nombre']:<{ancho}} : {formatear(p, r)}{extra}")

    t = mapa.get("tou")
    if t:
        print("\n=== Franjas Time Of Use ===")
        print("  Franja  Inicio  Potencia   Tensión  SOC   Carga")
        for i in range(t["franjas"]):
            carga = r[t["carga"] + i]
            texto_carga = t["opciones_carga"].get(str(carga), f"desconocido ({carga})")
            print(f"   {i + 1}      {hhmm(r[t['hora'] + i])}   {r[t['potencia'] + i]:>5} W  "
                  f"{r[t['tension'] + i] / 100:>6.2f} V  {r[t['soc'] + i]:>3} %   {texto_carga}")


def guardar_backup(serie: str, mapa_ruta: Path, r: dict[int, int]) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    ruta = BACKUP_DIR / f"config_{serie}_{datetime.now():%Y%m%d_%H%M%S}.json"
    datos = {
        "inversor": serie,
        "fecha": datetime.now().isoformat(timespec="seconds"),
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
    parser.add_argument("--raw", action="store_true", help="Mostrar también el valor en bruto de cada registro")
    args = parser.parse_args()

    conf = cargar_config(args.config)
    mapa_ruta, mapa = cargar_mapa(args.config)
    desde, hasta = mapa["info"]["rango_backup"]

    inv = conectar(conf)
    try:
        serie = leer_serie_inversor(inv)
        registros = leer_bloque(inv, desde, hasta - desde + 1)
    except Exception as e:
        sys.exit(f"ERROR al leer registros: {e}")
    finally:
        inv.disconnect()

    print(f"Inversor {serie}  |  Mapa: {mapa['info']['modelo']}")
    mostrar(mapa, registros, args.raw)

    if not args.no_backup:
        print(f"\nCopia de seguridad guardada en: {guardar_backup(serie, mapa_ruta, registros)}")


if __name__ == "__main__":
    main()

