# deyeconfig

[English](README.md) | **Español**

Lee y modifica la configuración de inversores híbridos Deye a través del datalogger
Solarman en tu red local (Modbus TCP, puerto 8899), sin depender de la nube. Incluye
un servicio de modo tormenta que precarga la batería desde la red cuando hay
previsión de tormentas, para que la casa esté preparada ante un corte de luz.

## Contenido

- [Funcionalidades](#funcionalidades)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Prueba de conexión](#prueba-de-conexión)
- [Leer la configuración del inversor](#leer-la-configuración-del-inversor)
- [Perfiles](#perfiles)
- [Exportar la configuración actual](#exportar-la-configuración-actual)
- [Modo tormenta](#modo-tormenta)
- [Referencia de configuración](#referencia-de-configuración)
- [Copias de seguridad, estado y cómo volver atrás](#copias-de-seguridad-estado-y-cómo-volver-atrás)
- [Mapas de registros](#mapas-de-registros)
- [Resolución de problemas](#resolución-de-problemas)
- [Hoja de ruta](#hoja-de-ruta)
- [Créditos](#créditos)
- [Aviso](#aviso)

## Funcionalidades

- **Acceso local**: se comunica directamente con el inversor a través del stick
  Solarman (Modbus TCP), sin cuenta en la nube ni cuotas de API.
- **Lectura completa de la configuración**: batería, carga desde red y generador,
  modo de trabajo, protecciones de red, puerto GEN / SmartLoad y las 6 franjas
  Time Of Use.
- **Perfiles**: pequeños ficheros TOML con solo los parámetros que quieres cambiar,
  en unidades normales. Cada escritura se valida, se respalda y se verifica.
- **Exportación**: vuelca la configuración actual como perfil para guardarla o
  volver a ella.
- **Servicio de modo tormenta**: planifica una carga desde red en horas valle cuando
  hay previsión de tormentas, mantiene la batería cargada durante la tormenta y deja
  disponible toda la batería durante un corte.
- **Seguro por defecto**: no se escribe nada sin `--apply`, los parámetros críticos
  son de solo lectura, y antes de cada escritura se hace una copia de seguridad y
  después se vuelve a leer para verificar.

## Requisitos

- Un inversor híbrido Deye con datalogger (stick) Solarman WiFi/LAN en tu red local.
  Probado con un SUN-6K-SG03LP1-EU (ver [Mapas de registros](#mapas-de-registros)).
- La **dirección IP** y el **número de serie** del logger (el del logger, no el del
  inversor):
  - Número de serie: en la pegatina del stick o en la app Solarman (información del
    dispositivo).
  - Dirección IP: en la lista de dispositivos conectados de tu router. Conviene
    fijarla con una reserva DHCP. También puedes encontrarla buscando en tu red el
    dispositivo con el puerto TCP 8899 abierto.
- Python 3.11 o superior en un equipo de la misma red (basta con una pequeña VM Linux).
- Para el modo tormenta: acceso a Internet (previsión meteorológica) y, para la
  protección por corte, que el equipo y la red sigan alimentados durante los cortes
  (salida de respaldo del inversor o SAI).

## Instalación

```bash
git clone https://github.com/rafabolivar/deyeconfig.git
cd deyeconfig
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
```

Edita `config.toml`: como mínimo `[logger] ip` y `serial` y, para el modo tormenta,
`[storm] latitude` y `longitude`. Consulta la
[Referencia de configuración](#referencia-de-configuración).

Después comprueba la conexión:

```bash
.venv/bin/python deye_test_connection.py
```

## Estructura del proyecto

```
deye_test_connection.py   Prueba de conexión (solo lectura)
deye_read_config.py       Muestra toda la configuración y guarda una copia (solo lectura)
deye_apply.py             Aplica un perfil (simulación salvo con --apply)
deye_export.py            Exporta la configuración actual como perfil (solo lectura)
deye_storm.py             Modo tormenta (comprobación única o servicio)
deye_common.py            Común: carga de configuración, conexión, lectura de registros
deye_map.py               Común: gestión del mapa de registros y conversión de valores
maps/                     Mapas de registros por modelo de inversor (TOML)
profiles/examples/        Perfiles de ejemplo (winter, summer)
profiles/                 Tus perfiles (ignorados por git)
systemd/                  Unidad systemd del servicio de modo tormenta
config.example.toml       Plantilla de configuración
config.toml               Tu configuración (ignorada por git)
backups/                  Copias JSON de los registros del inversor (ignoradas por git)
state/                    Estado del modo tormenta (ignorado por git)
```

## Prueba de conexión

```bash
.venv/bin/python deye_test_connection.py
```

Lee el número de serie del inversor, el SOC y la tensión de la batería. Solo lectura.
Si el número de serie coincide con la etiqueta de tu inversor, la conexión funciona.

## Leer la configuración del inversor

```bash
.venv/bin/python deye_read_config.py          # muestra todo y guarda una copia
.venv/bin/python deye_read_config.py --raw    # añade id, registro y valor en bruto
.venv/bin/python deye_read_config.py --no-backup
```

Muestra toda la configuración y guarda en `backups/` una copia JSON con todos los
registros en bruto del rango de configuración. Solo lectura.

`--raw` muestra, para cada parámetro, el `id` que se usa en los perfiles, si es de
solo lectura, su número de registro y su valor en bruto.

## Perfiles

Un perfil (`profiles/*.toml`) indica solo los parámetros que se quieren cambiar, en
unidades normales (A, %, V, "HH:MM", true/false). El resto no se toca.

```bash
.venv/bin/python deye_apply.py profiles/winter.toml                # muestra qué cambiaría
.venv/bin/python deye_apply.py profiles/winter.toml --show         # lo mismo, explícito
.venv/bin/python deye_apply.py profiles/winter.toml --apply        # escribe (pide confirmación)
.venv/bin/python deye_apply.py profiles/winter.toml --apply --yes  # sin confirmación (cron)
```

Sin `--apply` nunca se escribe nada. Con `--apply`:

1. Se valida el perfil completo: que los parámetros existan, sean escribibles y estén
   en rango, que las horas sean válidas y que las horas de inicio de las franjas sean
   crecientes. Ante cualquier error no se escribe nada.
2. Se guarda una copia de seguridad de la configuración actual en `backups/`.
3. Se escriben solo los registros que cambian y se vuelven a leer para verificarlos.

### Formato de un perfil

```toml
description = "Winter: grid charging during off-peak hours up to 80 %"

[parameters]
grid_charge = true
grid_charge_current = 40
time_of_use = "MTWTFSS"

[[tou]]
slot = 1
time = "00:00"
power = 6000
soc = 80
grid_charge = true

[[tou]]
slot = 2
time = "08:00"
soc = 20
grid_charge = false
```

- `description`: opcional, se muestra al aplicar el perfil.
- `[parameters]`: cualquier parámetro escribible del mapa, por su `id`
  (lístalos con `deye_read_config.py --raw`). Tipos de valor:
  - números en la unidad del parámetro (`grid_charge_current = 40`, `shutdown_voltage = 46.5`)
  - nombres de opción para las listas (`work_mode = "Zero export to load"`)
  - `true` / `false` para los interruptores
  - `time_of_use`: `true`, `false` o los días activos con 7 caracteres, por ejemplo
    `"MTWTF--"` para lunes a viernes (iniciales en inglés: Monday ... Sunday)
- `[[tou]]`: franjas Time Of Use (1 a 6). Cada entrada necesita `slot` y cualquiera
  de `time` ("HH:MM", inicio de la franja), `power` (W), `voltage` (V), `soc` (%),
  `grid_charge` y `gen_charge` (true/false). Cada franja dura hasta el inicio de la
  siguiente; la franja 6 dura hasta la franja 1 del día siguiente.

Notas:

- Algunos parámetros son de solo lectura por seguridad (protecciones de red, tipo
  de batería, tensiones de carga...). Se controla con `writable` en el mapa.
- El registro de carga de cada franja tiene otros bits además de los de carga desde
  red y generador (algunos firmwares usan el bit 2). Se muestran como "other bits" y
  siempre se conservan al escribir.
- `profiles/examples/` contiene ejemplos alineados con los periodos de la tarifa
  2.0TD. Cópialos a `profiles/` y ajústalos: los ficheros que estén directamente en
  `profiles/` quedan fuera del repositorio, así que tu configuración personal nunca
  se publica.

## Exportar la configuración actual

```bash
.venv/bin/python deye_export.py                            # muestra el perfil en pantalla
.venv/bin/python deye_export.py -o profiles/current.toml   # lo guarda en un fichero
.venv/bin/python deye_export.py -o profiles/current.toml --force   # sobrescribe si existe
```

Crea un perfil con los valores actuales del inversor: todos los parámetros
escribibles y las 6 franjas Time Of Use, listo para aplicarlo con `deye_apply.py`.
Los parámetros de solo lectura aparecen como comentarios. La extensión `.toml` se
añade automáticamente si falta. Solo lectura.

Buena práctica: exporta tu configuración antes de experimentar
(`deye_export.py -o profiles/original.toml`) para poder volver siempre a ella.

## Modo tormenta

`deye_storm.py` precarga la batería desde la red cuando hay previsión de tormentas o
lluvia muy intensa ([Open-Meteo](https://open-meteo.com/), gratuito, sin clave de
API), para que la casa esté preparada ante un posible corte de luz. Funciona como
servicio.

### Cómo funciona

Todos los valores se configuran en la sección `[storm]` de `config.toml` (valores
por defecto entre paréntesis).

1. **Ventana de planificación** (de `planning_time` a `ready_by`, 00:00-07:30, el
   periodo valle): en cada comprobación de la previsión, si hay tormenta prevista en
   las próximas `forecast_hours` (24), calcula cuánto se tarda en cargar desde el SOC
   actual hasta `target_soc` (80 %) y programa la carga desde red para terminar a
   `ready_by`, o `storm_margin_minutes` (30) antes de la tormenta si esta llega antes.
   Si no hay tiempo suficiente, carga inmediatamente.
2. **Tormenta en curso** (la previsión para la hora actual cumple el criterio):
   carga inmediatamente mientras haya red.
3. **Respaldo** fuera de la ventana de planificación: si hay tormenta prevista en las
   próximas `backup_hours` (4) y el SOC está por debajo de `backup_trigger_soc`
   (50 %), carga inmediatamente.
4. **Mantenimiento**: en modo tormenta, `target_soc` se mantiene como nivel mínimo
   de la batería.
5. **Siguiente tormenta**: si pasa la tormenta para la que se planificó la carga y hay
   otra prevista, deja de cargar y planifica la carga para la siguiente. El plan solo
   se recalcula cuando cambia la tormenta objetivo, así la carga no se enciende y
   apaga a medida que sube el SOC.
6. **Protección por corte**: en modo tormenta se comprueba el estado de la red cada
   `grid_check_interval` segundos (60). Si se va la red, todas las franjas Time Of
   Use bajan a `outage_soc` (15 %) para que toda la batería esté disponible, haga lo
   que haga el inversor con las franjas en modo isla. Cuando vuelve la red, la
   batería se recarga hasta `target_soc` (`recharge_after_outage`).
7. **Fin**: `grace_hours` (1) después de la última hora de tormenta prevista, si la
   tormenta deja de estar prevista (dos comprobaciones seguidas), o tras `max_hours`
   (36) como límite de seguridad, se restaura la configuración guardada al entrar en
   modo tormenta.

Qué cuenta como tormenta: los códigos meteorológicos de `storm_codes` (WMO 95, 96 y
99 = tormenta) o lluvia de al menos `heavy_rain_mm` (10 mm/h) con una probabilidad
de al menos `min_probability` (50 %).

El tiempo de carga se estima a partir de la capacidad de la batería (leída del
inversor), `battery_nominal_voltage`, la corriente de carga desde red configurada en
el inversor, la tensión de la batería y `charge_efficiency`. Si la estimación se
queda corta, la carga simplemente continúa un poco después de `ready_by` (el periodo
valle dura hasta las 08:00).

### Distribución de las franjas Time Of Use

El servicio calcula él mismo las franjas Time Of Use. Ejemplo: batería al 15 %,
tormenta prevista a las 18:00, planificación a las 00:05:

| Franja | Inicio | SOC | Carga desde red | Para qué |
|---|---|---|---|---|
| 1 | 00:00 | 15 % | No | Uso normal hasta que empieza la carga |
| 2 | 03:55 | 80 % | Sí | Cargar al 80 % antes de las 07:30 |
| 3-6 | 08:00, 12:00, 16:00, 20:00 | 80 % | Sí | Mantener el 80 % hasta que pase la tormenta |

La franja 1 usa el SOC mínimo de tu configuración normal. Una vez empezada la carga,
todas las franjas pasan al 80 % con carga desde red.

### Escrituras en el inversor

En cada comprobación, el servicio compara la configuración deseada con la del
inversor y solo escribe cuando hay que cambiar algo (normalmente al entrar en modo
tormenta, al irse y volver la red y al terminar). Antes de cada escritura se guarda
una copia en `backups/`, después se verifica, y hay un límite diario
(`max_writes_per_day`, 20). Si el inversor cambia un valor por su cuenta, la
siguiente comprobación lo corrige.

### Ejecución

```bash
.venv/bin/python deye_storm.py                    # comprobación única, muestra qué haría
.venv/bin/python deye_storm.py --apply            # comprobación única, actúa sobre el inversor
.venv/bin/python deye_storm.py --daemon --apply   # como servicio (lo usa systemd)
.venv/bin/python deye_storm.py --plan-now         # ejecuta la planificación ahora
```

Opciones de prueba (solo en una simulación única; no se escribe ni se guarda nada):

```bash
.venv/bin/python deye_storm.py --assume-storm 18                          # simula una tormenta dentro de 18 h
.venv/bin/python deye_storm.py --assume-outage                            # simula que no hay red
.venv/bin/python deye_storm.py --at '2026-10-08 00:05' --assume-storm 18  # simula que son las 00:05
```

### Instalar el servicio

Ajusta `User` y las rutas de `systemd/deye-storm.service` si hace falta y después:

```bash
sudo cp systemd/deye-storm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deye-storm.service
```

Comandos útiles:

```bash
systemctl status deye-storm.service                     # ¿está funcionando?
journalctl -u deye-storm.service -f                     # sigue el registro en directo (Ctrl+C para salir)
journalctl -u deye-storm.service --since "today 00:00"  # qué ha hecho esta noche
sudo systemctl restart deye-storm.service               # tras cambiar config.toml o el código
sudo systemctl stop deye-storm.service                  # lo detiene (el estado se conserva)
```

El estado se guarda en `state/`, así que el servicio continúa donde estaba tras un
reinicio del servicio o de la máquina. Después de cambiar `config.toml`, reinicia el
servicio.

**Requisito para la protección por corte:** el equipo que ejecuta el servicio y la
red tienen que seguir alimentados durante un corte (por ejemplo, conectados a la
salida de respaldo del inversor o a un SAI). El logger se alimenta del propio inversor.

## Referencia de configuración

`config.toml` (copia de `config.example.toml`, nunca se sube al repositorio):

### `[logger]`

| Clave | Por defecto | Descripción |
|---|---|---|
| `ip` | | Dirección IP del logger Solarman |
| `serial` | | Número de serie del logger (número, sin comillas) |
| `port` | 8899 | Puerto Modbus TCP del logger |

### `[modbus]`

| Clave | Por defecto | Descripción |
|---|---|---|
| `slave_id` | 1 | ID de esclavo Modbus del inversor |
| `timeout` | 10 | Tiempo de espera de la conexión (s) |

### `[inverter]`

| Clave | Por defecto | Descripción |
|---|---|---|
| `map` | `maps/deye_sg0xlp1.toml` | Mapa de registros de tu modelo de inversor |

### `[storm]`

| Clave | Por defecto | Descripción |
|---|---|---|
| `latitude`, `longitude` | | Ubicación para la previsión (grados decimales) |
| `timezone` | `Europe/Madrid` | Zona horaria local |
| `forecast_interval` | 900 | Segundos entre comprobaciones de la previsión |
| `grid_check_interval` | 60 | Segundos entre comprobaciones de la red en modo tormenta |
| `planning_time` | `"00:00"` | Inicio de la ventana de planificación (inicio del valle) |
| `ready_by` | `"07:30"` | Fin de la ventana de planificación; la carga debe terminar antes |
| `forecast_hours` | 24 | Horas por delante en las que buscar tormentas |
| `storm_margin_minutes` | 30 | Terminar la carga este tiempo antes de la tormenta, si llega antes de `ready_by` |
| `target_soc` | 80 | SOC al que cargar y que mantener en modo tormenta (%) |
| `outage_soc` | 15 | SOC mínimo mientras no hay red en modo tormenta (%) |
| `recharge_after_outage` | true | Recargar en cuanto vuelve la red (false: esperar a la siguiente ventana de planificación) |
| `backup_enabled` | true | Activa el respaldo fuera de la ventana de planificación |
| `backup_hours` | 4 | Respaldo: tormenta dentro de este número de horas |
| `backup_trigger_soc` | 50 | Respaldo: SOC por debajo de este valor (%) |
| `storm_codes` | [95, 96, 99] | Códigos meteorológicos WMO considerados tormenta |
| `heavy_rain_mm` | 10.0 | Lluvia considerada muy intensa (mm/h) |
| `min_probability` | 50 | Probabilidad mínima para que cuente la lluvia intensa (%) |
| `grace_hours` | 1 | Restaurar este número de horas después de la última hora de tormenta |
| `max_hours` | 36 | Límite de seguridad: horas máximas en modo tormenta |
| `battery_nominal_voltage` | 51.2 | Se usa para estimar el tiempo de carga |
| `charge_efficiency` | 0.9 | Se usa para estimar el tiempo de carga |
| `max_writes_per_day` | 20 | Límite de seguridad de escrituras en el inversor por día |

## Copias de seguridad, estado y cómo volver atrás

- **`backups/`**: ficheros JSON con todos los registros de configuración en bruto,
  creados por `deye_read_config.py` y antes de cada escritura de `deye_apply.py` y
  `deye_storm.py`. Cada fichero indica la fecha y el motivo.
- **`state/storm.json`**: estado del modo tormenta (activo o no, carga planificada,
  tormenta objetivo, corte, número de escrituras del día).
- **`state/pre_storm.toml`**: perfil con la configuración guardada al entrar en modo
  tormenta; se restaura al terminar.

Para volver manualmente a una configuración conocida:

```bash
sudo systemctl stop deye-storm.service                    # si el modo tormenta está en marcha
.venv/bin/python deye_apply.py profiles/original.toml --apply
```

Si el modo tormenta estaba activo, `state/pre_storm.toml` es la configuración que
tenías antes de que empezara y se puede aplicar del mismo modo. Borra
`state/storm.json` antes de volver a arrancar el servicio para que empiece en modo
normal.

## Mapas de registros

Los registros de cada modelo se definen en `maps/` (TOML), separados del código.
El mapa se elige en `config.toml` (`[inverter] map = ...`). Para añadir parámetros o
soportar otro modelo, edita o crea un mapa. La cabecera del fichero documenta el
formato (`group`, `id`, `writable`, `min`/`max`, `reg`, `name`, `type`, `scale`,
`unit`, `options`, `bit`).

| Mapa | Modelos | Probado con |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye híbrido monofásico BT (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

Los registros de datos en vivo que usa el modo tormenta (tensión de batería 183,
SOC 184, estado de la red 194) están definidos en `deye_storm.py`.

Antes de escribir en un modelo no probado, compara la salida de `deye_read_config.py`
con la pantalla o la app del inversor: los modelos monofásicos más antiguos usan otra
distribución de registros.

## Resolución de problemas

- **La conexión agota el tiempo de espera**: comprueba la IP del logger, que el
  puerto 8899 responda (`nc -zv <ip> 8899`) y que el equipo esté en la misma red.
  Algunas versiones de firmware del logger desactivan este puerto.
- **Conecta pero fallan las lecturas**: el número de serie de `config.toml` tiene que
  ser el del logger, no el del inversor.
- **Valores sin sentido** (horas como 00:92, corrientes enormes): el mapa de
  registros no corresponde a tu modelo o firmware. No escribas nada; revisa el mapa.
- **El modo tormenta no hace nada**: ejecuta `deye_storm.py` sin opciones para ver
  qué decidiría ahora y revisa el registro con `journalctl -u deye-storm.service`.
- **"daily write limit reached"**: algo está cambiando valores repetidamente. Revisa
  el registro antes de subir `max_writes_per_day`.

## Hoja de ruta

- Meteocat (Servei Meteorològic de Catalunya) como fuente adicional: detección de
  rayos (XDDE), lluvia medida en estaciones cercanas (XEMA) y su propia previsión,
  para confirmar las tormentas que están ocurriendo de verdad.

## Créditos

- Información de registros: protocolo Modbus Deye V118 y la definición
  `deye_hybrid.yaml` de [ha-solarman](https://github.com/davidrapan/ha-solarman).
- Comunicación con el logger: [pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5).
- Previsión meteorológica: [Open-Meteo](https://open-meteo.com/).

## Aviso

Escribir valores incorrectos en un inversor puede afectar a su funcionamiento o a la
batería. Úsalo bajo tu propia responsabilidad: ejecuta siempre primero sin `--apply`,
revisa los cambios y guarda una copia de tu configuración (`deye_export.py`).

