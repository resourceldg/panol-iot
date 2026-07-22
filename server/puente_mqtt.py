"""Puente MQTT: segunda cáscara sobre el MISMO motor de sesiones.

`api/app.py` habla HTTP y este módulo habla MQTT, pero los dos terminan en
`servicio.ingerir()`. Esa es la razón de que el motor sea puro: agregar un
transporte no toca una sola línea de la lógica de auditoría.

Esquema de topics
-----------------
    panol/<ubicacion_id>/<nodo_id>/evento/<tipo>   nodo  -> servidor
    panol/<ubicacion_id>/<nodo_id>/heartbeat       nodo  -> servidor
    panol/<ubicacion_id>/alarma/<codigo>           servidor -> Node-RED
    panol/<ubicacion_id>/sesion                    servidor -> Node-RED

La ubicación va en el topic **y** en el payload. Duplicarlo es a propósito:
el topic permite que Node-RED filtre por pañol sin parsear JSON, y el
payload mantiene el evento autocontenido si alguien lo reenvía o lo guarda.

Entrega
-------
Todo con QoS 1 ("al menos una vez") y sesión persistente (`clean_session`
en False con un client_id fijo). El broker guarda lo que llegue mientras el
puente esté caído y lo entrega al reconectar. Como QoS 1 admite duplicados
por diseño, la idempotencia por `event_id` deja de ser una precaución y
pasa a ser parte del contrato.
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paho.mqtt.client as mqtt

import servicio
from api.app import aplanar_sobre, construir_evento
from db import repositorio

BROKER_HOST = os.environ.get("MQTT_HOST", "mosquitto")
BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))
CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "panol-puente")
DSN = os.environ.get("PANOL_DSN")

TOPIC_EVENTOS = "panol/+/+/evento/+"
TOPIC_HEARTBEAT = "panol/+/+/heartbeat"
TOPIC_ESTADO_PUENTE = "panol/servidor/puente"

_conn = None


def log(*args):
    print("[MQTT]", *args, flush=True)


def _conexion():
    """Reconecta si la base se cayó. El puente no debe morir por eso."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = repositorio.conectar(DSN)
    return _conn


# --- Callbacks -----------------------------------------------------------


def al_conectar(cliente, _userdata, _flags, codigo, _props=None):
    if codigo != 0:
        log("conexión rechazada, código", codigo)
        return
    log("conectado a", BROKER_HOST, BROKER_PORT)
    # QoS 1 también en la suscripción: si se pidiera 0, el broker degradaría
    # la entrega y se perderían eventos aunque el nodo publique con 1.
    cliente.subscribe([(TOPIC_EVENTOS, 1), (TOPIC_HEARTBEAT, 1)])
    cliente.publish(TOPIC_ESTADO_PUENTE, "online", qos=1, retain=True)


def al_recibir(cliente, _userdata, msg):
    partes = msg.topic.split("/")
    try:
        sobre = json.loads(msg.payload.decode())
    except (ValueError, UnicodeDecodeError) as e:
        log("payload ilegible en", msg.topic, "->", e)
        return

    try:
        if partes[-1] == "heartbeat":
            _heartbeat(partes, sobre)
        else:
            _evento(partes, sobre, cliente)
    except Exception as e:
        # Una excepción acá mataría el loop de paho y dejaría al sistema
        # ciego. Se registra y se sigue: el evento se pierde, no el puente.
        log("error procesando", msg.topic, "->", repr(e))


def _heartbeat(partes, sobre):
    repositorio.registrar_heartbeat(
        _conexion(),
        nodo_id=sobre.get("nodo_id") or partes[2],
        ubicacion_id=sobre.get("ubicacion_id") or partes[1],
        rol=sobre.get("rol", "puerta"),
        uptime_s=sobre.get("uptime"),
        rssi=sobre.get("rssi"),
        modo_degradado=bool(sobre.get("modo_degradado", False)),
    )


def _evento(partes, sobre, cliente):
    # El topic manda si el payload no trae la identidad: un nodo puede
    # publicar el payload mínimo y el topic completa el resto.
    sobre.setdefault("ubicacion_id", partes[1])
    sobre.setdefault("nodo_id", partes[2])
    sobre.setdefault("tipo", partes[4])

    tipo, datos = aplanar_sobre(sobre)
    evento = construir_evento(tipo, datos)
    efectos = servicio.ingerir(_conexion(), evento)

    if not efectos:
        log(evento.event_id, "duplicado, ignorado")
        return
    log(evento.ubicacion_id, tipo, "->", [type(e).__name__ for e in efectos])
    _republicar(cliente, evento, efectos)


def _republicar(cliente, evento, efectos):
    """Reemite alarmas y cambios de sesión para que Node-RED los consuma.

    Node-RED no consulta la base: se suscribe. Así el panel se entera en el
    momento, sin polling.
    """
    for efecto in efectos:
        nombre = type(efecto).__name__
        if nombre == "Alarma":
            cliente.publish(
                f"panol/{evento.ubicacion_id}/alarma/{efecto.codigo}",
                json.dumps(
                    {
                        "codigo": efecto.codigo,
                        "severidad": efecto.severidad,
                        "ubicacion_id": efecto.ubicacion_id,
                        "timestamp": efecto.ts.isoformat(),
                        "detalle": efecto.detalle,
                    }
                ),
                qos=1,
            )
        elif nombre in ("CrearSesion", "FinalizarSesion"):
            cliente.publish(
                f"panol/{evento.ubicacion_id}/sesion",
                json.dumps(
                    {
                        "cambio": nombre,
                        "ubicacion_id": evento.ubicacion_id,
                        "timestamp": efecto.ts.isoformat(),
                        "uid_hex": getattr(efecto, "uid_hex", None),
                        "motivo": getattr(efecto, "motivo", None),
                    }
                ),
                qos=1,
                retain=(nombre == "CrearSesion"),
            )


# --- Arranque ------------------------------------------------------------


def main():
    cliente = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        clean_session=False,   # El broker guarda lo que llegue si el puente cae
    )
    # Testamento: si el puente muere sin avisar, el broker publica "offline"
    # y Node-RED puede mostrarlo en vez de suponer que todo anda bien.
    cliente.will_set(TOPIC_ESTADO_PUENTE, "offline", qos=1, retain=True)
    cliente.on_connect = al_conectar
    cliente.on_message = al_recibir

    while True:
        try:
            cliente.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            break
        except OSError as e:
            # El broker puede tardar más que este contenedor en levantar.
            log("esperando al broker:", e)
            time.sleep(3)

    cliente.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    repositorio.inicializar(_conexion())
    main()
