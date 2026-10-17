"""Quién puede entrar al pañol: tarjetas activas y pendientes de aprobación.

El servidor es la ÚNICA fuente de verdad. El nodo solo abre con las tarjetas
activas de acá (las baja como whitelist cada 15 min). Una tarjeta grabada con
el botón del nodo llega como PENDIENTE y no abre hasta que alguien la aprueba.

    python -m credenciales activas
    python -m credenciales pendientes
    python -m credenciales aprobar <uid> <usuario>   # usuario: id, email o "Nombre Apellido"
    python -m credenciales rechazar <uid>
    python -m credenciales baja <uid>

En el homelab:  docker exec -it panol-api python -m credenciales pendientes

Los cambios llegan al nodo en el próximo refresco de whitelist (≤ 15 min).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import repositorio


def _uid(texto: str) -> str:
    """Normaliza a AA:BB:CC:DD (como lo reporta el nodo)."""
    limpio = texto.replace(":", "").replace("-", "").strip().upper()
    if len(limpio) != 8 or any(c not in "0123456789ABCDEF" for c in limpio):
        sys.exit(f"UID inválido: {texto!r} (se espera AA:BB:CC:DD)")
    return ":".join(limpio[i:i + 2] for i in range(0, 8, 2))


def _usuario(conn, texto: str) -> dict:
    if texto.isdigit():
        filas = conn.execute("SELECT * FROM usuarios WHERE id = %s", (int(texto),)).fetchall()
    elif "@" in texto:
        filas = conn.execute("SELECT * FROM usuarios WHERE lower(email) = lower(%s)",
                             (texto,)).fetchall()
    else:
        filas = conn.execute(
            "SELECT * FROM usuarios WHERE lower(trim(nombre || ' ' || coalesce(apellido, '')))"
            " = lower(trim(%s))", (texto,)).fetchall()
    if len(filas) != 1:
        sys.exit(f"usuario {texto!r}: {len(filas)} coincidencias (usar el id o el email)")
    if not filas[0]["activo"]:
        sys.exit(f"usuario {texto!r} está inactivo")
    return filas[0]


def activas(conn):
    for f in conn.execute(
            "SELECT c.uid_hex, c.alta, coalesce(u.nombre || ' ' || coalesce(u.apellido, ''), '—') AS persona"
            " FROM credenciales c LEFT JOIN usuarios u ON u.id = c.usuario_id"
            " WHERE c.activa ORDER BY persona").fetchall():
        print(f"{f['uid_hex']}  {f['persona']:<30}  alta {f['alta'] or '—'}")


def pendientes(conn):
    filas = conn.execute(
        "SELECT * FROM credenciales_pendientes WHERE rechazada IS NULL ORDER BY primera_vez").fetchall()
    if not filas:
        print("no hay tarjetas pendientes")
    for f in filas:
        print(f"{f['uid_hex']}  desde {f['primera_vez']:%d/%m %H:%M}  nodo {f['nodo_id']}"
              f"  ({f['veces']} vez/veces)")


def aprobar(conn, uid: str, usuario: str):
    u = _usuario(conn, usuario)
    with conn.transaction():
        if conn.execute("SELECT 1 FROM credenciales WHERE uid_hex = %s AND activa", (uid,)).fetchone():
            sys.exit(f"{uid} ya está activa")
        conn.execute("INSERT INTO credenciales (uid_hex, usuario_id, alta, activa)"
                     " VALUES (%s, %s, now(), TRUE)", (uid, u["id"]))
        conn.execute("DELETE FROM credenciales_pendientes WHERE uid_hex = %s", (uid,))
    print(f"{uid} aprobada para {u['nombre']} {u['apellido'] or ''} (abre tras el próximo refresco)")


def rechazar(conn, uid: str):
    n = conn.execute("UPDATE credenciales_pendientes SET rechazada = now()"
                     " WHERE uid_hex = %s AND rechazada IS NULL", (uid,)).rowcount
    print(f"{uid}: rechazada" if n else f"{uid}: no estaba pendiente")


def baja(conn, uid: str):
    n = conn.execute("UPDATE credenciales SET activa = FALSE, baja = now()"
                     " WHERE uid_hex = %s AND activa", (uid,)).rowcount
    print(f"{uid}: dada de baja (deja de abrir tras el próximo refresco)" if n
          else f"{uid}: no estaba activa")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
    orden, args = argv[0], argv[1:]
    if orden == "activas":
        activas(conn)
    elif orden == "pendientes":
        pendientes(conn)
    elif orden == "aprobar" and len(args) >= 2:
        aprobar(conn, _uid(args[0]), " ".join(args[1:]))
    elif orden == "rechazar" and len(args) == 1:
        rechazar(conn, _uid(args[0]))
    elif orden == "baja" and len(args) == 1:
        baja(conn, _uid(args[0]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
