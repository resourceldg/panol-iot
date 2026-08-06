"""Persistencia sobre PostgreSQL: aplica los efectos del motor.

El motor decide y este módulo escribe. La separación importa porque acá
viven las dos consultas que hacen correcta la auditoría:

* `sesion_vigente_en()` — resuelve quién era responsable en un instante
  dado, no ahora. Es lo que permite atribuir bien un evento que llegó
  tarde desde la cola en flash de un nodo.
* `ya_procesado()` — descarta reenvíos por `event_id`.

Postgres (y no SQLite) porque hay **dos procesos escribiendo**: la API HTTP
y el puente MQTT, cada uno en su contenedor. Con dos escritores reales, las
garantías las tiene que dar el motor de base, no la disciplina del código.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from engine import modelo as m

# Argentina no aplica horario de verano, así que un offset fijo alcanza y
# evita depender de la base de zonas horarias del contenedor.
TZ = timezone(timedelta(hours=-3))

RUTA_ESQUEMA = Path(__file__).with_name("esquema.sql")

DSN_POR_DEFECTO = os.environ.get(
    "PANOL_DSN", "postgresql://panol:panol@localhost:5432/panol"
)


def ahora() -> datetime:
    return datetime.now(TZ)


# --- Conexión ------------------------------------------------------------


def conectar(dsn: str | None = None) -> psycopg.Connection:
    """Conexión con autocommit. Las transacciones se abren explícitamente.

    Autocommit + `with conn.transaction()` donde hace falta es más claro que
    una transacción implícita siempre abierta: se ve en el código dónde
    empieza y termina la atomicidad.
    """
    conn = psycopg.connect(dsn or DSN_POR_DEFECTO, row_factory=dict_row)
    conn.autocommit = True
    # Los timestamptz vuelven en esta zona, así que lo que se lee coincide
    # con lo que reportó el nodo. Tiene que ser un nombre IANA: un literal
    # '-03:00' Postgres no lo reconoce y cae silenciosamente a UTC.
    conn.execute("SET TIME ZONE 'America/Argentina/Buenos_Aires'")
    return conn


# Clave arbitraria y estable del lock de aviso ("panol" en ASCII). Cualquier
# proceso que vaya a crear el esquema pide ESTA misma.
_LOCK_ESQUEMA = 0x70616E6F6C


def inicializar(conn: psycopg.Connection) -> None:
    """Crea el esquema, serializado con un lock de aviso.

    `CREATE TABLE IF NOT EXISTS` NO es seguro entre procesos concurrentes: la
    api y el puente arrancan juntos, los dos ven que la tabla no existe y los
    dos la crean; el que pierde revienta con UniqueViolation sobre `pg_type`.
    Con el lock, el segundo espera al primero y encuentra todo hecho.

    El lock es de transacción, así que se suelta solo al terminar — incluso si
    el esquema falla a la mitad.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ESQUEMA,))
        conn.execute(RUTA_ESQUEMA.read_text(encoding="utf-8"))


# --- Topología -----------------------------------------------------------


def asegurar_ubicacion(conn: psycopg.Connection, ubicacion_id: str) -> None:
    """Da de alta la ubicación si no existía.

    El alta automática mantiene el servidor usable mientras se instalan
    nodos, pero tiene un costo: un ESP32 flasheado con un ID mal escrito
    crea un pañol fantasma en vez de fallar de forma visible. Conviene
    mirar `/api/estado` después de cada instalación.
    """
    conn.execute(
        "INSERT INTO ubicaciones (id, nombre) VALUES (%s, %s)"
        " ON CONFLICT (id) DO NOTHING",
        (ubicacion_id, ubicacion_id),
    )


