"""Pruebas del despacho de sesiones a EMATP (Fase 2).

Espeja al test de alarmas: se prueba la bandeja de salida, no el HTTP. Lo
propio de sesiones es que se empujan al crear Y al cerrar (el flag se vuelve a
prender), y que llevan el `ematp_user_id` del espejo de identidad.
"""

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import servicio
from adapters import emisor_sesiones
from db import repositorio
from engine import modelo as m

LAB01 = "panol-lab01"
UID = "CF:8C:11:0E"
T0 = datetime(2026, 8, 6, 12, 0, 0, tzinfo=repositorio.TZ)


class TestEmisorSesiones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
        repositorio.inicializar(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.execute(
            "TRUNCATE sesiones, usuarios, credenciales, ubicaciones,"
            " eventos_procesados RESTART IDENTITY CASCADE"
        )
        repositorio.asegurar_ubicacion(self.conn, LAB01)
        # Un usuario espejado de EMATP + su tarjeta: así la sesión sale con
        # ematp_user_id resuelto.
        self.conn.execute(
            "INSERT INTO usuarios (ematp_user_id, email, nombre, apellido)"
            " VALUES (13, 'lucas@gmail.ar', 'Lucas', 'Gómez')"
        )
        self.conn.execute(
            "INSERT INTO credenciales (uid_hex, usuario_id, activa)"
            " SELECT %s, id, TRUE FROM usuarios WHERE ematp_user_id = 13",
            (UID,),
        )
        self.enviadas = []
        self._post_real = emisor_sesiones._post
        self._hab_real = emisor_sesiones.habilitado
        emisor_sesiones.habilitado = lambda: True

    def tearDown(self):
        emisor_sesiones._post = self._post_real
        emisor_sesiones.habilitado = self._hab_real

    def responder(self, ok, acepta=None):
        def _post(sesiones):
            self.enviadas.extend(sesiones)
            if not ok:
                return False, "sin conexión"
            ids = [s["id"] for s in sesiones]
            return True, ids if acepta is None else [i for i in ids if i in acepta]
        emisor_sesiones._post = _post

    def crear_sesion(self, ts=T0):
        ev = m.Evento("acceso", LAB01, ts, event_id=None,
                      datos={"uid_hex": UID, "resultado": "CONCEDIDO"})
        servicio.ingerir(self.conn, ev)

    def pendientes_n(self):
        return self.conn.execute(
            "SELECT count(*) n FROM sesiones WHERE push_pendiente"
        ).fetchone()["n"]

    # --- Casos --------------------------------------------------------------

    def test_una_sesion_nueva_se_empuja_con_ematp_user_id(self):
        self.crear_sesion()
        self.responder(ok=True)
        resumen = emisor_sesiones.despachar(self.conn)

        self.assertEqual(resumen.get("enviadas"), 1)
        self.assertEqual(len(self.enviadas), 1)
        self.assertEqual(self.enviadas[0]["ematp_user_id"], 13)
        self.assertEqual(self.enviadas[0]["uid_hex"], UID)
        self.assertEqual(self.enviadas[0]["estado"], "EN_CURSO")
        self.assertEqual(self.pendientes_n(), 0, "aceptada => flag apagado")

    def test_el_cierre_reactiva_el_push(self):
        """Empujada al nacer, se apaga; al cerrarse por relevo se vuelve a prender."""
        self.crear_sesion(T0)
        self.responder(ok=True)
        emisor_sesiones.despachar(self.conn)
        self.assertEqual(self.pendientes_n(), 0)

        # Otra pasada de la misma tarjeta = relevo: cierra la sesión 1.
        self.crear_sesion(T0.replace(minute=30))
        # La sesión 1 quedó COMPLETA y pendiente de re-empujar; la 2, nueva.
        self.assertEqual(self.pendientes_n(), 2)

        self.enviadas.clear()
        emisor_sesiones.despachar(self.conn)
        cerrada = [s for s in self.enviadas if s["estado"] == "COMPLETA"]
        self.assertEqual(len(cerrada), 1)
        self.assertEqual(cerrada[0]["motivo_cierre"], "RELEVO")

    def test_si_falla_queda_pendiente(self):
        self.crear_sesion()
        self.responder(ok=False)
        emisor_sesiones.despachar(self.conn)
        self.assertEqual(self.pendientes_n(), 1, "no se apaga si no hubo ACK")
        fila = self.conn.execute(
            "SELECT push_reintentos FROM sesiones"
        ).fetchone()
        self.assertEqual(fila["push_reintentos"], 1)

    def test_sin_usuario_espejado_va_null_pero_se_empuja(self):
        """Una tarjeta sin usuario EMATP: la sesión igual se registra."""
        self.conn.execute("UPDATE credenciales SET usuario_id = NULL")
        self.crear_sesion()
        self.responder(ok=True)
        emisor_sesiones.despachar(self.conn)
        self.assertIsNone(self.enviadas[0]["ematp_user_id"])
        self.assertEqual(self.pendientes_n(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
