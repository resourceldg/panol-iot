"""Política de retención de la auditoría.

El criterio no es "cuánto entra en el disco" sino **qué pregunta tiene que
poder responder cada tabla, y hasta cuándo**:

* Las **alarmas** son tickets de EMATP. Son el registro de que algo pasó y de
  que alguien tuvo que atenderlo: no se borran nunca desde acá. Son además la
  tabla más chica del sistema (una fila por episodio, no por muestra).
* Las **sesiones** responden "quién era responsable". Es la respuesta legal
  del sistema y también es chica: no se purgan.
* Los **eventos de acceso, puerta y armario** son la evidencia que respalda a
  una alarma. Se conservan bastante más que el tiempo en que un ticket se
  discute, y bastante menos que para siempre.
* Los **eventos de PIR** son muestreo, no hechos: dos por minuto mientras haya
  presencia indebida. Son la tabla que crece y la que menos valor tiene por
  fila; el hecho quedó en la alarma.
* `eventos_procesados` es un libro de recibos para descartar reenvíos, no
  auditoría. Se purga, pero con MUCHO margen sobre la cola offline de un nodo:
  si se borrara un recibo que el nodo todavía puede reenviar, ese evento se
  registraría dos veces.

Sobre el disco: lo que hace crecer una base no es solo insertar, también es
borrar. Por eso la purga corre UNA VEZ POR DÍA, en lotes, y a una hora en que
no hay nadie — y no se llama a VACUUM FULL, que reescribe la tabla entera y
necesita el doble de espacio justo cuando falta. El autovacuum reutiliza el
espacio liberado, que es lo que se quiere en una tabla que crece y se purga
todos los días.
"""

import os
from dataclasses import dataclass

# Tamaño de lote. Borrar 200k filas en una sola sentencia toma un lock largo y
# genera un pico de WAL; en lotes, cada transacción es corta y el autovacuum va
# reciclando entremedio.
LOTE = 5_000


@dataclass(frozen=True)
class Politica:
    """Días que se conserva cada cosa. 0 o None = para siempre."""

    eventos_pir: int = int(os.environ.get("PANOL_RET_PIR_DIAS", "90"))
    eventos_puerta: int = int(os.environ.get("PANOL_RET_PUERTA_DIAS", "365"))
    eventos_armario: int = int(os.environ.get("PANOL_RET_ARMARIO_DIAS", "365"))
    eventos_acceso: int = int(os.environ.get("PANOL_RET_ACCESO_DIAS", "365"))
    # Más que cualquier corte de red plausible de un nodo. Bajarlo de acá es
    # arriesgar duplicados en la auditoría, no ahorrar espacio.
    eventos_procesados: int = int(os.environ.get("PANOL_RET_RECIBOS_DIAS", "60"))
    # Las alarmas son tickets y las sesiones son la respuesta del sistema:
    # no se purgan desde acá.
    alarmas: int = 0
    sesiones: int = 0


# Tabla -> columna de fecha. Solo lo que se purga figura acá.
_COLUMNA_FECHA = {
    "eventos_pir": "timestamp",
    "eventos_puerta": "timestamp",
    "eventos_armario": "timestamp",
    "eventos_acceso": "timestamp",
    "eventos_procesados": "recibido",
}


def purgar(conn, politica: Politica | None = None) -> dict:
    """Borra lo vencido según la política. Devuelve cuántas filas por tabla.

    Es idempotente y barata cuando no hay nada que borrar: el índice por fecha
    hace que la consulta no encuentre candidatos y no toque una sola página.
    """
    politica = politica or Politica()
    borradas = {}

    for tabla, columna in _COLUMNA_FECHA.items():
        dias = getattr(politica, tabla, 0)
        if not dias:
            continue

        total = 0
        while True:
            # DELETE por lotes usando la clave primaria: el subselect ordena
            # por fecha y el borrado toca solo esas filas.
            fila = conn.execute(
                f"""
                WITH vencidas AS (
                    SELECT {'event_id' if tabla == 'eventos_procesados' else 'id'} AS k
                    FROM {tabla}
                    WHERE {columna} < now() - make_interval(days => %s)
                    LIMIT %s
                )
                DELETE FROM {tabla} t
                USING vencidas v
                WHERE t.{'event_id' if tabla == 'eventos_procesados' else 'id'} = v.k
                RETURNING 1
                """,
                (dias, LOTE),
            ).fetchall()
            n = len(fila)
            total += n
            if n < LOTE:
                break

        if total:
            borradas[tabla] = total

    return borradas


def tamanos(conn) -> list[dict]:
    """Tamaño de cada tabla, para poder mirar la política contra la realidad."""
    return conn.execute(
        """
        SELECT relname AS tabla,
               n_live_tup AS filas,
               pg_size_pretty(pg_total_relation_size(relid)) AS tamano
        FROM pg_stat_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
        """
    ).fetchall()