def registrar_heartbeat(
    conn: psycopg.Connection,
    nodo_id: str,
    ubicacion_id: str,
    rol: str,
    uptime_s: int | None = None,
    rssi: int | None = None,
    modo_degradado: bool = False,
) -> bool:
    """Registra el heartbeat. Devuelve True si el nodo ENTRÓ en modo degradado.

    La transición (no el estado sostenido) es lo que dispara la alarma
    MODO_DEGRADADO una sola vez por episodio: un nodo degradado manda un
    heartbeat por minuto y no debe generar una alarma por minuto.
    """
    asegurar_ubicacion(conn, ubicacion_id)
    fila = conn.execute(
        "SELECT modo_degradado FROM nodos WHERE id = %s", (nodo_id,)
    ).fetchone()
    antes = bool(fila["modo_degradado"]) if fila else False
    conn.execute(
        """
        INSERT INTO nodos (id, ubicacion_id, rol, ultimo_heartbeat,
                           modo_degradado, uptime_s, rssi)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            ultimo_heartbeat = EXCLUDED.ultimo_heartbeat,
            modo_degradado   = EXCLUDED.modo_degradado,
            uptime_s         = EXCLUDED.uptime_s,
            rssi             = EXCLUDED.rssi
        """,
        (nodo_id, ubicacion_id, rol, ahora(), modo_degradado, uptime_s, rssi),
    )
    return modo_degradado and not antes


def estado_puerta_actual(
    conn: psycopg.Connection, ubicacion_id: str
) -> tuple[str | None, datetime | None]:
    """Último estado del reed y desde cuándo. El reed solo reporta cambios,
    así que el último evento ABIERTO marca cuándo empezó a estar abierta."""
    fila = conn.execute(
        "SELECT estado_reed, timestamp FROM eventos_puerta"
        " WHERE ubicacion_id = %s ORDER BY timestamp DESC, id DESC LIMIT 1",
        (ubicacion_id,),
    ).fetchone()
    if not fila:
        return None, None
    return fila["estado_reed"], fila["timestamp"]


def alarma_existe_desde(
    conn: psycopg.Connection,
    ubicacion_id: str,
    codigo: str,
    desde: datetime,
    nodo_id: str | None = None,
) -> bool:
    """¿Ya hay una alarma de este código desde `desde`? Implementa el one-shot
    de estados sostenidos sin guardar un flag: se lee de la propia tabla.

    `nodo_id` acota la pregunta a un nodo (mirando el detalle). Sin eso, dos
    nodos mudos de la misma ubicación compartirían el one-shot y el segundo
    quedaría tapado por el primero.
    """
    fila = conn.execute(
        "SELECT 1 FROM alarmas WHERE ubicacion_id = %s AND codigo = %s"
        " AND timestamp >= %s"
        " AND (%s::text IS NULL OR detalle->>'nodo_id' = %s) LIMIT 1",
        (ubicacion_id, codigo, desde, nodo_id, nodo_id),
    ).fetchone()
    return fila is not None


def nodos_sin_heartbeat(conn: psycopg.Connection, umbral_s: int = 300) -> list[dict]:
    """Nodos mudos: un nodo caído es ceguera silenciosa (spec §6)."""
    return conn.execute(
        "SELECT * FROM nodos WHERE ultimo_heartbeat IS NULL"
        "    OR ultimo_heartbeat < now() - make_interval(secs => %s)",
        (umbral_s,),
    ).fetchall()


# --- Idempotencia --------------------------------------------------------


def ya_procesado(conn: psycopg.Connection, event_id: str | None) -> bool:
    if not event_id:
        return False
    fila = conn.execute(
        "SELECT 1 FROM eventos_procesados WHERE event_id = %s", (event_id,)
    ).fetchone()
    return fila is not None


def marcar_procesado(
    conn: psycopg.Connection,
    event_id: str | None,
    nodo_id: str | None,
    tipo: str,
) -> None:
    if not event_id:
        return
    # SIN `ON CONFLICT DO NOTHING`, y es a propósito. Con él, dos procesos que
    # ingieren el MISMO evento a la vez (la api por HTTP y el puente por MQTT,
    # que es el despliegue real) pasaban los dos el chequeo de `ya_procesado`,
    # los dos aplicaban sus efectos y la marca duplicada se descartaba en
    # silencio: el evento quedaba registrado dos veces. Dejando que la clave
    # primaria falle, la transacción entera se deshace y `servicio.ingerir` lo
    # trata como lo que es, un duplicado. La idempotencia la garantiza la base,
    # no el orden en que corran los procesos.
    conn.execute(
        "INSERT INTO eventos_procesados (event_id, nodo_id, tipo)"
        " VALUES (%s, %s, %s)",
        (event_id, nodo_id, tipo),
    )


# --- Sesiones ------------------------------------------------------------


