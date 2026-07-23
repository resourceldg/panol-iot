"""Capa de aplicación: une el motor puro con la persistencia.

Es el único lugar donde se decide *qué sesión* se le pasa al motor, y esa
decisión no es trivial, así que está concentrada acá en vez de repartida
entre los endpoints.
"""

from datetime import timedelta

import psycopg

from db import repositorio
from engine import modelo as m
from engine.motor import decidir

# Eventos cuya atribución depende del momento en que OCURRIERON, no del
# momento en que llegaron. Son los que puede retrasar la cola en flash de
# un nodo sin red.
_ATRIBUIDOS_POR_TIMESTAMP = {"puerta", "pir", "armario"}


def ingerir(
    conn, evento: m.Evento, cfg: m.Config | None = None
) -> list:
    """Procesa un evento y devuelve los efectos aplicados.

    Devuelve lista vacía si el evento ya había sido procesado: el nodo
    reenvía desde su cola cuando vuelve la red, y un ACK perdido hace que
    mande de nuevo algo que el servidor ya había registrado.
    """
    cfg = cfg or m.Config()

    if repositorio.ya_procesado(conn, evento.event_id):
        return []

    sesion = _sesion_para(conn, evento)
    _enriquecer(conn, evento, sesion, cfg)
    efectos = _sin_escrituras_inutiles(sesion, decidir(evento, sesion, cfg), cfg)

    try:
        repositorio.aplicar(conn, efectos, evento)
    except psycopg.errors.UniqueViolation:
        # Carrera entre los DOS procesos que ingieren (la api por HTTP y el
        # puente por MQTT): los dos ven "no procesado" y los dos aplican. El
        # `event_id` es PRIMARY KEY y la marca va en la MISMA transacción que
        # los efectos, así que el segundo aborta entero: no quedan efectos a
        # medias. Que la base gane la carrera es lo correcto; lo que estaba mal
        # era escupir una excepción cuando el resultado es, justamente, el que
        # el contrato de idempotencia promete.
        return []

    return efectos


def _enriquecer(conn, evento: m.Evento, sesion: m.Sesion | None, cfg: m.Config) -> None:
    """Agrega al evento los datos de base que el motor necesita y no puede ver.

    El motor es puro: no consulta nada. Cuando una decisión depende del estado
    del mundo (¿ya alarmé por esto?), el dato entra por `datos`.
    """
    if evento.tipo == "acceso" and sesion is None:
        if evento.datos.get("resultado") == "CONCEDIDO":
            evento.datos["sesion_reanudable"] = repositorio.sesion_reanudable(
                conn,
                evento.ubicacion_id,
                evento.datos.get("uid_hex"),
                evento.ts,
                cfg.t_reanudacion_s,
            )

    if evento.tipo == "pir" and sesion is None:
        evento.datos["ya_alarmado"] = repositorio.alarma_existe_desde(
            conn,
            evento.ubicacion_id,
            "PRESENCIA_SIN_SESION",
            evento.ts - timedelta(seconds=cfg.t_recordatorio_alarma_s),
        )


def _sin_escrituras_inutiles(sesion: m.Sesion | None, efectos: list, cfg: m.Config) -> list:
    """Descarta el UPDATE de actividad cuando no cambia ninguna decisión.

    El PIR reporta cada 30 s: con una sesión abierta toda la tarde, eso es un
    UPDATE por muestra sobre la misma fila, cientos por día y por ubicación,
    cada uno con su WAL. Y no sirve de nada: la ausencia se mide en 15 minutos,
    así que una marca con hasta `t_precision_actividad_s` de atraso decide
    exactamente lo mismo. Se conserva el evento —esa es la evidencia—, se
    ahorra el UPDATE.
    """
    if sesion is None:
        return efectos
    conservados = []
    for efecto in efectos:
        if isinstance(efecto, m.MarcarActividad):
            atraso = (efecto.ts - sesion.ultima_actividad).total_seconds()
            if 0 <= atraso < cfg.t_precision_actividad_s:
                continue
        conservados.append(efecto)
    return conservados


