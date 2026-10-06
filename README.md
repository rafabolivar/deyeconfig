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

## Aplicar un perfil de configuración

Un perfil (`perfiles/*.toml`) indica solo los parámetros que se quieren cambiar,
en unidades normales (A, %, V, "HH:MM", true/false). El resto no se toca.

```bash
.venv/bin/python deye_aplicar.py perfiles/invierno.toml             # muestra qué cambiaría
.venv/bin/python deye_aplicar.py perfiles/invierno.toml --show   # ídem, explícito
.venv/bin/python deye_aplicar.py perfiles/invierno.toml --apply   # escribe (pide confirmación)
.venv/bin/python deye_aplicar.py perfiles/invierno.toml --apply --yes   # sin confirmación (cron)
```

Sin `--apply` nunca se escribe nada. Con `--apply`:

1. Se valida el perfil completo (parámetros existentes, escribibles y en rango).
   Ante cualquier error no se escribe nada.
2. Se guarda una copia de seguridad de la configuración actual en `backups/`.
3. Se escriben solo los registros que cambian y se verifican leyéndolos de nuevo.

Ejemplo de perfil:

```toml
descripcion = "Invierno: carga desde red en valle hasta el 80 %"

[parametros]
carga_red = true
corriente_carga_red = 40

[[tou]]
franja = 1
hora = "00:00"
soc = 80
carga_red = true
```

- `[parametros]`: cualquier parámetro escribible del mapa, por su `id`.
  Consulta los ids con `deye_leer_config.py --raw`.
- `[[tou]]`: franjas Time Of Use (1 a 6) con `hora`, `potencia`, `tension`, `soc`,
  `carga_red` y `carga_gen`. Las horas de inicio deben ser crecientes.

Por seguridad, algunos parámetros son de solo lectura (protecciones de red, tipo de
batería, tensiones de carga...). Se controla con `escribible` en el mapa.

Los perfiles de `perfiles/` son ejemplos: revísalos y ajústalos a tu instalación
antes de aplicarlos.

## Mapas de registros

Los registros de cada modelo se definen en `mapas/` (formato TOML), separados del código.
El mapa se elige en `config.toml` (`[inversor] mapa = ...`). Para añadir parámetros
o soportar otro modelo, basta con editar o crear un mapa.

| Mapa | Modelos | Probado con |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye híbrido monofásico BT (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

Fuentes: protocolo Modbus Deye V118 y la definición `deye_hybrid.yaml` de
[ha-solarman](https://github.com/davidrapan/ha-solarman).
