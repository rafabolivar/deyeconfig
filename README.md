# Deye Solarman Tool

Herramienta para leer (y más adelante modificar) parámetros de inversores Deye
a través del datalogger Solarman en red local (Modbus TCP, puerto 8899), sin depender de la nube.

## Instalación

```bash
git clone <url-del-repo>
cd deye
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edita `config.toml` con la IP y el número de serie de tu logger.

## Prueba de conexión

```bash
.venv/bin/python deye_test_conexion.py
```

Solo lee registros; no modifica nada en el inversor.

Requiere Python 3.11 o superior.

## Leer la configuración del inversor

```bash
.venv/bin/python deye_leer_config.py          # muestra todo y guarda backup
.venv/bin/python deye_leer_config.py --raw    # añade el valor en bruto de cada registro
```

Muestra toda la configuración: batería, carga desde red y generador, modo de trabajo,
protecciones de red, puerto GEN/SmartLoad y las 6 franjas Time Of Use. Guarda además
una copia de seguridad en JSON con todos los registros en bruto en `backups/`
(excluida del repositorio). Solo lee; no modifica nada.

## Mapas de registros

Los registros de cada modelo se definen en `mapas/` (formato TOML), separados del código.
El mapa se elige en `config.toml` (`[inversor] mapa = ...`). Para añadir parámetros
o soportar otro modelo, basta con editar o crear un mapa.

| Mapa | Modelos | Probado con |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye híbrido monofásico BT (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

Fuentes: protocolo Modbus Deye V118 y la definición `deye_hybrid.yaml` de
[ha-solarman](https://github.com/davidrapan/ha-solarman).
