"""
Funciones comunes: carga de configuración y conexión con el inversor Deye
a través del datalogger Solarman (Modbus TCP).
"""

import sys
import tomllib
from pathlib import Path

from pysolarmanv5 import PySolarmanV5

DEFAULT_CONFIG = Path(__file__).with_name("config.toml")


def cargar_config(ruta: Path = DEFAULT_CONFIG) -> dict:
    if not ruta.exists():
        sys.exit(f"ERROR: no existe {ruta}. Copia config.example.toml como config.toml y rellénalo.")
    with ruta.open("rb") as f:
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

    errores = []
    if not conf["ip"]:
        errores.append("falta [logger] ip")
    if not isinstance(conf["serial"], int) or conf["serial"] <= 0:
        errores.append("[logger] serial debe ser el número de serie del logger (número, sin comillas)")
    if errores:
        sys.exit("ERROR en la configuración:\n  - " + "\n  - ".join(errores))
    return conf


def conectar(conf: dict) -> PySolarmanV5:
    print(f"Conectando a {conf['ip']}:{conf['port']} (logger {conf['serial']})...")
    try:
        return PySolarmanV5(conf["ip"], conf["serial"], port=conf["port"],
                            mb_slave_id=conf["slave_id"], socket_timeout=conf["timeout"],
                            verbose=False)
    except Exception as e:
        sys.exit(f"ERROR: no se pudo abrir la conexión: {e}")


def leer_bloque(inv: PySolarmanV5, inicio: int, cantidad: int, trozo: int = 40) -> dict[int, int]:
    """Lee 'cantidad' holding registers desde 'inicio' en trozos pequeños. Devuelve {registro: valor}."""
    datos = {}
    for desde in range(inicio, inicio + cantidad, trozo):
        n = min(trozo, inicio + cantidad - desde)
        valores = inv.read_holding_registers(register_addr=desde, quantity=n)
        datos.update({desde + i: v for i, v in enumerate(valores)})
    return datos


def leer_serie_inversor(inv: PySolarmanV5) -> str:
    """Registros 3-7: número de serie del inversor en ASCII (2 caracteres por registro)."""
    regs = inv.read_holding_registers(register_addr=3, quantity=5)
    return "".join(chr(r >> 8) + chr(r & 0xFF) for r in regs).strip("\x00 ")

