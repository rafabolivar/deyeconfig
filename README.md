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
.venv/bin/python deye_leer_config.py
```

Muestra los parámetros de batería, modo de trabajo y las 6 franjas Time Of Use,
y guarda una copia de seguridad en JSON en `backups/` (excluida del repositorio).
Solo lee; no modifica nada.

Probado con: Deye SUN-6K-SG03LP1-EU (mapa de registros de configuración 200-280).
