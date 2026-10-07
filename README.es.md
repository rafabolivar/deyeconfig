# Deyeconfig

[English](README.md) | **Español**

Lee y modifica la configuración de inversores híbridos Deye a través del datalogger
Solarman en tu red local (Modbus TCP, puerto 8899), sin depender de la nube. Incluye
un modo automático que carga la batería en las horas más baratas según los precios
reales de la electricidad de cada hora y protege la casa ante tormentas y cortes de luz.

## Contenido

- [Funcionalidades](#funcionalidades)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Prueba de conexión](#prueba-de-conexión)
- [Leer la configuración del inversor](#leer-la-configuración-del-inversor)
- [Perfiles](#perfiles)
- [Exportar la configuración actual](#exportar-la-configuración-actual)
- [Modo automático](#modo-automático)
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
- **Modo automático**: planifica la carga desde red con los precios PVPC reales de
  cada hora, la previsión solar y el consumo, mantiene una reserva cuando hay
  previsión de tormentas y deja disponible toda la batería durante un corte.
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
- Para el modo automático: acceso a Internet (precios y previsión meteorológica) y, para la
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

Edita `config.toml`: como mínimo `[logger] ip` y `serial` y, para el modo automático,
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
deye_optimizer.py         Modo automático: optimizador por precios con protección ante tormentas y cortes
deye_common.py            Común: carga de configuración, conexión, lectura de registros
deye_map.py               Común: gestión del mapa de registros y conversión de valores
deye_tariff.py            Común: periodos de la tarifa (precios de respaldo), fines de semana y festivos
maps/                     Mapas de registros por modelo de inversor (TOML)
profiles/examples/        Perfiles de ejemplo (afternoon, autumn, winter, summer)
profiles/                 Tus perfiles (ignorados por git)
systemd/                  Unidad systemd del servicio de modo automático
config.example.toml       Plantilla de configuración
config.toml               Tu configuración (ignorada por git)
backups/                  Copias JSON de los registros del inversor (ignoradas por git)
state/                    Estado del modo automático, caché de precios y planes (ignorado por git)
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
3. Se escriben solo los registros que cambian, en bloques contiguos (la tabla Time
   Of Use en una sola operación), y se vuelven a leer unos segundos después para
   verificarlos.

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
  2.0TD (ver [Perfiles de temporada](#perfiles-de-temporada)). Cópialos a `profiles/` y ajústalos: los ficheros que estén directamente en
  `profiles/` quedan fuera del repositorio, así que tu configuración personal nunca
  se publica.

### Perfiles de temporada

Ejemplos para días de sol en otoño e invierno (`profiles/examples/autumn.toml` y
`winter.toml`):

| Franja | Inicio | Periodo | SOC | Carga desde red | Efecto |
|---|---|---|---|---|---|
| 1 | 00:00 | Valle | 60 % otoño / 70 % invierno | Sí | Carga a precio valle; por la noche la batería no baja de ese nivel |
| 2 | 08:00 | Llano | 15 % | No | Batería disponible para empezar el día |
| 3 | 10:00 | Punta | 15 % | No | Batería disponible; el sol la va cargando |
| 4 | 14:00 | Llano | 15 % | No | Batería disponible |
| 5 | 18:00 | Punta | 15 % | No | Batería disponible para la punta de la tarde |
| 6 | 22:00 | Llano | 15 % | No | Batería disponible |

- La carga nocturna da batería para las primeras horas del día, cuando el sol aún
  es débil y la energía de la red es cara, y deja sitio para el excedente solar:
  cargar más llenaría la batería con energía de la red y el excedente se
  exportaría a precio bajo. En otoño hay más sol, así que carga menos.
- Por la noche la casa usa la energía barata de la red en lugar de la batería.
- Carga suave a 25 A (unas 4-4,5 horas desde el 15 %) y consumo de red limitado a 4000 W
  (*peak shaving*) para no superar la potencia contratada.
- Límites de corriente de la batería por debajo de los umbrales de protección del
  BMS (100 A durante 3 s en carga, 100 A durante 30 s en descarga): 90 A de carga y
  95 A de descarga. Por encima del umbral del BMS la batería se desconectaría, y
  durante un corte dejaría la casa sin luz. Comprueba los valores de tu propio BMS
  antes de copiarlos.
- Estos perfiles son para uso manual. Mientras el servicio de modo automático
  funciona, él mismo gestiona las franjas Time Of Use y las sobrescribe en el
  siguiente plan.

`profiles/examples/summer.toml` es un ejemplo anterior sin carga desde red.

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

## Modo automático

`deye_optimizer.py` es un servicio que gestiona la batería por sí solo: planifica la
carga desde red con los **precios PVPC reales de cada hora**, la previsión solar, el
consumo previsto y el SOC, protege la casa ante tormentas y cortes, y escribe las
franjas Time Of Use en el inversor.

### Por qué precios reales

Los periodos fijos de la tarifa (valle, llano y punta) solo determinan una parte del
precio (peajes y cargos). El precio PVPC de cada hora incluye además el precio del
mercado mayorista, que baja a mediodía por la generación solar. En
septiembre-octubre de 2026 las 3 horas más baratas del día cayeron a mediodía o por
la tarde en 27 de 31 días (unos 0,06 €/kWh de 14 a 17 h, frente a 0,18 de noche y
0,33 de 19 a 21 h). Los precios salen de REData, la API pública de Red Eléctrica
(sin clave).

### Cómo funciona

- **Cada `interval` segundos (900), plan completo:**
  1. Lee el SOC, la capacidad y la tensión de la batería.
  2. Obtiene los precios PVPC de hoy y de mañana (los de mañana se publican hacia
     las 20:15; hasta entonces se usan los del día anterior como estimación, y los
     precios de respaldo de `[tariff]` si no hay nada más) y, en una sola consulta a
     Open-Meteo, la previsión solar sobre el plano de las placas y la de tormentas.
  3. **Protección contra tormentas**: desde `reserve_lead_hours` (0,5) antes de cada
     hora de tormenta hasta `grace_hours` (1) después, la batería tiene que estar en
     `reserve_soc` (80 %) o por encima. Quedarse por debajo tiene un coste alto en el
     cálculo (`shortfall_penalty`), así que el plan carga en las horas más baratas
     antes de la tormenta, y en punta solo lo justo si no queda otra.
  4. Simula la batería hora a hora y busca las horas en las que **cargar** desde la
     red y las horas en las que **reservar** la batería (la casa usa la red mientras
     está barata) que minimizan el coste total, teniendo en cuenta las pérdidas de la
     batería, un coste de desgaste por kWh y el valor de la energía que queda al
     final. El sol carga primero; la red solo completa lo que compensa comprar.
  5. Construye las 6 franjas Time Of Use y la corriente de carga desde red (solo la
     necesaria, hasta `max_grid_charge_current`) y las escribe solo si son distintas
     de las del inversor.
- **Cada `grid_check_interval` segundos (60), protección por corte:** si se va la
  red, todas las franjas bajan a `outage_soc` (15 %) en el acto, para que toda la
  batería esté disponible. Cuando vuelve la red, el plan se recalcula
  inmediatamente.

Qué cuenta como tormenta: los códigos meteorológicos de `storm_codes` (WMO 95, 96 y
99 = tormenta) o lluvia de al menos `heavy_rain_mm` (10 mm/h) con una probabilidad
de al menos `min_probability` (50 %).

### Franjas Time Of Use

Cada hora del plan acaba con una de tres acciones:

| Acción | Franja | Efecto |
|---|---|---|
| Cargar | Carga desde red activada, SOC = objetivo | El sol primero; la red completa hasta el objetivo |
| Reservar | Sin carga desde red, SOC = nivel actual | La batería no se usa; la casa tira de la red mientras está barata |
| Usar | Sin carga desde red, SOC = mínimo (15 %, o la reserva de tormenta) | La batería alimenta la casa |

Las 24 horas se agrupan en las 6 franjas del inversor, uniendo las horas vecinas que
menos estropean el plan (las horas de carga nunca se mueven). Ejemplo de plan en un
día normal de octubre: cargar de 15:00 a 17:00 (las horas más baratas) hasta el
100 %, y batería disponible el resto del día.

### Escrituras en el inversor

Los SOC de las franjas se redondean hacia arriba a pasos de `soc_step` (5 %), así que
los cambios pequeños del plan no reescriben el inversor. Los registros se escriben
en bloques contiguos y se vuelven a leer para verificarlos, antes de cada escritura
se guarda una copia en `backups/`, y hay un límite diario (`max_writes_per_day`, 24)
que nunca se aplica a la protección por corte. Los límites de corriente de la
batería (`max_charge_current`, 90 A; `max_discharge_current`, 95 A) y el límite de
potencia de red (`grid_power_limit`, 4000 W, *peak shaving* del inversor) se
escriben con cada plan.

### Ejecución

```bash
.venv/bin/python deye_optimizer.py                    # plan único, simulación
.venv/bin/python deye_optimizer.py --apply            # plan único, lo escribe
.venv/bin/python deye_optimizer.py --daemon --apply   # servicio (lo usa systemd)
.venv/bin/python deye_optimizer.py --assume-storm 6   # simulación con tormenta dentro de 6 h (prueba)
.venv/bin/python deye_optimizer.py --assume-outage    # simulación sin red (prueba)
```

El último plan se guarda en `state/optimizer_plan.json` y cada plan queda registrado
en `state/optimizer_log.csv`.

### Instalar el servicio

Ajusta `User` y las rutas de `systemd/deye-optimizer.service` si hace falta y después:

```bash
sudo cp systemd/deye-optimizer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deye-optimizer.service
```

Comandos útiles:

```bash
systemctl status deye-optimizer.service             # ¿está funcionando?
journalctl -u deye-optimizer.service -f             # sigue el registro en directo (Ctrl+C para salir)
journalctl -u deye-optimizer.service --since today  # qué ha hecho hoy
sudo systemctl restart deye-optimizer.service       # tras cambiar config.toml o el código
sudo systemctl stop deye-optimizer.service          # lo detiene (mandan los perfiles manuales)
```

Mientras el servicio funciona, él gestiona las franjas Time Of Use, así que los
perfiles aplicados a mano se sobrescriben en el siguiente plan. Para usar perfiles
manuales, detén el servicio.

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
| `latitude`, `longitude` | | Ubicación para las previsiones (grados decimales) |
| `timezone` | `Europe/Madrid` | Zona horaria local |
| `enabled` | true | Activa o desactiva la protección contra tormentas (la optimización por precios funciona siempre) |
| `grid_check_interval` | 60 | Segundos entre comprobaciones de la red (detección de cortes) |
| `storm_codes` | [95, 96, 99] | Códigos meteorológicos WMO considerados tormenta |
| `heavy_rain_mm` | 10.0 | Lluvia considerada muy intensa (mm/h) |
| `min_probability` | 50 | Probabilidad mínima para que cuente la lluvia intensa (%) |
| `reserve_soc` | 80 | Nivel mínimo de batería alrededor de una tormenta (%) |
| `reserve_lead_hours` | 0.5 | La reserva se aplica desde estas horas antes de cada hora de tormenta |
| `grace_hours` | 1 | ... hasta estas horas después |
| `shortfall_penalty` | 2.0 | Coste (€ por kWh y hora) de estar por debajo de la reserva |
| `outage_soc` | 15 | Nivel de batería permitido mientras no hay red (%) |

### `[tariff]`

Solo como respaldo, cuando no hay precios PVPC reales disponibles.

| Clave | Por defecto | Descripción |
|---|---|---|
| `fallback_prices` | valle 0,18, llano 0,15, punta 0,25 | Precio representativo de cada periodo (€/kWh) |
| `periods` | 2.0TD | Periodos de los días laborables: `["HH:MM", "off-peak" \| "mid" \| "peak"]`, el primero a las 00:00, como máximo 6 |
| `weekend_off_peak` | true | Sábados y domingos son valle todo el día |
| `holidays` | Festivos nacionales de fecha fija | Días valle: `"MM-DD"` (todos los años) o `"YYYY-MM-DD"` |

### `[optimizer]`

| Clave | Por defecto | Descripción |
|---|---|---|
| `interval` | 900 | Segundos entre planes |
| `pv_kwp` | 3.535 | Potencia pico de las placas (kWp) |
| `pv_performance` | 0.54 | Producción real / teórica según la radiación sobre las placas |
| `panel_tilt`, `panel_azimuth` | 35, -45 | Inclinación (grados) y orientación (0 = sur, -90 = este) |
| `min_soc`, `max_soc` | 15, 100 | Rango de batería que usa el plan (%) |
| `charge_efficiency`, `discharge_efficiency` | 0.95, 0.95 | Eficiencias de la batería |
| `cycle_cost` | 0.01 | Coste de desgaste de la batería por kWh descargado (€) |
| `export_price` | 0.04 | Precio cobrado por la energía exportada (€/kWh) |
| `grid_power_limit` | 4000 | Consumo máximo de red (W, *peak shaving* del inversor) |
| `max_grid_charge_current` | 65 | Corriente máxima de carga desde red (A) |
| `max_charge_current`, `max_discharge_current` | 90, 95 | Límites de corriente de la batería (A) |
| `horizon_hours` | 36 | Horas que planifica (limitadas por los precios publicados) |
| `load_profile` | unos 12,7 kWh/día | Consumo previsto (kW) de cada hora, de 00 a 23 |

La ubicación y la zona horaria se toman de `[storm]` si no se indican en `[optimizer]`.

## Copias de seguridad, estado y cómo volver atrás

- **`backups/`**: ficheros JSON con todos los registros de configuración en bruto,
  creados por `deye_read_config.py` y antes de cada escritura de `deye_apply.py` y
  `deye_optimizer.py`. Cada fichero indica la fecha y el motivo.
- **`state/optimizer.json`**: estado del modo automático (corte, número de
  escrituras del día).
- **`state/prices.json`**: caché de los precios PVPC de los últimos días.
- **`state/optimizer_plan.json`** y **`state/optimizer_log.csv`**: último plan e
  historial de planes.

Para volver manualmente a una configuración conocida:

```bash
sudo systemctl stop deye-optimizer.service                # si no, reescribe las franjas
.venv/bin/python deye_apply.py profiles/original.toml --apply
```

Vuelve a arrancar el servicio (`sudo systemctl start deye-optimizer.service`) para
volver al modo automático.

## Mapas de registros

Los registros de cada modelo se definen en `maps/` (TOML), separados del código.
El mapa se elige en `config.toml` (`[inverter] map = ...`). Para añadir parámetros o
soportar otro modelo, edita o crea un mapa. La cabecera del fichero documenta el
formato (`group`, `id`, `writable`, `min`/`max`, `reg`, `name`, `type`, `scale`,
`unit`, `options`, `bit`).

| Mapa | Modelos | Probado con |
|---|---|---|
| `deye_sg0xlp1.toml` | Deye híbrido monofásico BT (SG03LP1, SG04LP1, SG05LP1...) | SUN-6K-SG03LP1-EU |

Los registros de datos en vivo que usa el modo automático (tensión de batería 183,
SOC 184, estado de la red 194, capacidad de la batería 204) están definidos en
`deye_optimizer.py`.

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
- **El modo automático hace algo inesperado**: ejecuta `deye_optimizer.py` sin
  opciones para ver el plan actual hora a hora (simulación) y revisa el registro con
  `journalctl -u deye-optimizer.service`.
- **Se deshacen los cambios de perfiles manuales**: el servicio de modo automático
  gestiona las franjas Time Of Use. Detenlo para usar perfiles manuales.
- **"daily write limit reached"**: algo está cambiando valores repetidamente. Revisa
  el registro antes de subir `max_writes_per_day`.

## Hoja de ruta

- Aprender el perfil de consumo del historial del inversor, en lugar de un valor
  fijo de la configuración.
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

