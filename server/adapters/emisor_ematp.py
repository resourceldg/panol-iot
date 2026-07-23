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

# Cuántas alarmas se mandan por pedido. Van TODAS en un solo POST: EMATP corre
# en Vercel Hobby, donde cada llamado es una invocación de función y un despertar
# de la base Neon. Veinte alarmas sueltas serían veinte de cada cosa. El tope
# tiene que ser <= al que acepta EMATP (25).
LOTE = int(os.environ.get("EMATP_LOTE", "20"))

# Reintento con espera creciente: 1, 2, 4, 8… minutos, hasta media hora. Contra
# un plan gratuito, insistir cada minuto con algo que está fallando es la forma
# más rápida de gastar la cuota sin resolver nada.
ESPERA_BASE_S = int(os.environ.get("EMATP_ESPERA_BASE_S", "60"))
ESPERA_MAX_S = int(os.environ.get("EMATP_ESPERA_MAX_S", "1800"))

# Después de esto se deja de insistir y la alarma queda visible como no enviada.
# Con la espera creciente, 24 intentos son más de diez horas.
MAX_REINTENTOS = int(os.environ.get("EMATP_MAX_REINTENTOS", "24"))


def habilitado() -> bool:
    return bool(URL and TOKEN)


def log(*args):
    print("[EMATP]", *args, flush=True)


def _post(alarmas: list[dict]):
    """Manda un LOTE en un solo pedido. Devuelve (aceptado, ids | detalle)."""
    cuerpo = json.dumps({"alarmas": alarmas}, default=str).encode("utf-8")
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
            # EMATP contesta una línea por alarma: puede aceptar unas y
            # rechazar otras (una mal formada no invalida el lote).
            return True, [r["id"] for r in datos.get("resultados", []) if r.get("ok")]
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
        WHERE NOT enviada_ematp
          AND reintentos < %s
          -- Espera creciente entre intentos: 1, 2, 4… minutos, con techo.
          AND (ultimo_intento IS NULL
               OR ultimo_intento < now() - make_interval(secs =>
                    LEAST(%s * POWER(2, LEAST(reintentos, 10)), %s)))
        ORDER BY timestamp
        LIMIT %s
        """,
        (MAX_REINTENTOS, ESPERA_BASE_S, ESPERA_MAX_S, limite),
    ).fetchall()


def despachar(conn, limite: int = LOTE) -> dict:
    """Intenta enviar lo pendiente, en un solo pedido. Resumen para el log."""
    if not habilitado():
        return {}

    lote = pendientes(conn, limite)
    if not lote:
        return {}

    ok, detalle = _post([dict(a) for a in lote])
    ids = [a["id"] for a in lote]

    if not ok:
        # Falló el pedido entero: nadie se marca como enviada y todas suman un
        # intento, con lo que la próxima espera es más larga.
        conn.execute(
            "UPDATE alarmas SET reintentos = reintentos + 1, ultimo_intento = now()"
            " WHERE id = ANY(%s)",
            (ids,),
        )
        if lote[0]["reintentos"] % 5 == 0:
            log(len(ids), "alarmas sin enviar:", detalle)
        return {"pendientes": len(ids)}

    aceptadas = [i for i in ids if i in set(detalle)]
    rechazadas = [i for i in ids if i not in set(detalle)]

    if aceptadas:
        conn.execute(
            "UPDATE alarmas SET enviada_ematp = TRUE, ultimo_intento = now()"
            " WHERE id = ANY(%s)",
            (aceptadas,),
        )
        log(len(aceptadas), "alarmas -> tickets")
    if rechazadas:
        # Quedaron pendientes: una mal formada, o el lote cortado a la mitad
        # por un error de EMATP. Se reintentan con espera creciente.
        conn.execute(
            "UPDATE alarmas SET reintentos = reintentos + 1, ultimo_intento = now()"
            " WHERE id = ANY(%s)",
            (rechazadas,),
        )
        log(len(rechazadas), "alarmas no aceptadas:", rechazadas)

    resumen = {}
    if aceptadas:
        resumen["enviadas"] = len(aceptadas)
    if rechazadas:
        resumen["pendientes"] = len(rechazadas)
    return resumen