def _a_sesion(fila: dict) -> m.Sesion:
    return m.Sesion(
        id=fila["id"],
        ubicacion_id=fila["ubicacion_id"],
        uid_hex=fila["uid_hex"],
        inicio=fila["hora_inicio"],
        ultima_actividad=fila["ultima_actividad"],
        estado=fila["estado"],
        alarmada_puerta_abierta=fila["alarmada_puerta_abierta"],
    )


def sesion_en_curso(conn: psycopg.Connection, ubicacion_id: str) -> m.Sesion | None:
    fila = conn.execute(
        "SELECT * FROM sesiones WHERE ubicacion_id = %s AND estado = 'EN_CURSO'",
        (ubicacion_id,),
    ).fetchone()
    return _a_sesion(fila) if fila else None


def sesion_vigente_en(
    conn: psycopg.Connection, ubicacion_id: str, ts: datetime
) -> m.Sesion | None:
    """Sesión que estaba vigente en `ts`, aunque ya haya terminado.

    Con la cola offline un evento puede llegar minutos u horas después. Si
    se atribuyera a la sesión *actual*, la auditoría diría que abrió el
    armario quien entró después. Esta consulta responde la pregunta
    correcta: quién era responsable cuando el evento ocurrió de verdad.
    """
    fila = conn.execute(
        """
        SELECT * FROM sesiones
        WHERE ubicacion_id = %s
          AND hora_inicio <= %s
          AND (hora_fin IS NULL OR hora_fin > %s)
        ORDER BY hora_inicio DESC
        LIMIT 1
        """,
        (ubicacion_id, ts, ts),
    ).fetchone()
    return _a_sesion(fila) if fila else None


def sesion_reanudable(
    conn: psycopg.Connection, ubicacion_id: str, uid_hex: str, ts: datetime,
    ventana_s: int,
) -> int | None:
    """Turno del MISMO llavero, cerrado por ausencia hace poco.

    Solo AUSENCIA y CIERRE_SISTEMA: un RELEVO significa que otro se hizo cargo
    en el medio, y ahí el turno anterior terminó de verdad. Tampoco se reanuda
    una sesión INCONSISTENTE — esa quedó marcada para revisión y reabrirla
    borraría la marca.
    """
    fila = conn.execute(
        """
        SELECT id FROM sesiones
        WHERE ubicacion_id = %s
          AND uid_hex = %s
          AND estado = 'COMPLETA'
          AND motivo_cierre IN ('AUSENCIA', 'CIERRE_SISTEMA')
          AND hora_fin IS NOT NULL
          AND hora_fin > %s - make_interval(secs => %s)
          AND hora_fin <= %s
        ORDER BY hora_fin DESC
        LIMIT 1
        """,
        (ubicacion_id, uid_hex, ts, ventana_s, ts),
    ).fetchone()
    return fila["id"] if fila else None


# --- Aplicación de efectos ----------------------------------------------


def aplicar(
    conn: psycopg.Connection, efectos: list, evento: m.Evento | None = None
) -> None:
    """Escribe todo lo que el motor decidió, en una sola transacción.

    O se aplica el efecto completo de un evento o no se aplica nada: un
    relevo a medias dejaría dos sesiones abiertas o ninguna.

    El `event_id` se marca como procesado DENTRO de la misma transacción.
    Si se marcara aparte, un corte entre ambas escrituras dejaría el evento
    marcado pero sin aplicar (se pierde) o aplicado sin marcar (se duplica
    en el próximo reenvío).
    """
    with conn.transaction():
        for efecto in efectos:
            _aplicar_uno(conn, efecto)
        if evento is not None:
            marcar_procesado(conn, evento.event_id, evento.nodo_id, evento.tipo)


