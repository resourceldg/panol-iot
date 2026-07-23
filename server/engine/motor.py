"""Máquina de estados de sesión. Pura: sin HTTP, sin SQLite, sin reloj.

La FSM del servidor tiene solo dos estados reales, y eso es deliberado:

    SIN_SESION  <---->  EN_CURSO

`SIN_SESION` no es una fila en ninguna tabla: es "no hay sesión EN_CURSO
para esta ubicación". El `ESPERA_APERTURA` que dibuja la spec v1.0 §4 NO
existe acá — la "promesa" de 10 s es enteramente local al nodo, y el
servidor solo ve el resultado final. Si el nodo se resetea durante la
promesa, el servidor nunca llegó a crear una sesión a medias.

Toda función de este módulo es determinista: mismos argumentos, mismos
efectos. No escribe nada; devuelve una lista de efectos para que el
llamador los aplique. Así la FSM se valida con un simulador.
"""

from datetime import datetime

from .modelo import (
    AUSENCIA,
    CIERRE_SISTEMA,
    RELEVO,
    Alarma,
    Config,
    CrearSesion,
    Evento,
    FinalizarSesion,
    MarcarActividad,
    MarcarAlarmadaPuertaAbierta,
    MarcarInconsistente,
    RegistrarAcceso,
    RegistrarArmario,
    RegistrarPir,
    RegistrarPuerta,
    Sesion,
)


