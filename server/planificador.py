"""Latido del servidor: corre las tareas periódicas del motor.

Este proceso es el que faltaba. El motor tenía escritas —y probadas— las
tareas de ausencia, fin de jornada, puerta abierta y nodo mudo, pero **nadie
las llamaba en producción**. El efecto era invisible desde afuera y grave: las
sesiones nunca se cerraban solas, así que el pañol quedaba con un responsable
eterno hasta que otro pasara la tarjeta, y las alarmas que dependen del paso
del tiempo simplemente no existían.

Por qué un proceso aparte y no un hilo en la API: gunicorn corre con varios
workers, y un hilo dentro de la API se duplicaría por worker. Además el
planificador tiene que seguir latiendo aunque la cáscara HTTP esté caída —
misma razón por la que el puente MQTT tampoco depende de ella.

Aun así, no se confía en que haya una sola instancia: cada vuelta toma un lock
de aviso de Postgres y, si no lo consigue, se saltea. Dos planificadores
levantados por error no duplican alarmas.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import servicio
from db import repositorio
from engine import modelo as m

INTERVALO_S = int(os.environ.get("PANOL_INTERVALO_TAREAS_S", "60"))
DSN = os.environ.get("PANOL_DSN")

# Misma clave arbitraria y estable para todas las instancias.
LOCK_TAREAS = 0x70616E6F6C01

_conn = None


def log(*args):
    print("[TAREAS]", *args, flush=True)


def _conexion():
    """Reconecta si la base se cayó. El planificador no debe morir por eso."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = repositorio.conectar(DSN)
    return _conn


def _una_vuelta(conn, cfg) -> list:
    """Una pasada de tareas, protegida por el lock de aviso."""
    fila = conn.execute(
        "SELECT pg_try_advisory_lock(%s) AS tomado", (LOCK_TAREAS,)
    ).fetchone()
    if not fila["tomado"]:
        log("otra instancia está corriendo las tareas; se saltea esta vuelta")
        return []
    try:
        return servicio.tareas_periodicas(conn, cfg)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_TAREAS,))


def main():
    cfg = m.Config()
    log("arrancado. Intervalo:", INTERVALO_S, "s |",
        "ausencia:", cfg.t_ausencia_s, "s |",
        "fin de jornada:", cfg.hora_fin_jornada, "h")

    while True:
        try:
            efectos = _una_vuelta(_conexion(), cfg)
            for efecto in efectos:
                nombre = type(efecto).__name__
                if nombre == "Alarma":
                    log("ALARMA", efecto.codigo, "en", efecto.ubicacion_id)
                elif nombre == "FinalizarSesion":
                    log("cierra sesión", efecto.sesion_id, "por", efecto.motivo)
                else:
                    log(nombre)
        except Exception as e:
            # Una excepción acá dejaría al sistema sin latido y sin que nadie
            # se entere. Se registra y se sigue; la conexión se rehace sola.
            log("error en la vuelta:", repr(e))
            global _conn
            try:
                if _conn is not None:
                    _conn.close()
            except Exception:
                pass
            _conn = None

        time.sleep(INTERVALO_S)


if __name__ == "__main__":
    main()
