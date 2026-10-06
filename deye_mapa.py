"""
Interpretación del mapa de registros (mapas/*.toml): carga del mapa,
conversión registro -> texto legible y valor legible -> registro (con validación).
"""

import sys
import tomllib
from pathlib import Path

BASE = Path(__file__).parent
MAPA_POR_DEFECTO = "mapas/deye_sg0xlp1.toml"
DIAS = "LMXJVSD"


class ErrorValor(ValueError):
    """Valor de perfil no válido para un parámetro."""


def cargar_mapa(ruta_config: Path) -> tuple[Path, dict]:
    with ruta_config.open("rb") as f:
        cfg = tomllib.load(f)
    ruta = BASE / cfg.get("inversor", {}).get("mapa", MAPA_POR_DEFECTO)
    if not ruta.exists():
        sys.exit(f"ERROR: no existe el mapa de registros {ruta}")
    with ruta.open("rb") as f:
        mapa = tomllib.load(f)
    mapa["por_id"] = {p["id"]: p for p in mapa["param"]}
    return ruta, mapa


def hhmm(v: int) -> str:
    return f"{v // 100:02d}:{v % 100:02d}"


# ------------------------------------------------------------------ registro -> texto

def formatear(p: dict, r: dict[int, int]) -> str:
    v = r[p["reg"]]
    tipo = p["tipo"]
    if tipo == "num":
        escala = p.get("escala", 1)
        texto = f"{v * escala:.2f}".rstrip("0").rstrip(".") if escala != 1 else str(v)
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
        return "Activo (" + "".join(d if v >> (i + 1) & 1 else "-" for i, d in enumerate(DIAS)) + ")"
    if tipo == "fecha":
        a, b, c = r[p["reg"]], r[p["reg"] + 1], r[p["reg"] + 2]
        return f"{2000 + (a >> 8)}-{a & 0xFF:02d}-{b >> 8:02d} {b & 0xFF:02d}:{c >> 8:02d}:{c & 0xFF:02d}"
    return f"tipo desconocido '{tipo}' ({v})"


def formatear_carga_tou(v: int) -> str:
    """Bit 0 = red, bit 1 = generador. Otros bits se muestran en bruto."""
    partes = [n for b, n in ((0, "Red"), (1, "Gen")) if v >> b & 1]
    otros = v & ~0b11
    texto = " + ".join(partes) if partes else "No"
    return f"{texto} (otros bits: {otros})" if otros else texto


# ------------------------------------------------------------------ valor legible -> registro

def _bool(valor, nombre: str) -> bool:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str) and valor.strip().lower() in ("sí", "si", "no"):
        return valor.strip().lower() != "no"
    raise ErrorValor(f"{nombre}: se esperaba true/false, no {valor!r}")


def _rango(valor: float, mn, mx, nombre: str, unidad: str = "") -> None:
    if mn is not None and valor < mn or mx is not None and valor > mx:
        raise ErrorValor(f"{nombre}: {valor} {unidad} fuera de rango ({mn}-{mx} {unidad})".replace("  ", " "))


def a_registro(p: dict, valor, actual: int) -> int:
    """Convierte el valor de un perfil al valor del registro. 'actual' se usa para preservar otros bits."""
    nombre, tipo = p["id"], p["tipo"]
    if tipo == "num":
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise ErrorValor(f"{nombre}: se esperaba un número, no {valor!r}")
        _rango(valor, p.get("min"), p.get("max"), nombre, p.get("unidad", ""))
        return round(valor / p.get("escala", 1))
    if tipo == "enum":
        for clave, texto in p["opciones"].items():
            if str(valor).strip().lower() in (texto.lower(), clave):
                return int(clave)
        raise ErrorValor(f"{nombre}: {valor!r} no válido. Opciones: {', '.join(p['opciones'].values())}")
    if tipo == "switch":
        return 1 if _bool(valor, nombre) else 0
    if tipo == "bit":
        mascara = 1 << p["bit"]
        return actual | mascara if _bool(valor, nombre) else actual & ~mascara
    if tipo == "dias":
        if isinstance(valor, bool):
            return 0xFF if valor else 0
        texto = str(valor).upper()
        if len(texto) != 7 or any(c not in (d, "-") for c, d in zip(texto, DIAS)):
            raise ErrorValor(f"{nombre}: usa true/false o 7 caracteres tipo 'LMXJV--'")
        return 1 | sum(1 << (i + 1) for i, c in enumerate(texto) if c != "-")
    raise ErrorValor(f"{nombre}: el tipo '{tipo}' no se puede escribir")


def hora_a_registro(valor, nombre: str) -> int:
    try:
        h, m = (int(x) for x in str(valor).split(":"))
    except ValueError:
        raise ErrorValor(f"{nombre}: hora {valor!r} no válida, usa 'HH:MM'") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ErrorValor(f"{nombre}: hora {valor!r} fuera de rango")
    return h * 100 + m

