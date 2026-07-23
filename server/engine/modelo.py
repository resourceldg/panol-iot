"""Tipos del motor de sesiones.

Nada de esto sabe de HTTP ni de SQLite: son datos planos que entran y salen
del motor. Esa frontera es lo que permite probar la máquina de estados con
un simulador, sin levantar servidor ni base.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# --- Motivos de cierre de sesión (spec v1.0 §3) --------------------------
RELEVO = "RELEVO"
AUSENCIA = "AUSENCIA"
CIERRE_SISTEMA = "CIERRE_SISTEMA"

# --- Estados de sesión (spec v1.0 §8) ------------------------------------
EN_CURSO = "EN_CURSO"
COMPLETA = "COMPLETA"
INCONSISTENTE = "INCONSISTENTE"

# --- Códigos de anomalía y su severidad (spec v1.0 §9) -------------------
SEVERIDADES = {
    "APERTURA_SIN_CREDENCIAL": "critica",
    "PRESENCIA_SIN_SESION": "critica",
    "ARMARIO_SIN_SESION": "critica",
    "PUERTA_ABIERTA_SIN_GENTE": "alta",
    "PUERTA_ABIERTA_PROLONGADA": "alta",
    "NODO_SIN_HEARTBEAT": "alta",
    "MODO_DEGRADADO": "media",
    "SESION_INCONSISTENTE": "baja",
}


@dataclass
class Config:
    """Parámetros de la spec §12. Se pasan al motor, no se leen de globals."""

    t_ausencia_s: int = 15 * 60
    t_puerta_abierta_s: int = 5 * 60   # Puerta abierta prolongada, con o sin gente

    # Cierre de jornada POR QUIESCENCIA, no por reloj. El colegio dice 6 a 00,
    # pero hay actos, reuniones y jornadas especiales: un corte a hora fija
    # cierra sesiones de gente que sigue adentro, o deja abiertas las de un día
    # atípico. Silencio total prolongado es una señal más confiable que la hora,
    # y cierra incluso con la puerta abierta (donde la ausencia no cierra).
    t_jornada_quiescente_s: int = 90 * 60

    # Volver a pasar la MISMA tarjeta después de un cierre por ausencia reanuda
    # la sesión en vez de abrir una nueva: el profe que fue al recreo sigue
    # siendo el mismo responsable, y la auditoría no debe partir su turno en
    # pedazos. Fuera de esta ventana ya es un turno nuevo.
    t_reanudacion_s: int = 90 * 60

    # Precisión con la que se guarda la marca de actividad. Por debajo de esto
    # el UPDATE no cambia ninguna decisión (la ausencia se mide en minutos) y
    # solo genera escrituras: el PIR reporta cada 30 s durante horas.
    t_precision_actividad_s: int = 60

    # Cierre por reloj: red de seguridad OPCIONAL, apagada por defecto. Poner
    # una hora (0-23) solo si se quiere además del cierre por quiescencia.
    hora_fin_jornada: int | None = None
    # Un nodo late cada 60 s: cinco minutos de silencio ya no es una WiFi
    # temperamental, es un nodo caído.
    t_sin_heartbeat_s: int = 5 * 60
    # Cada cuánto se REPITE la alarma de una condición que sigue pasando
    # (presencia sin sesión, nodo mudo). Ni una por muestra —una persona
    # trabajando serían decenas— ni una sola para siempre, que haría creer que
    # el episodio terminó.
    t_recordatorio_alarma_s: int = 15 * 60


@dataclass
class Sesion:
    """Sesión de aula: el período de responsabilidad de un usuario.

    NO representa el estado físico de la puerta. La puerta puede abrirse y
    cerrarse por dentro sin que la sesión cambie.
    """

    id: int
    ubicacion_id: str
    uid_hex: str
    inicio: datetime
    ultima_actividad: datetime
    estado: str = EN_CURSO
    # One-shot de PUERTA_ABIERTA_SIN_GENTE: la condición dura minutos y se
    # evalúa cada minuto, así que sin este flag EMATP recibiría una alarma
    # por minuto durante todo el episodio.
    alarmada_puerta_abierta: bool = False


@dataclass
class Evento:
    """Un evento ya normalizado, venga del nodo o de una tarea programada.

    `ts` es el timestamp ORIGINAL del evento, no el de llegada al servidor.
    Con la cola offline un evento puede llegar tarde, y la atribución tiene
    que responder "quién era responsable cuando pasó", no "cuando llegó".
    """

    tipo: str                  # acceso | puerta | pir | armario | tarea_*
    ubicacion_id: str
    ts: datetime
    event_id: str | None = None
    nodo_id: str | None = None
    datos: dict[str, Any] = field(default_factory=dict)


# --- Efectos ------------------------------------------------------------
# El motor no ejecuta nada: describe qué habría que hacer. Quien lo llama
# decide si eso se escribe en SQLite, se imprime en una prueba o se
# descarta. Es lo que hace que la FSM sea auditable y determinista.


@dataclass
class CrearSesion:
    ubicacion_id: str
    uid_hex: str
    ts: datetime


@dataclass
class ReanudarSesion:
    """Reabre una sesión cerrada por ausencia: es el MISMO turno, no uno nuevo.

    Se cuenta la reanudación en la sesión para que el hueco quede a la vista:
    la auditoría tiene que poder decir "estuvo, se fue 20 minutos y volvió",
    no fabricar una continuidad que no existió.
    """

    sesion_id: int
    ts: datetime


@dataclass
class FinalizarSesion:
    sesion_id: int
    motivo: str
    ts: datetime


@dataclass
class MarcarActividad:
    sesion_id: int
    ts: datetime


@dataclass
class RegistrarAcceso:
    ubicacion_id: str
    uid_hex: str
    resultado: str             # CONCEDIDO | DENEGADO | SIN_INGRESO
    ts: datetime
    modo_degradado: bool = False


@dataclass
class RegistrarPuerta:
    ubicacion_id: str
    sesion_id: int | None      # None = anomalía (apertura sin sesión)
    estado_reed: str           # ABIERTO | CERRADO
    ts: datetime


@dataclass
class RegistrarPir:
    ubicacion_id: str
    sesion_id: int | None      # None = movimiento sin sesión (anomalía)
    ts: datetime


@dataclass
class RegistrarArmario:
    ubicacion_id: str
    sesion_id: int | None      # None = anomalía crítica
    armario_id: int
    ts: datetime


@dataclass
class Alarma:
    ubicacion_id: str
    codigo: str
    ts: datetime
    sesion_id: int | None = None
    detalle: dict[str, Any] = field(default_factory=dict)

    @property
    def severidad(self) -> str:
        return SEVERIDADES[self.codigo]


@dataclass
class MarcarInconsistente:
    """Sesión que quedó EN_CURSO tras un corte, con la actividad vencida."""

    sesion_id: int
    ts: datetime


@dataclass
class MarcarAlarmadaPuertaAbierta:
    """Arma el one-shot para no repetir la alarma durante el mismo episodio."""

    sesion_id: int
    valor: bool
