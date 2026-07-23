"""Envío de alarmas a EMATP, con patrón de bandeja de salida.

Cada alarma es un ticket allá. La regla que ordena todo el diseño es que el
pañol **no puede perder una alarma porque EMATP no estaba**: la red del colegio
se cae, Vercel tiene un mal minuto, alguien renueva un certificado. Por eso la
alarma se escribe primero en la base local (eso ya pasó cuando el motor la
decidió) y recién después se intenta enviar; si el envío falla, queda pendiente
y se reintenta en la próxima vuelta del planificador.

La contracara es que EMATP puede recibir la misma alarma dos veces: un ACK
perdido significa que allá se creó el ticket pero acá no nos enteramos. La
idempotencia la resuelve EMATP con `origen_ref` — acá se prefiere reintentar de
más antes que perder un aviso de seguridad.

Sin `EMATP_URL` configurada el módulo no hace nada: el sistema funciona igual,
con las alarmas acumulándose en su tabla.
"""

import json
import os
import urllib.error
import urllib.request

URL = os.environ.get("EMATP_URL")            # https://<dominio>/api/integraciones/panol
TOKEN = os.environ.get("EMATP_TOKEN")
TIMEOUT_S = float(os.environ.get("EMATP_TIMEOUT_S", "8"))

# Cuántas alarmas se despachan por vuelta. Acotado para que una acumulación de
# días no monopolice la vuelta del planificador ni inunde a EMATP de golpe.
LOTE = int(os.environ.get("EMATP_LOTE", "20"))

# Después de esto se deja de reintentar y la alarma queda visible como no
# enviada. Con una vuelta por minuto son ~2 horas de insistencia: si en dos
# horas EMATP no volvió, el problema no se arregla reintentando.
MAX_REINTENTOS = int(os.environ.get("EMATP_MAX_REINTENTOS", "120"))


def habilitado() -> bool:
    return bool(URL and TOKEN)


def log(*args):
    print("[EMATP]", *args, flush=True)


def _post(alarma: dict) -> tuple[bool, str]:
    """Manda una alarma. Devuelve (aceptada, detalle)."""
    cuerpo = json.dumps(alarma, default=str).encode("utf-8")
    pedido = urllib.request.Request(
        URL,
        data=cuerpo,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(pedido, timeout=TIMEOUT_S) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8") or "{}")
            return True, datos.get("numero_orden", "sin número")
    except urllib.error.HTTPError as e:
        # 4xx: el pedido está mal y reintentarlo igual no lo va a arreglar…
        # salvo 401/403/429, que sí pueden ser transitorios (token que se está
        # rotando, límite de tasa).
        cuerpo_error = e.read().decode("utf-8", "replace")[:200]
        permanente = 400 <= e.code < 500 and e.code not in (401, 403, 408, 429)
        if permanente:
            return False, f"rechazo permanente {e.code}: {cuerpo_error}"
        return False, f"HTTP {e.code}: {cuerpo_error}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"sin conexión: {e}"
    except ValueError as e:
        return False, f"respuesta ilegible: {e}"


def pendientes(conn, limite: int = LOTE) -> list[dict]:
    """Alarmas que todavía no llegaron a EMATP, las más viejas primero.

    Usa el índice parcial `ix_alarmas_pendientes`: la consulta no mira las
    alarmas ya enviadas, que con el tiempo son casi todas.
    """
    return conn.execute(
        """
        SELECT id, ubicacion_id, codigo, severidad, sesion_id, detalle,
               timestamp, reintentos
        FROM alarmas
        WHERE NOT enviada_ematp AND reintentos < %s
        ORDER BY timestamp
        LIMIT %s
        """,
        (MAX_REINTENTOS, limite),
    ).fetchall()


def despachar(conn, limite: int = LOTE) -> dict:
    """Intenta enviar lo pendiente. Devuelve un resumen para el log."""
    if not habilitado():
        return {}

    enviadas, fallidas = 0, 0
    for alarma in pendientes(conn, limite):
        ok, detalle = _post(dict(alarma))
        if ok:
            conn.execute(
                "UPDATE alarmas SET enviada_ematp = TRUE WHERE id = %s",
                (alarma["id"],),
            )
            enviadas += 1
            log("alarma", alarma["id"], alarma["codigo"], "->", detalle)
        else:
            # El contador es también la señal de alerta: una alarma con muchos
            # reintentos es una que EMATP no está aceptando.
            conn.execute(
                "UPDATE alarmas SET reintentos = reintentos + 1 WHERE id = %s",
                (alarma["id"],),
            )
            fallidas += 1
            if alarma["reintentos"] == 0 or alarma["reintentos"] % 10 == 0:
                log("alarma", alarma["id"], "no enviada:", detalle)

    resumen = {}
    if enviadas:
        resumen["enviadas"] = enviadas
    if fallidas:
        resumen["pendientes"] = fallidas
    return resumen