def _sesion_para(conn, evento: m.Evento) -> m.Sesion | None:
    """Elige contra qué sesión se evalúa el evento.

    Para puerta/PIR/armario se usa la sesión vigente en el timestamp del
    evento: es lo que hace correcta la auditoría de "quién abrió qué"
    cuando el evento llegó tarde.

    Para `acceso` y las tareas programadas se usa la sesión EN_CURSO
    actual, porque lo que hacen es *modificar* la sesión activa (relevarla,
    cerrarla), no atribuirse a una pasada.

    Limitación conocida: un `acceso` que llega tarde, con una sesión más
    nueva ya abierta, releva a la actual y no a la que correspondía. Es un
    caso raro (requiere que el nodo de puerta pierda la red justo entre dos
    ingresos) y resolverlo bien implica reordenar sesiones hacia atrás. Por
    ahora se documenta en vez de resolverse a medias.
    """
    if evento.tipo in _ATRIBUIDOS_POR_TIMESTAMP:
        return repositorio.sesion_vigente_en(conn, evento.ubicacion_id, evento.ts)
    return repositorio.sesion_en_curso(conn, evento.ubicacion_id)


def verificar_puertas_abiertas(conn, cfg: m.Config | None = None, ts=None) -> list:
    """Corre cada minuto: alarma si una puerta lleva mucho tiempo abierta.

    Independiente de la sesión y de la actividad (a diferencia de la tarea
    de ausencia). El one-shot se resuelve mirando si ya hubo una alarma
    desde que la puerta abrió; se rearma solo cuando se cierra.
    """
    cfg = cfg or m.Config()
    ts = ts or repositorio.ahora()
    efectos_totales = []

    filas = conn.execute("SELECT id FROM ubicaciones").fetchall()
    for fila in filas:
        ubic = fila["id"]
        estado, desde = repositorio.estado_puerta_actual(conn, ubic)
        if estado != "ABIERTO":
            continue
        ya = repositorio.alarma_existe_desde(
            conn, ubic, "PUERTA_ABIERTA_PROLONGADA", desde
        )
        evento = m.Evento(
            tipo="tarea_puerta_abierta",
            ubicacion_id=ubic,
            ts=ts,
            datos={"reed_actual": estado, "abierta_desde": desde, "ya_alarmado": ya},
        )
        sesion = repositorio.sesion_en_curso(conn, ubic)
        efectos = decidir(evento, sesion, cfg)
        if efectos:
            repositorio.aplicar(conn, efectos)
            efectos_totales.extend(efectos)

    return efectos_totales


def tareas_periodicas(conn, cfg: m.Config | None = None, ts=None) -> list:
    """El latido del servidor. Corre cada minuto (ver planificador.py).

    Hasta ahora NADIE llamaba a estas tareas: el motor las tenía escritas y
    probadas, pero en producción no corrían. La consecuencia era silenciosa y
    grave — ninguna sesión se cerraba jamás por ausencia ni por fin de jornada,
    así que el pañol quedaba con un responsable eterno hasta que otro pasara la
    tarjeta, y las alarmas de puerta abierta y de nodo mudo no existían.

    Todo lo que decide sigue estando en el motor; acá solo se juntan los datos
    de la base y se le pregunta.
    """
    # `ts` inyectable: sin eso, el comportamiento del planificador depende de la
    # hora de pared y no se puede probar el fin de jornada sin esperar a las 22.
    cfg = cfg or m.Config()
    ts = ts or repositorio.ahora()
    efectos_totales = []

    efectos_totales += _tarea_por_ubicacion(conn, cfg, ts)
    efectos_totales += verificar_puertas_abiertas(conn, cfg, ts)
    efectos_totales += _tarea_nodos_mudos(conn, cfg, ts)
    return efectos_totales


def _corte_de_jornada(ts, cfg: m.Config):
    """Instante del cierre por reloj de HOY, o None si está deshabilitado.

    Deshabilitado es el default: el colegio dice 6 a 00 pero hay actos y
    jornadas especiales, así que la hora no es un dato confiable. El cierre
    normal lo hace la quiescencia (ver `tarea_ausencia` en el motor).
    """
    if cfg.hora_fin_jornada is None:
        return None
    return ts.replace(hour=cfg.hora_fin_jornada, minute=0, second=0, microsecond=0)


