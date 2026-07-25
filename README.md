# Pañol IoT — Control de acceso y auditabilidad

Sistema IoT de control de acceso a un pañol: puerta con RFID + solenoide + reed + PIR (ESP32 #1) y,
más adelante, armarios de CPU con sensores IR (ESP32 #2). Un servidor concentra la **máquina de
estados de sesiones** y deriva alarmas/tickets a EMATP.

**Criterio de diseño:** profesional, robusto y **a prueba de fallos de manera sencilla**
(tolerante a cortes de suministro). Ver [docs/DISENO.md](docs/DISENO.md).

## Estructura

```
server/            Cerebro
  engine/          Máquina de estados PURA (sin transporte ni DB)
  db/              PostgreSQL: esquema, atribución por timestamp, idempotencia
  servicio.py      Une motor + persistencia
  api/app.py       Shell HTTP (Flask) — curl-testeable
  puente_mqtt.py   Shell MQTT — mismo motor, otro transporte
  planificador.py  Latido: tareas periódicas del motor + purga diaria
  retencion.py     Política de retención (docs/PERSISTENCIA.md)
  adapters/        emisor_ematp.py: bandeja de salida hacia EMATP
  tests/           Simulador de escenarios (docs/DISENO.md §5)
firmware/
  nodo_panol/      ESP32 #1: FSM local, cola en flash, WiFi
  nodo_armarios/   ESP32 #2 (MCP23017 + IR) — etapa posterior
stack/mosquitto/   Config del broker
docs/              Especificación v1.0 + diseño
```

El motor no sabe que existen HTTP ni MQTT: `api/app.py` y `puente_mqtt.py` son dos cáscaras
sobre el mismo `servicio.ingerir()`. Agregar un transporte no toca la lógica de auditoría.

## Levantar el stack

```bash
cp .env.example .env      # ajustar POSTGRES_PASSWORD y puertos
docker compose up -d
```

Ningún servicio publica su puerto por defecto — están tunelizados para otras cosas. Todo lo
expuesto al host vive en el bloque 18000 y se configura en `.env`:

| Servicio | Host | Interno |
|---|---|---|
| API / panel | 18500 | 18500 |
| MQTT | 18830 | 1883 |
| MQTT websockets | 19001 | 9001 |
| Node-RED | 18800 | 1880 |
| PostgreSQL | 15432 (solo localhost) | 5432 |

## API por internet (nodo en otra red)

Por defecto el nodo reporta por HTTP en la LAN. Para que reporte estando en otra
red, la API se publica por Caddy con TLS y token (lo arma el rol `panol` del
homelab): `https://panol-api.<dominio>`. Capas de defensa:

- Solo se enrutan los paths que usa un nodo (`/api/evento/*`, `/api/eventos`,
  `/api/heartbeat`, `/api/whitelist`); el resto responde 404 en el proxy. Los
  endpoints de consulta (estado, sesiones, alarmas) quedan solo en LAN/Tailscale.
- Rate limit por IP y cuerpo acotado en Caddy.
- La **API valida el token** (`PANOL_API_TOKEN`) en tiempo constante: si alguien
  enruta al 18500 sin pasar por el proxy, igual lo rechaza.

En el firmware, `API_TOKEN` va en `secrets.py` y `SERVER_URL` apunta al dominio
HTTPS. En la LAN, sin token, `API_TOKEN=""` y se usa la IP directa.

## Levantar en el homelab (etapa 2)

El homelab ya corre el broker, la base y Node-RED (repo `homelab`, stack `panol`,
documentado en `docs/panol-iot.md` de ese repo). Acá solo se despliega el
cerebro, que se une a esa red en vez de levantar servicios propios:

```bash
# EN el servidor, con este repo clonado ahí
docker compose -f docker-compose.homelab.yml up -d --build
```

No hay `.env` que llenar: las credenciales salen de `/etc/panol/app.env`, que
genera Ansible. Diferencias con el stack local, y por qué:

| | Local (banco) | Homelab |
|---|---|---|
| Broker | anónimo | usuario + clave + ACL por nodo |
| Base y broker | los levanta este compose | ya están corriendo, se comparten |
| API | `127.0.0.1:18500` | `0.0.0.0:18500`, abierto por UFW solo a la LAN |

El broker del homelab **no** es anónimo: un MQTT abierto en la red del colegio
deja publicar un `acceso CONCEDIDO` falso y la auditoría deja de valer. Por eso
el puente lee `MQTT_USER` / `MQTT_PASSWORD` y llama a `username_pw_set()` antes
de conectar. Si no están definidas —el caso local— se conecta como siempre.

Credenciales por defecto (conocidas a propósito, **cambiar antes del colegio**):
`panol-servidor` / `cambiar-servidor-panol` para api y puente,
`nodo-panol-puerta` / `cambiar-nodo-puerta` para el ESP32.

## Topics MQTT

```
panol/<ubicacion_id>/<nodo_id>/evento/<tipo>   nodo     -> servidor
panol/<ubicacion_id>/<nodo_id>/heartbeat       nodo     -> servidor
panol/<ubicacion_id>/alarma/<codigo>           servidor -> Node-RED
panol/<ubicacion_id>/sesion                    servidor -> Node-RED
```

Todo con QoS 1. Como "al menos una vez" admite duplicados por diseño, la idempotencia por
`event_id` es parte del contrato, no una precaución.

## Tests

```bash
PANOL_DSN=postgresql://panol:panol@localhost:15432/panol \
  .venv/bin/python -m unittest discover -s server/tests -t server
```

Corren contra un PostgreSQL real: las garantías que más importan (el índice único parcial de
"una sesión activa por ubicación") las da el motor de base, no el código.

La capa de red del nodo se prueba sin hardware ni MicroPython (dobles de `network`,
`urequests` y los `ticks_*`, simulando el bucle principal a 50 ms):

```bash
python -m unittest discover -s firmware/tests -t .
```

## Hoja de ruta

- **Etapa 1:** stack local + validar la máquina de estados con el simulador, y probar RFID,
  PIR y reed sobre el ESP32 real. ✔
- **Etapa 2:** despliegue en el homelab, expuesto LAN + WLAN (admins); nodo de armarios.
  Infra y credenciales listas; falta el tablero Node-RED y rotar las claves `cambiar-*`.
- **Etapa 3:** integración con EMATP. Cada alarma es un ticket: por eso se agrupan por
  episodio y no se purgan nunca (ver [docs/PERSISTENCIA.md](docs/PERSISTENCIA.md)).
  Implementada — falta configurar `EMATP_URL` y `EMATP_TOKEN` y correr `migration_v7.sql`
  del lado de EMATP.

## Ajustar los tiempos para probar

Los parámetros de la spec §12 son los valores por defecto del `Config` y se
pueden bajar por variable de entorno sin tocar código. Esperar quince minutos
para ver si una alarma se repite no es una prueba, es una siesta.

| Variable | Default | Qué controla |
|---|---|---|
| `PANOL_T_RECORDATORIO_S` | 900 | cada cuánto se repite la alarma de **presencia** sin sesión |
| `PANOL_T_RECORDATORIO_INFRA_S` | 3600 | cada cuánto se repite la alarma de **nodo mudo** (más espaciada: un nodo caído no cambia de un cuarto de hora al otro) |
| `PANOL_T_AUSENCIA_S` | 900 | inactividad que cierra la sesión |
| `PANOL_T_QUIESCENCIA_S` | 5400 | silencio que cierra la jornada, incluso con la puerta abierta |
| `PANOL_T_REANUDACION_S` | 5400 | ventana para que el mismo llavero reanude su turno |
| `PANOL_T_PUERTA_ABIERTA_S` | 300 | puerta abierta demasiado tiempo |
| `PANOL_T_SIN_HEARTBEAT_S` | 300 | silencio de un nodo que dispara alarma |
| `PANOL_T_PRECISION_ACTIVIDAD_S` | 60 | atraso tolerado en la marca de actividad (por debajo no se escribe) |

En el homelab van en `/etc/panol/app.env`; en el stack local, en el `.env`.
**Volvé a los valores de producción antes de montar en el colegio**: con el
recordatorio en dos minutos, una persona trabajando sin fichar genera una
alarma —y un ticket— cada dos minutos.

## Alarmas → tickets de EMATP

El planificador vacía una **bandeja de salida**: la alarma se escribe primero en la base
local y recién después se intenta el ticket. Si EMATP no está, queda pendiente
(`enviada_ematp = false`) y se reintenta cada minuto — una alarma de seguridad no se
pierde porque el otro sistema se cayó. La contracara es que EMATP puede recibirla dos
veces, y por eso la idempotencia la resuelve EMATP con `origen_ref`.

```bash
EMATP_URL=https://<dominio>/api/integraciones/panol
EMATP_TOKEN=<el mismo valor que PANOL_API_TOKEN en EMATP>
```

Sin esas variables la integración no hace nada y el sistema funciona igual, con las
alarmas acumulándose en su tabla.

## Mapa de pines — nodo pañol (ESP32 #1, WROOM)

| Función | GPIO | Nota |
|---|---|---|
| RC522 SCK / MOSI / MISO / RST / SDA | 18 / 23 / 19 / 4 / 5 | SoftSPI, VCC 3.3 V exclusivo |
| Relé solenoide | 26 | Activo-LOW, pulso 800 ms, **pull-up externo + 1N4007** |
| Reed switch | 27 | Pull-up interno, ABIERTA = 1 |
| PIR HC-SR501 | 16 | Retrigger H, throttle 30 s |
| LED acceso / LED puerta | 2 / 33 | |
| Botón grabar tarjeta | 32 | Pull-down |