def decidir(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Decide los efectos de un evento sobre la sesión de UNA ubicación.

    `sesion` es la sesión vigente para `evento.ubicacion_id` **en el
    instante `evento.ts`**, no la actual. Resolverla es responsabilidad de
    quien llama, y es lo que hace correcta la auditoría cuando un evento
    llega tarde desde la cola offline de un nodo (spec §3, DISEÑO §3.3).
    """
    manejador = _MANEJADORES.get(evento.tipo)
    if manejador is None:
        raise ValueError(f"tipo de evento desconocido: {evento.tipo!r}")
    return manejador(evento, sesion, cfg)


def _actividad(sesion: Sesion, ts: datetime) -> list:
    """Marca actividad y desarma el one-shot de puerta abierta.

    Cualquier evento físico (PIR, puerta o armario) es evidencia de
    presencia: el PIR no es la única fuente. Si había gente, el episodio de
    "puerta abierta sin gente" terminó y la alarma puede volver a armarse.
    """
    efectos = [MarcarActividad(sesion.id, ts)]
    if sesion.alarmada_puerta_abierta:
        efectos.append(MarcarAlarmadaPuertaAbierta(sesion.id, False))
    return efectos


# --- Eventos de los nodos ------------------------------------------------


def _acceso(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Pasada de llavero. El nodo ya decidió; el servidor registra y correlaciona.

    Solo `CONCEDIDO` (autorizado *y* con ingreso confirmado por el reed)
    crea sesión. `SIN_INGRESO` es alguien que se identificó pero no entró:
    queda en la auditoría de accesos y no toca la responsabilidad del aula.
    """
    uid = evento.datos["uid_hex"]
    resultado = evento.datos["resultado"]
    efectos = [
        RegistrarAcceso(
            ubicacion_id=evento.ubicacion_id,
            uid_hex=uid,
            resultado=resultado,
            ts=evento.ts,
            modo_degradado=bool(evento.datos.get("modo_degradado", False)),
        )
    ]

    if resultado != "CONCEDIDO":
        return efectos

    # Relevo: incluso si es el MISMO UID. Una segunda pasada renueva la
    # sesión en vez de interpretarse como salida (spec §14): evita la
    # ambigüedad salida/re-ingreso, que en v1 no se puede desambiguar.
    if sesion is not None:
        efectos.append(FinalizarSesion(sesion.id, RELEVO, evento.ts))
    efectos.append(CrearSesion(evento.ubicacion_id, uid, evento.ts))
    return efectos


def _puerta(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Cambio del reed. Informativo: el reed NUNCA cierra una sesión."""
    estado_reed = evento.datos["estado_reed"]

    if sesion is not None:
        return [
            RegistrarPuerta(evento.ubicacion_id, sesion.id, estado_reed, evento.ts),
            *_actividad(sesion, evento.ts),
        ]

    efectos = [RegistrarPuerta(evento.ubicacion_id, None, estado_reed, evento.ts)]
    if estado_reed == "ABIERTO":
        # Se abrió sin credencial y sin pulso: forzada o con llave física.
        efectos.append(
            Alarma(evento.ubicacion_id, "APERTURA_SIN_CREDENCIAL", evento.ts)
        )
    return efectos


def _pir(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Movimiento detectado. Un mismo evento físico, dos naturalezas.

    CON sesión es pura señal de presencia (spec §7): corre el reloj de
    actividad y no deja registro propio. SIN sesión es una anomalía, y ahí
    sí deja huella auditable además de alarmar — simétrico con el armario.
    Consecuencia: `eventos_pir` solo acumula movimientos indebidos.

    Ya viene throttleado por el nodo (1 cada 30 s).

    La presencia es una condición SOSTENIDA muestreada cada 30 s, no un hecho
    puntual: una persona trabajando media hora sin sesión son ~60 muestras. El
    registro se guarda entero —es la evidencia— pero la alarma se agrupa por
    episodio, igual que PUERTA_ABIERTA_*. Sin esto, media hora de presencia
    indebida son 60 alarmas y, con EMATP conectado, 60 tickets por una persona.

    `ya_alarmado` lo provee quien llama (es un dato de la base): "ya hubo
    alarma de este código en los últimos cfg.t_recordatorio_alarma_s". Así el
    episodio se rearma solo, y si la presencia continúa vuelve a alarmar cada
    ese intervalo — no se pierde la señal de "esto sigue pasando".
    """
    if sesion is not None:
        return _actividad(sesion, evento.ts)
    # Puede ser intrusión, o el "falso cierre" de spec §9: alguien quieto a
    # quien el PIR no vio, cuya sesión se cerró por AUSENCIA. La distinción
    # la hace la auditoría mirando si viene justo después de un cierre.
    efectos = [RegistrarPir(evento.ubicacion_id, None, evento.ts)]
    if not evento.datos.get("ya_alarmado"):
        efectos.append(Alarma(evento.ubicacion_id, "PRESENCIA_SIN_SESION", evento.ts))
    return efectos


def _armario(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Apertura de un armario de CPU. Solo el flanco de apertura se releva.

    El `armario_id` es único dentro de su ubicación, no globalmente: el
    armario 3 del lab01 y el del lab02 son distintos.
    """
    armario_id = int(evento.datos["armario_id"])

    if sesion is not None:
        return [
            RegistrarArmario(evento.ubicacion_id, sesion.id, armario_id, evento.ts),
            *_actividad(sesion, evento.ts),
        ]

    return [
        RegistrarArmario(evento.ubicacion_id, None, armario_id, evento.ts),
        Alarma(
            evento.ubicacion_id,
            "ARMARIO_SIN_SESION",
            evento.ts,
            detalle={"armario_id": armario_id},
        ),
    ]


# --- Tareas programadas --------------------------------------------------


def _tarea_ausencia(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Corre cada minuto. Cierra por inactividad, o alarma si quedó abierta.

    El umbral es generoso (15 min) a propósito: el PIR no detecta a una
    persona quieta, y cerrar de más produce el falso cierre de spec §9.
    """
    if sesion is None:
        return []
    inactivo_s = (evento.ts - sesion.ultima_actividad).total_seconds()
    if inactivo_s < cfg.t_ausencia_s:
        return []

    # Se cierra salvo que el reed diga EXPLÍCITAMENTE que la puerta está
    # abierta. Antes se exigía un "CERRADO" explícito, y entonces una ubicación
    # sin dato de reed (nunca reportó, o el sensor no está cableado) no cerraba
    # nunca: quedaba un responsable eterno en el registro. Lo que falta en ese
    # caso es información sobre la PUERTA, no sobre la ausencia de la persona.
    if evento.datos.get("reed_actual") != "ABIERTO":
        return [FinalizarSesion(sesion.id, AUSENCIA, evento.ts)]

    # Puerta abierta sin gente: se fueron sin cerrar. La sesión NO se cierra
    # (el responsable sigue siéndolo), pero hay que avisar. One-shot: la
    # condición persiste y se reevalúa cada minuto.
    if sesion.alarmada_puerta_abierta:
        return []
    return [
        Alarma(
            evento.ubicacion_id,
            "PUERTA_ABIERTA_SIN_GENTE",
            evento.ts,
            sesion_id=sesion.id,
        ),
        MarcarAlarmadaPuertaAbierta(sesion.id, True),
    ]


def _tarea_puerta_abierta(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Puerta físicamente abierta demasiado tiempo, HAYA O NO gente.

    Distinta de PUERTA_ABIERTA_SIN_GENTE (esa exige ausencia): esta salta
    aunque haya actividad, para el caso de puerta trabada o dejada abierta
    a propósito. Vale con o sin sesión: es un hecho físico, no de sesión.

    Quien llama provee `abierta_desde` (cuándo abrió) y `ya_alarmado` (si ya
    hubo alarma en este episodio), porque son datos de la base. El one-shot
    lo garantiza `ya_alarmado`: se rearma solo cuando la puerta se cierra.
    """
    if evento.datos.get("reed_actual") != "ABIERTO":
        return []
    abierta_desde = evento.datos.get("abierta_desde")
    if abierta_desde is None or evento.datos.get("ya_alarmado"):
        return []
    if (evento.ts - abierta_desde).total_seconds() < cfg.t_puerta_abierta_s:
        return []
    return [
        Alarma(
            evento.ubicacion_id,
            "PUERTA_ABIERTA_PROLONGADA",
            evento.ts,
            sesion_id=sesion.id if sesion else None,
            detalle={"abierta_desde": abierta_desde.isoformat()},
        )
    ]


def _tarea_fin_jornada(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Cierre administrativo. Nadie queda como responsable de un día para otro.

    `corte` es el instante de cierre de HOY (lo calcula quien llama, que es
    quien sabe la zona horaria). Solo se cierran las sesiones que empezaron
    ANTES del corte: si alguien fichó a las 22:30, esa sesión es de la jornada
    siguiente y cerrarla en el próximo minuto sería absurdo.
    """
    if sesion is None:
        return []
    corte = evento.datos.get("corte")
    if corte is not None and sesion.inicio >= corte:
        return []
    return [FinalizarSesion(sesion.id, CIERRE_SISTEMA, evento.ts)]


def _tarea_nodo_mudo(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Un nodo que dejó de latir. Ceguera silenciosa (spec §6).

    Es lo más parecido a un fallo invisible que tiene el sistema: sin
    heartbeats el servidor no recibe nada y, si nadie mira, "no pasa nada" se
    confunde con "no me estoy enterando de nada". Por eso alarma.

    Una sola alarma por episodio (`ya_alarmado`), que se rearma cuando el nodo
    vuelve: un nodo muerto un fin de semana no debe generar una alarma por
    minuto.
    """
    if evento.datos.get("ya_alarmado"):
        return []
    return [
        Alarma(
            evento.ubicacion_id,
            "NODO_SIN_HEARTBEAT",
            evento.ts,
            detalle={
                "nodo_id": evento.datos.get("nodo_id"),
                "ultimo_heartbeat": evento.datos.get("ultimo_heartbeat"),
            },
        )
    ]


def _tarea_recuperacion(evento: Evento, sesion: Sesion | None, cfg: Config) -> list:
    """Corre al arrancar el servidor, después de un corte.

    Una sesión EN_CURSO con actividad reciente se reanuda tal cual: el
    apagón no borra la responsabilidad. Una con actividad vencida no se
    puede cerrar honestamente (no se sabe cuándo se fueron), así que se
    marca INCONSISTENTE para revisión manual en vez de inventar una hora.
    """
    if sesion is None:
        return []
    inactivo_s = (evento.ts - sesion.ultima_actividad).total_seconds()
    if inactivo_s < cfg.t_ausencia_s:
        return []
    return [
        MarcarInconsistente(sesion.id, evento.ts),
        Alarma(
            evento.ubicacion_id,
            "SESION_INCONSISTENTE",
            evento.ts,
            sesion_id=sesion.id,
            detalle={"ultima_actividad": sesion.ultima_actividad.isoformat()},
        ),
    ]


_MANEJADORES = {
    "acceso": _acceso,
    "puerta": _puerta,
    "pir": _pir,
    "armario": _armario,
    "tarea_ausencia": _tarea_ausencia,
    "tarea_puerta_abierta": _tarea_puerta_abierta,
    "tarea_fin_jornada": _tarea_fin_jornada,
    "tarea_nodo_mudo": _tarea_nodo_mudo,
    "tarea_recuperacion": _tarea_recuperacion,
}
