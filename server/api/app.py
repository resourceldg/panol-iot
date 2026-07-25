"""Shell HTTP del servidor de pañoles (spec v1.0 §11).

Es una cáscara delgada a propósito: parsea, delega en `servicio.ingerir()` y
responde. Toda la lógica de auditoría vive en `engine/`, que no sabe que
existe HTTP ni MQTT. `puente_mqtt.py` es otra cáscara sobre el mismo motor.

Probar sin hardware:
    curl -X POST localhost:18500/api/evento/acceso \
      -H 'Content-Type: application/json' \
      -d '{"ubicacion_id":"panol-lab01","uid_hex":"C1:D1:3D:05",
           "resultado":"CONCEDIDO","event_id":"demo-1"}'
"""

import hmac
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask, g, jsonify, request

import servicio
from db import repositorio
from engine import modelo as m

DSN = os.environ.get("PANOL_DSN")
PUERTO = int(os.environ.get("PANOL_API_PORT", "18500"))

# Token para autenticar a los nodos. Vacío = sin auth (modo banco, en LAN
# aislada). En cuanto la API se publica a internet DEBE estar definido: es la
# defensa que no depende del proxy — si mañana alguien enruta al 18500 sin
# pasar por Caddy, la API igual exige el token.
API_TOKEN = os.environ.get("PANOL_API_TOKEN", "").strip()

# Endpoints que NO requieren token: solo el healthcheck del contenedor.
_SIN_AUTH = {"/salud"}

app = Flask(__name__)


@app.before_request
def _exigir_token():
    """Valida el bearer token antes de tocar la lógica.

    Comparación en tiempo constante: con un `==` normal, el tiempo de respuesta
    filtra cuántos caracteres del token acertó quien prueba. Si no hay token
    configurado, no se exige nada (modo banco).
    """
    if not API_TOKEN or request.path in _SIN_AUTH:
        return None
    cabecera = request.headers.get("Authorization", "")
    recibido = cabecera[7:] if cabecera.startswith("Bearer ") else ""
    if not hmac.compare_digest(recibido, API_TOKEN):
        return jsonify({"ok": False, "error": "no autorizado"}), 401
    return None


# --- Conexión por request ------------------------------------------------


def conn():
    if "conn" not in g:
        g.conn = repositorio.conectar(DSN)
    return g.conn


@app.teardown_appcontext
def _cerrar(_exc):
    c = g.pop("conn", None)
    if c is not None:
        c.close()


# --- Helpers -------------------------------------------------------------


def _cuerpo() -> dict:
    return request.get_json(silent=True) or {}


def _json(fila: dict) -> dict:
    """Serializa timestamptz como ISO-8601.

    Sin esto Flask emite fechas en formato RFC 822 ("Tue, 21 Jul 2026..."),
    que es incómodo de parsear en Node-RED y en el panel.
    """
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fila.items()
    }


def _timestamp(datos: dict) -> datetime:
    """Timestamp del evento: el del nodo si lo mandó, si no el de recepción.

    El nodo puede bootear sin NTP y no tener hora real. En ese caso reporta
    sin `timestamp` y el servidor pone la de llegada — es lo mejor que se
    puede hacer, y queda explícito en vez de inventar una hora falsa.
    """
    crudo = datos.get("timestamp")
    if not crudo:
        return repositorio.ahora()
    try:
        ts = datetime.fromisoformat(crudo)
    except (ValueError, TypeError):
        return repositorio.ahora()
    return ts if ts.tzinfo else ts.replace(tzinfo=repositorio.TZ)


_CAMPOS = {
    "acceso": ("uid_hex", "resultado"),
    "puerta": ("estado_reed",),
    "pir": (),
    "armario": ("armario_id",),
}


def construir_evento(tipo: str, datos: dict) -> m.Evento:
    """Valida y normaliza. Compartido con el puente MQTT."""
    if tipo not in _CAMPOS:
        raise ValueError(f"tipo desconocido: {tipo}")
    ubicacion_id = datos.get("ubicacion_id")
    if not ubicacion_id:
        raise ValueError("falta ubicacion_id")
    campos = _CAMPOS[tipo]
    faltantes = [c for c in campos if c not in datos]
    if faltantes:
        raise ValueError(f"faltan campos: {', '.join(faltantes)}")
    payload = {c: datos[c] for c in campos}
    if "modo_degradado" in datos:
        payload["modo_degradado"] = datos["modo_degradado"]
    return m.Evento(
        tipo=tipo,
        ubicacion_id=ubicacion_id,
        ts=_timestamp(datos),
        event_id=datos.get("event_id"),
        nodo_id=datos.get("nodo_id"),
        datos=payload,
    )


