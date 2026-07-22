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
    hora_fin_jornada: int = 22


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
