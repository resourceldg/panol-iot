"""Capa de aplicación: une el motor puro con la persistencia.

Es el único lugar donde se decide *qué sesión* se le pasa al motor, y esa
decisión no es trivial, así que está concentrada acá en vez de repartida
entre los endpoints.
"""

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
    efectos = decidir(evento, sesion, cfg)
    repositorio.aplicar(conn, efectos, evento)
    return efectos


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


def verificar_puertas_abiertas(conn, cfg: m.Config | None = None) -> list:
    """Corre cada minuto: alarma si una puerta lleva mucho tiempo abierta.

    Independiente de la sesión y de la actividad (a diferencia de la tarea
    de ausencia). El one-shot se resuelve mirando si ya hubo una alarma
    desde que la puerta abrió; se rearma solo cuando se cierra.
    """
    cfg = cfg or m.Config()
    ts = repositorio.ahora()
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