def resumen_efectos(efectos: list) -> str:
    """Una línea de log con marca clara de si el evento cayó en una sesión.

    Es la distinción que importa de un vistazo: un evento CON sesión es
    actividad normal; SIN sesión es anomalía (alarma). Se decide por los
    efectos, no hay que cruzar tablas para leerlo.
    """
    if not efectos:
        return "· duplicado (ya procesado)"
    nombres = [type(e).__name__ for e in efectos]
    alarmas = [e.codigo for e in efectos if type(e).__name__ == "Alarma"]
    acceso = next((e for e in efectos if type(e).__name__ == "RegistrarAcceso"), None)

    # Anomalia primero: es lo que hay que ver de un vistazo al debuggear.
    if alarmas:
        return "SIN SESION  ! " + " ".join(alarmas)
    if "CrearSesion" in nombres and "FinalizarSesion" in nombres:
        return "CON SESION  ~ RELEVO (cierra + nace)"
    if "CrearSesion" in nombres:
        return "CON SESION  + nace sesion (acceso CONCEDIDO)"
    if "FinalizarSesion" in nombres:
        motivo = next(e.motivo for e in efectos
                      if type(e).__name__ == "FinalizarSesion")
        return "CON SESION  x cierra sesion ({})".format(motivo)
    if acceso is not None:
        # Acceso que NO creo sesion: credencial rechazada o sin ingreso.
        marca = "! " if acceso.resultado == "DENEGADO" else "  "
        return "-  {}acceso {}".format(marca, acceso.resultado)
    if {"MarcarActividad", "RegistrarPuerta", "RegistrarArmario"} & set(nombres):
        return "CON SESION    " + " ".join(nombres)
    return "-  " + " ".join(nombres)


def aplanar_sobre(sobre: dict) -> tuple[str, dict]:
    """Convierte el sobre que escribe el firmware en (tipo, datos planos)."""
    datos = dict(sobre.get("datos") or {})
    datos.update(
        {
            "ubicacion_id": sobre.get("ubicacion_id"),
            "nodo_id": sobre.get("nodo_id"),
            "event_id": sobre.get("event_id"),
            "timestamp": sobre.get("timestamp"),
        }
    )
    return sobre.get("tipo"), datos


# --- Eventos de los nodos (spec §11) -------------------------------------


@app.post("/api/evento/<tipo>")
def evento(tipo: str):
    try:
        ev = construir_evento(tipo, _cuerpo())
    except ValueError as e:
        codigo = 404 if "tipo desconocido" in str(e) else 400
        # Un evento rechazado tiene que verse en el log: casi siempre es un
        # nodo mal configurado o un contrato desalineado, no un ataque.
        app.logger.warning("RECHAZADO %s (%d): %s", tipo, codigo, e)
        return jsonify({"ok": False, "error": str(e)}), codigo

    efectos = servicio.ingerir(conn(), ev)
    app.logger.info("[%s] %-8s %s", ev.ubicacion_id, tipo, resumen_efectos(efectos))
    return jsonify(
        {
            "ok": True,
            "event_id": ev.event_id,
            "duplicado": ev.event_id is not None and not efectos,
            "efectos": [type(e).__name__ for e in efectos],
        }
    )


@app.post("/api/eventos")
def eventos_en_lote():
    """Vaciado de la cola en flash de un nodo que estuvo sin red.

    Recibe los sobres tal cual los escribe el firmware y responde qué
    `event_id` quedaron confirmados. El nodo trunca solo esos: lo que no
    aparezca acá se reintenta, así un corte a mitad de la sincronización
    no pierde eventos.
    """
    sobres = _cuerpo().get("eventos", [])
    confirmados, rechazados = [], []

    for sobre in sobres:
        tipo, datos = aplanar_sobre(sobre)
        try:
            servicio.ingerir(conn(), construir_evento(tipo, datos))
            confirmados.append(sobre.get("event_id"))
        except Exception as e:
            # Un evento malo no debe frenar la sincronización de los demás.
            app.logger.warning("evento rechazado: %s", e)
            rechazados.append({"event_id": sobre.get("event_id"), "error": str(e)})

    return jsonify({"ok": True, "confirmados": confirmados, "rechazados": rechazados})


