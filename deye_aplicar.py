#!/usr/bin/env python3
"""
Aplica un perfil de configuración (perfiles/*.toml) a un inversor Deye.

Uso:
    python deye_aplicar.py perfiles/invierno.toml             # muestra qué cambiaría
    python deye_aplicar.py perfiles/invierno.toml --show   # ídem, de forma explícita
    python deye_aplicar.py perfiles/invierno.toml --apply   # escribe (pide confirmación)
    python deye_aplicar.py perfiles/invierno.toml --apply --yes   # escribe sin preguntar

Proceso:
  1. Valida el perfil completo (parámetros existentes, escribibles y en rango).
     Si hay cualquier error, no se escribe nada.
  2. Lee la configuración actual y muestra solo lo que cambia (actual -> nuevo).
  3. Con --apply: guarda una copia de seguridad, escribe los registros
     modificados y los vuelve a leer para verificar.
"""

import argparse
import sys
import tomllib
from pathlib import Path

from deye_comun import DEFAULT_CONFIG, cargar_config, conectar
from deye_leer_config import guardar_backup, leer_registros
from deye_mapa import (ErrorValor, a_registro, cargar_mapa, formatear, formatear_carga_tou,
                       hhmm, hora_a_registro)

CLAVES_TOU = {"franja", "hora", "potencia", "tension", "soc", "carga_red", "carga_gen"}


def calcular_cambios(perfil: dict, mapa: dict, r: dict[int, int]):
    """Devuelve (propuesto, cambios, errores, avisos). No toca el inversor."""
    propuesto = dict(r)
    cambios, errores, avisos = [], [], []

    # --- parámetros generales
    for ident, valor in perfil.get("parametros", {}).items():
        p = mapa["por_id"].get(ident)
        if p is None:
            errores.append(f"{ident}: parámetro desconocido (consulta los ids con deye_leer_config.py --raw)")
            continue
        if not p.get("escribible"):
            errores.append(f"{ident}: parámetro de solo lectura, no se puede modificar desde un perfil")
            continue
        try:
            nuevo = a_registro(p, valor, propuesto[p["reg"]])
        except ErrorValor as e:
            errores.append(str(e))
            continue
        antes = formatear(p, propuesto)
        propuesto[p["reg"]] = nuevo
        despues = formatear(p, propuesto)
        if antes != despues:
            cambios.append((p["nombre"], antes, despues))

    # --- franjas Time Of Use
    t = mapa.get("tou")
    for entrada in perfil.get("tou", []):
        n = entrada.get("franja")
        if not t:
            errores.append("el mapa de este inversor no define franjas Time Of Use")
            break
        if not isinstance(n, int) or not 1 <= n <= t["franjas"]:
            errores.append(f"tou: 'franja' debe ser un número de 1 a {t['franjas']} (recibido {n!r})")
            continue
        desconocidas = set(entrada) - CLAVES_TOU
        if desconocidas:
            errores.append(f"franja {n}: claves desconocidas {sorted(desconocidas)}. Válidas: {sorted(CLAVES_TOU - {'franja'})}")
            continue
        i, etiqueta = n - 1, f"Franja {n}"
        try:
            if "hora" in entrada:
                reg = t["hora"] + i
                antes, propuesto[reg] = hhmm(propuesto[reg]), hora_a_registro(entrada["hora"], f"franja {n} hora")
                cambios.append((f"{etiqueta}: inicio", antes, hhmm(propuesto[reg])))
            if "potencia" in entrada:
                v = entrada["potencia"]
                if not isinstance(v, int) or not 0 <= v <= t["potencia_max"]:
                    raise ErrorValor(f"franja {n} potencia: {v!r} fuera de rango (0-{t['potencia_max']} W)")
                reg = t["potencia"] + i
                cambios.append((f"{etiqueta}: potencia", f"{propuesto[reg]} W", f"{v} W"))
                propuesto[reg] = v
            if "tension" in entrada:
                v = entrada["tension"]
                if not isinstance(v, (int, float)) or not t["tension_min"] <= v <= t["tension_max"]:
                    raise ErrorValor(f"franja {n} tension: {v!r} fuera de rango ({t['tension_min']}-{t['tension_max']} V)")
                reg = t["tension"] + i
                cambios.append((f"{etiqueta}: tensión", f"{propuesto[reg] / 100:.2f} V", f"{v:.2f} V"))
                propuesto[reg] = round(v * 100)
            if "soc" in entrada:
                v = entrada["soc"]
                if not isinstance(v, int) or not 0 <= v <= 100:
                    raise ErrorValor(f"franja {n} soc: {v!r} fuera de rango (0-100 %)")
                reg = t["soc"] + i
                cambios.append((f"{etiqueta}: SOC", f"{propuesto[reg]} %", f"{v} %"))
                propuesto[reg] = v
            for clave, bit in (("carga_red", 0), ("carga_gen", 1)):
                if clave in entrada:
                    v = entrada[clave]
                    if not isinstance(v, bool):
                        raise ErrorValor(f"franja {n} {clave}: se esperaba true/false, no {v!r}")
                    reg = t["carga"] + i
                    antes = formatear_carga_tou(propuesto[reg])
                    propuesto[reg] = propuesto[reg] | (1 << bit) if v else propuesto[reg] & ~(1 << bit)
                    cambios.append((f"{etiqueta}: carga", antes, formatear_carga_tou(propuesto[reg])))
        except ErrorValor as e:
            errores.append(str(e))

    if t:
        # Las horas de inicio deben ir en orden creciente
        horas = [propuesto[t["hora"] + i] for i in range(t["franjas"])]
        if any(a >= b for a, b in zip(horas, horas[1:])):
            errores.append("franjas TOU: las horas de inicio deben ser crecientes "
                           f"({', '.join(hhmm(h) for h in horas)})")
        # Avisos de coherencia
        carga_tou_red = any(propuesto[t["carga"] + i] & 1 for i in range(t["franjas"]))
        p_red, p_tou = mapa["por_id"].get("carga_red"), mapa["por_id"].get("time_of_use")
        if carga_tou_red and p_red and not propuesto[p_red["reg"]]:
            avisos.append("hay franjas con carga desde red, pero 'carga_red' está desactivada: no cargará")
        if carga_tou_red and p_tou and not propuesto[p_tou["reg"]] & 1:
            avisos.append("hay franjas con carga desde red, pero 'time_of_use' está inactivo: no se aplicarán")

    # Quitar entradas sin cambio real (p. ej. el mismo valor que ya tenía)
    cambios = [c for c in cambios if c[1] != c[2]]
    return propuesto, cambios, errores, avisos


