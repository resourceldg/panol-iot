"""Envío de sesiones a EMATP (Fase 2 de la unificación de identidad).

Espeja al `emisor_ematp` de alarmas, con el mismo patrón de bandeja de salida:
la sesión ya está en la base local (la creó el motor); acá se intenta empujar su
estado a EMATP y, si falla, queda pendiente y se reintenta con espera creciente.

Una sesión se empuja al menos DOS veces: al nacer (EN_CURSO) y al cerrarse
(COMPLETA/INCONSISTENTE). El flag `push_pendiente` se prende en cada uno de esos
cambios; EMATP hace UPSERT por `origen_ref`, así que reenviar el cierre no
duplica: actualiza. Por eso acá no hay concepto de "duplicado a rechazar" como
en alarmas: todo push aceptado apaga el flag.

Usa el mismo `EMATP_URL`/`EMATP_TOKEN` que las alarmas; la ruta es el
sub-endpoint `/sesiones`. Sin configuración, no hace nada.
"""

import json
import os
import urllib.error
import urllib.request

_BASE = os.environ.get("EMATP_URL")
URL = (_BASE.rstrip("/") + "/sesiones") if _BASE else None
TOKEN = os.environ.get("EMATP_TOKEN")
TIMEOUT_S = float(os.environ.get("EMATP_TIMEOUT_S", "8"))
LOTE = int(os.environ.get("EMATP_LOTE", "20"))
ESPERA_BASE_S = int(os.environ.get("EMATP_ESPERA_BASE_S", "60"))
ESPERA_MAX_S = int(os.environ.get("EMATP_ESPERA_MAX_S", "1800"))
MAX_REINTENTOS = int(os.environ.get("EMATP_MAX_REINTENTOS", "24"))


def habilitado() -> bool:
    return bool(URL and TOKEN)


def log(*args):
    print("[EMATP-SES]", *args, flush=True)


def _post(sesiones: list[dict]):
    """Manda un LOTE en un solo pedido. Devuelve (aceptado, ids | detalle)."""
    cuerpo = json.dumps({"sesiones": sesiones}, default=str).encode("utf-8")
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
            return True, [r["id"] for r in datos.get("resultados", []) if r.get("ok")]
    except urllib.error.HTTPError as e:
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
    """Sesiones que faltan empujar, las más viejas primero.

    Trae el `ematp_user_id` del espejo de identidad (Fase 1): es la clave con la
    que EMATP enlaza la sesión a la persona. Si la tarjeta no tiene usuario
    espejado, va NULL y la sesión igual se registra (sin responsable resuelto).
    """
    return conn.execute(
        """
        SELECT s.id, s.ubicacion_id, s.uid_hex, s.estado, s.motivo_cierre,
               s.hora_inicio, s.hora_fin, u.ematp_user_id, s.push_reintentos
        FROM sesiones s
        LEFT JOIN usuarios u ON u.id = s.usuario_id
        WHERE s.push_pendiente
          AND s.push_reintentos < %s
          AND (s.push_ultimo_intento IS NULL
               OR s.push_ultimo_intento < now() - make_interval(secs =>
                    LEAST(%s * POWER(2, LEAST(s.push_reintentos, 10)), %s)))
        ORDER BY s.hora_inicio
        LIMIT %s
        """,
        (MAX_REINTENTOS, ESPERA_BASE_S, ESPERA_MAX_S, limite),
    ).fetchall()


def despachar(conn, limite: int = LOTE) -> dict:
    """Intenta empujar lo pendiente en un solo pedido. Resumen para el log."""
    if not habilitado():
        return {}

    lote = pendientes(conn, limite)
    if not lote:
        return {}

    payload = [
        {
            "id": s["id"],
            "ubicacion_id": s["ubicacion_id"],
            "ematp_user_id": s["ematp_user_id"],
            "uid_hex": s["uid_hex"],
            "estado": s["estado"],
            "motivo_cierre": s["motivo_cierre"],
            "hora_inicio": s["hora_inicio"],
            "hora_fin": s["hora_fin"],
        }
        for s in lote
    ]
    ok, detalle = _post(payload)
    ids = [s["id"] for s in lote]

    if not ok:
        conn.execute(
            "UPDATE sesiones SET push_reintentos = push_reintentos + 1,"
            " push_ultimo_intento = now() WHERE id = ANY(%s)",
            (ids,),
        )
        if lote[0]["push_reintentos"] % 5 == 0:
            log(len(ids), "sesiones sin enviar:", detalle)
        return {"pendientes": len(ids)}

    aceptadas = [i for i in ids if i in set(detalle)]
    rechazadas = [i for i in ids if i not in set(detalle)]

    if aceptadas:
        conn.execute(
            "UPDATE sesiones SET push_pendiente = FALSE, push_ultimo_intento = now()"
            " WHERE id = ANY(%s)",
            (aceptadas,),
        )
        log(len(aceptadas), "sesiones -> EMATP")
    if rechazadas:
        conn.execute(
            "UPDATE sesiones SET push_reintentos = push_reintentos + 1,"
            " push_ultimo_intento = now() WHERE id = ANY(%s)",
            (rechazadas,),
        )
        log(len(rechazadas), "sesiones no aceptadas:", rechazadas)

    resumen = {}
    if aceptadas:
        resumen["enviadas"] = len(aceptadas)
    if rechazadas:
        resumen["pendientes"] = len(rechazadas)
    return resumen