@app.post("/api/heartbeat")
def heartbeat():
    d = _cuerpo()
    if not d.get("nodo_id") or not d.get("ubicacion_id"):
        return jsonify({"ok": False, "error": "falta nodo_id o ubicacion_id"}), 400
    entro_degradado = repositorio.registrar_heartbeat(
        conn(),
        nodo_id=d["nodo_id"],
        ubicacion_id=d["ubicacion_id"],
        rol=d.get("rol", "puerta"),
        uptime_s=d.get("uptime"),
        rssi=d.get("rssi"),
        modo_degradado=bool(d.get("modo_degradado", False)),
    )
    # El heartbeat normal no se logea (llega cada 60 s, seria ruido). Solo la
    # TRANSICION a modo degradado, que genera la alarma de spec §9 una vez
    # por episodio (el nodo opera contra la cache NVS por no llegar al server).
    if entro_degradado:
        repositorio.aplicar(conn(), [
            m.Alarma(d["ubicacion_id"], "MODO_DEGRADADO", repositorio.ahora(),
                     detalle={"nodo_id": d["nodo_id"], "rssi": d.get("rssi")})
        ])
        app.logger.warning("[%s] MODO DEGRADADO nodo=%s rssi=%s",
                           d["ubicacion_id"], d["nodo_id"], d.get("rssi"))
    return jsonify({"ok": True})


@app.get("/api/whitelist")
def whitelist():
    """Lista blanca para refrescar la caché NVS del nodo (cada 15 min).

    El nodo decide la autorización contra esta copia local, así que el
    servidor nunca está en el camino crítico de abrir la puerta: un corte
    de red no demora un acceso legítimo.
    """
    filas = conn().execute(
        "SELECT uid_hex FROM credenciales WHERE activa ORDER BY uid_hex"
    ).fetchall()
    return jsonify({"uids": [f["uid_hex"] for f in filas]})


# --- Consulta (WLAN de admins) -------------------------------------------


@app.get("/api/estado")
def estado():
    """Panorama de todas las ubicaciones: quién es responsable de cada pañol."""
    filas = conn().execute(
        """
        SELECT u.id, u.nombre, u.cantidad_maquinas,
               s.id AS sesion_id, s.uid_hex, s.hora_inicio, s.ultima_actividad
        FROM ubicaciones u
        LEFT JOIN sesiones s
          ON s.ubicacion_id = u.id AND s.estado = 'EN_CURSO'
        ORDER BY u.id
        """
    ).fetchall()
    return jsonify({"ubicaciones": [_json(f) for f in filas]})


@app.get("/api/nodos")
def nodos():
    """Salud de la periferia. Un nodo mudo es ceguera silenciosa (spec §6)."""
    filas = conn().execute(
        "SELECT *, ultimo_heartbeat < now() - interval '5 minutes' AS mudo"
        " FROM nodos ORDER BY ubicacion_id, rol"
    ).fetchall()
    return jsonify({"nodos": [_json(f) for f in filas]})


@app.get("/api/sesiones")
def sesiones():
    ubicacion = request.args.get("ubicacion_id")
    if ubicacion:
        filas = conn().execute(
            "SELECT * FROM sesiones WHERE ubicacion_id = %s"
            " ORDER BY id DESC LIMIT 100",
            (ubicacion,),
        ).fetchall()
    else:
        filas = conn().execute(
            "SELECT * FROM sesiones ORDER BY id DESC LIMIT 100"
        ).fetchall()
    return jsonify({"sesiones": [_json(f) for f in filas]})


@app.get("/api/alarmas")
def alarmas():
    filas = conn().execute(
        "SELECT * FROM alarmas ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify({"alarmas": [_json(f) for f in filas]})


@app.get("/salud")
def salud():
    """Healthcheck del contenedor: comprueba que la base responde."""
    try:
        conn().execute("SELECT 1")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


# --- Arranque ------------------------------------------------------------


def preparar():
    """Crea el esquema y recupera lo que haya quedado abierto tras un corte."""
    c = repositorio.conectar(DSN)
    try:
        repositorio.inicializar(c)
        efectos = servicio.recuperar_al_arrancar(c)
        if efectos:
            print(f"[BOOT] recuperación: {len(efectos)} efectos aplicados")
        else:
            print("[BOOT] sin sesiones que recuperar")
    finally:
        c.close()


if __name__ == "__main__":
    preparar()
    # host 0.0.0.0: los nodos llegan desde la subred IoT, no desde localhost.
    app.run(host="0.0.0.0", port=PUERTO, debug=False, use_reloader=False)