def escribir(conf: dict, r: dict[int, int], propuesto: dict[int, int]) -> list[str]:
    """Escribe los registros modificados y verifica leyéndolos de nuevo. Devuelve fallos."""
    modificados = sorted(reg for reg in propuesto if propuesto[reg] != r[reg])
    fallos = []
    inv = conectar(conf)
    try:
        for reg in modificados:
            try:
                inv.write_multiple_holding_registers(register_addr=reg, values=[propuesto[reg]])
            except Exception as e:
                fallos.append(f"registro {reg}: error al escribir ({e})")
        for reg in modificados:
            leido = inv.read_holding_registers(register_addr=reg, quantity=1)[0]
            if leido != propuesto[reg]:
                fallos.append(f"registro {reg}: se escribió {propuesto[reg]} pero el inversor tiene {leido}")
    finally:
        inv.disconnect()
    print(f"Registros escritos: {len(modificados)}")
    return fallos


def main():
    parser = argparse.ArgumentParser(description="Aplica un perfil de configuración al inversor Deye")
    parser.add_argument("perfil", type=Path, help="Fichero de perfil (p. ej. perfiles/invierno.toml)")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--show", action="store_true", help="Solo mostrar qué cambiaría (por defecto)")
    modo.add_argument("--apply", action="store_true", help="Escribir los cambios en el inversor")
    parser.add_argument("--yes", action="store_true", help="Con --apply, no pedir confirmación")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="Ruta al fichero de configuración (por defecto: config.toml)")
    args = parser.parse_args()

    if not args.perfil.exists():
        sys.exit(f"ERROR: no existe el perfil {args.perfil}")
    try:
        with args.perfil.open("rb") as f:
            perfil = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"ERROR: el perfil {args.perfil} no es un TOML válido: {e}")

    conf = cargar_config(args.config)
    mapa_ruta, mapa = cargar_mapa(args.config)

    print(f"Perfil: {args.perfil.name}" + (f" — {perfil['descripcion']}" if "descripcion" in perfil else ""))
    serie, r = leer_registros(conf, mapa)
    propuesto, cambios, errores, avisos = calcular_cambios(perfil, mapa, r)

    if errores:
        print("\nERRORES en el perfil (no se ha escrito nada):")
        for e in errores:
            print(f"  - {e}")
        sys.exit(1)

    if not cambios:
        print("\nLa configuración del inversor ya coincide con el perfil. Nada que cambiar.")
        return

    ancho = max(len(c[0]) for c in cambios)
    print(f"\nCambios ({len(cambios)}):")
    for nombre, antes, despues in cambios:
        print(f"  {nombre:<{ancho}} : {antes}  ->  {despues}")
    for a in avisos:
        print(f"\nAVISO: {a}")

    if not args.apply:
        print("\nModo simulación: no se ha modificado nada. Usa --apply para escribir los cambios.")
        return

    if not args.yes:
        if input("\n¿Aplicar estos cambios al inversor? [s/y/N] ").strip().lower() not in ("s", "si", "sí", "y", "yes"):
            print("Cancelado. No se ha modificado nada.")
            return

    backup = guardar_backup(serie, mapa_ruta, r, motivo=f"antes de aplicar {args.perfil.name}")
    print(f"\nCopia de seguridad previa: {backup}")
    fallos = escribir(conf, r, propuesto)
    if fallos:
        print("\nATENCIÓN, la verificación ha detectado problemas:")
        for f in fallos:
            print(f"  - {f}")
        sys.exit(2)
    print("Cambios aplicados y verificados correctamente.")


if __name__ == "__main__":
    main()