def _tarea_por_ubicacion(conn, cfg: m.Config, ts) -> list:
    """Ausencia y fin de jornada, una vuelta por ubicación con sesión abierta."""
    efectos_totales = []
    corte = _corte_de_jornada(ts, cfg)

    filas = conn.execute(
        "SELECT ubicacion_id FROM sesiones WHERE estado = 'EN_CURSO'"
    ).fetchall()

    for fila in filas:
        ubic = fila["ubicacion_id"]
        sesion = repositorio.sesion_en_curso(conn, ubic)
        if sesion is None:
            continue

        estado_reed, _ = repositorio.estado_puerta_actual(conn, ubic)

        # El fin de jornada manda sobre la ausencia: si ya pasó la hora, la
        # sesión se cierra por CIERRE_SISTEMA aunque la puerta esté abierta.
        # Pero solo para las sesiones que venían de ANTES del corte: una que
        # empezó a las 22:30 es de la jornada siguiente, y tiene que seguir
        # sujeta a la ausencia como cualquier otra en vez de quedar sin
        # vigilancia hasta la medianoche.
        if corte is not None and ts >= corte and sesion.inicio < corte:
            evento = m.Evento(
                tipo="tarea_fin_jornada", ubicacion_id=ubic, ts=ts,
                datos={"corte": corte},
            )
        else:
            evento = m.Evento(
                tipo="tarea_ausencia", ubicacion_id=ubic, ts=ts,
                datos={"reed_actual": estado_reed},
            )

        efectos = decidir(evento, sesion, cfg)
        if efectos:
            repositorio.aplicar(conn, efectos)
            efectos_totales.extend(efectos)

    return efectos_totales


def _tarea_nodos_mudos(conn, cfg: m.Config, ts) -> list:
    """Alarma por cada nodo que dejó de latir, una vez por episodio."""
    efectos_totales = []
    # Ventana de infraestructura, no la de presencia: el nodo mudo se recuerda
    # cada hora, no cada 15 minutos.
    desde = ts - timedelta(seconds=cfg.t_recordatorio_infra_s)

    for nodo in repositorio.nodos_sin_heartbeat(conn, cfg.t_sin_heartbeat_s):
        ubic = nodo["ubicacion_id"]
        ya = repositorio.alarma_existe_desde(
            conn, ubic, "NODO_SIN_HEARTBEAT", desde, nodo_id=nodo["id"]
        )
        ultimo = nodo["ultimo_heartbeat"]
        evento = m.Evento(
            tipo="tarea_nodo_mudo",
            ubicacion_id=ubic,
            ts=ts,
            datos={
                "nodo_id": nodo["id"],
                "ultimo_heartbeat": ultimo.isoformat() if ultimo else None,
                "ya_alarmado": ya,
            },
        )
        efectos = decidir(evento, repositorio.sesion_en_curso(conn, ubic), cfg)
        if efectos:
            repositorio.aplicar(conn, efectos)
            efectos_totales.extend(efectos)

    return efectos_totales


def recuperar_al_arrancar(
    conn, cfg: m.Config | None = None
) -> list:
    """Revisa todas las ubicaciones tras un reinicio del servidor.

    Una sesión con actividad reciente se reanuda sola: no hay que hacer
    nada, sigue EN_CURSO en la base. Una con actividad vencida se marca
    INCONSISTENTE, porque no se puede saber a qué hora se fueron y no
    corresponde inventar una hora de cierre en un registro de auditoría.
    """
    cfg = cfg or m.Config()
    ts = repositorio.ahora()
    efectos_totales = []

    filas = conn.execute(
        "SELECT DISTINCT ubicacion_id FROM sesiones WHERE estado = 'EN_CURSO'"
    ).fetchall()

    for fila in filas:
        ubicacion_id = fila["ubicacion_id"]
        evento = m.Evento(tipo="tarea_recuperacion", ubicacion_id=ubicacion_id, ts=ts)
        sesion = repositorio.sesion_en_curso(conn, ubicacion_id)
        efectos = decidir(evento, sesion, cfg)
        if efectos:
            repositorio.aplicar(conn, efectos)
            efectos_totales.extend(efectos)

    return efectos_totales
