"""Sincronización de identidad EMATP → pañol (Fase 1 de la unificación).

La identidad canónica de las PERSONAS vive en EMATP (`users`, en Neon). El
pañol necesita esa identidad **local**: cuando el nodo reporta un acceso, el
servidor resuelve el usuario del uid y crea la sesión en el acto, sin poder
esperar una consulta por internet. Por eso se mantiene un espejo local de
`usuarios`, y este módulo lo mantiene al día.

Dirección única y sentido claro: EMATP manda, el pañol copia. Nunca al revés
—una escritura del pañol sobre la identidad rompería el "quién es dueño de qué"
que se acordó—. Las `credenciales` (uid → persona) sí son locales del pañol:
las tarjetas no son un concepto de EMATP.

Se lee Neon directo (read-only), igual que hace el `reset-prueba.sh` del
homelab, en vez de sumar un endpoint en EMATP: para un pull de identidad es más
simple y el homelab ya tiene el DSN de Neon en sus secretos.

Correr a mano:  EMATP_DSN=... PANOL_DSN=... python -m adapters.sync_ematp
"""

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import repositorio

# DSN de solo lectura a la base de EMATP (Neon). Sin esto, el sync no corre y
# el pañol sigue con los usuarios que ya tenga: la identidad local es un caché,
# no una dependencia dura.
EMATP_DSN = os.environ.get("EMATP_DSN")


def habilitado() -> bool:
    return bool(EMATP_DSN)


def log(*args):
    print("[SYNC-EMATP]", *args, flush=True)


def _partir_nombre(full_name: str) -> tuple[str, str]:
    """EMATP guarda `full_name` en un solo campo; el pañol tiene nombre y
    apellido por separado (y NOT NULL). Se parte por el primer espacio. No es
    perfecto (a veces el apellido va primero), pero para mostrar en la auditoría
    alcanza y el email queda como clave inequívoca."""
    partes = (full_name or "").strip().split(" ", 1)
    nombre = partes[0] if partes and partes[0] else "(sin nombre)"
    apellido = partes[1] if len(partes) > 1 else ""
    return nombre, apellido


def _leer_usuarios_ematp(dsn: str) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT id, email, full_name, role, is_active FROM users ORDER BY id"
        ).fetchall()


def sincronizar(conn, usuarios_ematp: list[dict]) -> tuple[int, int]:
    """Upsert del espejo local por `ematp_user_id`. Devuelve (altas, updates).

    Idempotente: correrlo mil veces deja el mismo resultado. El id local
    (`usuarios.id`) NO se toca —lo referencian las credenciales—; lo que ata las
    dos identidades es `ematp_user_id`.
    """
    altas = updates = 0
    with conn.transaction():
        for u in usuarios_ematp:
            nombre, apellido = _partir_nombre(u["full_name"])
            fila = conn.execute(
                """
                INSERT INTO usuarios (ematp_user_id, email, nombre, apellido,
                                      rol, activo)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (ematp_user_id) DO UPDATE SET
                    email    = EXCLUDED.email,
                    nombre   = EXCLUDED.nombre,
                    apellido = EXCLUDED.apellido,
                    rol      = EXCLUDED.rol,
                    activo   = EXCLUDED.activo
                RETURNING (xmax = 0) AS insertado
                """,
                (u["id"], u["email"], nombre, apellido, u["role"],
                 bool(u["is_active"])),
            ).fetchone()
            # xmax = 0 en la fila resultante ⇒ fue INSERT, no UPDATE.
            if fila["insertado"]:
                altas += 1
            else:
                updates += 1
    return altas, updates


def correr(conn) -> tuple[int, int]:
    """Sincroniza usando el `conn` del pañol ya abierto. Devuelve (altas, updates)."""
    if not habilitado():
        log("EMATP_DSN no configurado; se omite el sync de identidad")
        return (0, 0)
    usuarios = _leer_usuarios_ematp(EMATP_DSN)
    altas, updates = sincronizar(conn, usuarios)
    log("usuarios sincronizados:", len(usuarios), "|", altas, "altas,",
        updates, "actualizados")
    return altas, updates


def main():
    conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
    try:
        repositorio.inicializar(conn)
        correr(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