def _aplicar_uno(conn: psycopg.Connection, e) -> None:
    if isinstance(e, m.CrearSesion):
        asegurar_ubicacion(conn, e.ubicacion_id)
        conn.execute(
            """
            INSERT INTO sesiones (ubicacion_id, uid_hex, usuario_id, hora_inicio,
                                  ultima_actividad, estado)
            VALUES (%s, %s,
                    (SELECT usuario_id FROM credenciales
                      WHERE uid_hex = %s AND activa LIMIT 1),
                    %s, %s, 'EN_CURSO')
            """,
            (e.ubicacion_id, e.uid_hex, e.uid_hex, e.ts, e.ts),
        )

    elif isinstance(e, m.ReanudarSesion):
        # Vuelve a abrir el MISMO turno: se limpia el cierre y se cuenta la
        # interrupción. La hora de inicio no se toca — el turno empezó cuando
        # empezó, y eso es lo que la auditoría tiene que poder responder.
        conn.execute(
            "UPDATE sesiones SET estado = 'EN_CURSO', hora_fin = NULL,"
            " motivo_cierre = NULL, ultima_actividad = %s,"
            " reanudaciones = reanudaciones + 1,"
            " alarmada_puerta_abierta = FALSE WHERE id = %s",
            (e.ts, e.sesion_id),
        )

    elif isinstance(e, m.FinalizarSesion):
        # El cierre hay que empujarlo a EMATP (el tablero necesita ver la sesión
        # terminada, con su motivo). Se reinicia el backoff: es un cambio NUEVO,
        # no un reintento del envío anterior —que fue exitoso—, así que debe
        # salir ya, sin esperar la ventana de espera creciente.
        conn.execute(
            "UPDATE sesiones SET hora_fin = %s, motivo_cierre = %s,"
            " estado = 'COMPLETA', push_pendiente = TRUE,"
            " push_reintentos = 0, push_ultimo_intento = NULL WHERE id = %s",
            (e.ts, e.motivo, e.sesion_id),
        )

    elif isinstance(e, m.MarcarActividad):
        # GREATEST: un evento que llega tarde no debe RETRASAR la marca de
        # actividad y provocar un cierre por ausencia que no corresponde.
        conn.execute(
            "UPDATE sesiones SET ultima_actividad = GREATEST(ultima_actividad, %s)"
            " WHERE id = %s",
            (e.ts, e.sesion_id),
        )

    elif isinstance(e, m.MarcarAlarmadaPuertaAbierta):
        conn.execute(
            "UPDATE sesiones SET alarmada_puerta_abierta = %s WHERE id = %s",
            (e.valor, e.sesion_id),
        )

    elif isinstance(e, m.MarcarInconsistente):
        conn.execute(
            "UPDATE sesiones SET estado = 'INCONSISTENTE', push_pendiente = TRUE,"
            " push_reintentos = 0, push_ultimo_intento = NULL WHERE id = %s",
            (e.sesion_id,),
        )

    elif isinstance(e, m.RegistrarAcceso):
        asegurar_ubicacion(conn, e.ubicacion_id)
        conn.execute(
            "INSERT INTO eventos_acceso (ubicacion_id, uid_hex, resultado,"
            " timestamp, modo_degradado) VALUES (%s, %s, %s, %s, %s)",
            (e.ubicacion_id, e.uid_hex, e.resultado, e.ts, e.modo_degradado),
        )

    elif isinstance(e, m.RegistrarPuerta):
        asegurar_ubicacion(conn, e.ubicacion_id)
        conn.execute(
            "INSERT INTO eventos_puerta (ubicacion_id, sesion_id, estado_reed,"
            " timestamp) VALUES (%s, %s, %s, %s)",
            (e.ubicacion_id, e.sesion_id, e.estado_reed, e.ts),
        )

    elif isinstance(e, m.RegistrarPir):
        asegurar_ubicacion(conn, e.ubicacion_id)
        conn.execute(
            "INSERT INTO eventos_pir (ubicacion_id, sesion_id, timestamp)"
            " VALUES (%s, %s, %s)",
            (e.ubicacion_id, e.sesion_id, e.ts),
        )

    elif isinstance(e, m.RegistrarArmario):
        asegurar_ubicacion(conn, e.ubicacion_id)
        conn.execute(
            "INSERT INTO eventos_armario (ubicacion_id, sesion_id, armario_id,"
            " timestamp) VALUES (%s, %s, %s, %s)",
            (e.ubicacion_id, e.sesion_id, e.armario_id, e.ts),
        )

    elif isinstance(e, m.Alarma):
        asegurar_ubicacion(conn, e.ubicacion_id)
        conn.execute(
            "INSERT INTO alarmas (ubicacion_id, codigo, severidad, sesion_id,"
            " detalle, timestamp) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                e.ubicacion_id,
                e.codigo,
                e.severidad,
                e.sesion_id,
                psycopg.types.json.Jsonb(e.detalle) if e.detalle else None,
                e.ts,
            ),
        )

    else:
        raise TypeError(f"efecto desconocido: {type(e).__name__}")
