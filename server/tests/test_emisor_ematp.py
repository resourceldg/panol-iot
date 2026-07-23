"""Pruebas del despacho de alarmas a EMATP.

Lo que se prueba es la política de la bandeja de salida, no el HTTP: si algo
falla, la alarma tiene que quedar pendiente y volver a intentarse; si sale
bien, no tiene que mandarse dos veces. La red se sustituye por un doble.
"""

import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters import emisor_ematp
from db import repositorio

LAB01 = "panol-lab01"


class BaseEmisor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
        repositorio.inicializar(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.execute("TRUNCATE alarmas, ubicaciones RESTART IDENTITY CASCADE")
        repositorio.asegurar_ubicacion(self.conn, LAB01)
        self.enviadas = []
        # Doble de la red: habilita el módulo sin tocar variables de entorno.
        self._post_real = emisor_ematp._post
        self._habilitado_real = emisor_ematp.habilitado
        emisor_ematp.habilitado = lambda: True

    def tearDown(self):
        emisor_ematp._post = self._post_real
        emisor_ematp.habilitado = self._habilitado_real

    def responder(self, ok, detalle="TK-1"):
        def _post(alarma):
            self.enviadas.append(alarma)
            return ok, detalle
        emisor_ematp._post = _post

    def alarma(self, codigo="PRESENCIA_SIN_SESION", severidad="critica", hace_min=0):
        self.conn.execute(
            "INSERT INTO alarmas (ubicacion_id, codigo, severidad, timestamp)"
            " VALUES (%s, %s, %s, now() - make_interval(mins => %s))",
            (LAB01, codigo, severidad, hace_min),
        )

    def estado(self):
        return self.conn.execute(
            "SELECT id, enviada_ematp, reintentos FROM alarmas ORDER BY id"
        ).fetchall()


class TestDespacho(BaseEmisor):
    def test_una_alarma_enviada_no_se_manda_de_nuevo(self):
        self.alarma()
        self.responder(True)

        self.assertEqual(emisor_ematp.despachar(self.conn), {"enviadas": 1})
        self.assertTrue(self.estado()[0]["enviada_ematp"])

        emisor_ematp.despachar(self.conn)
        self.assertEqual(len(self.enviadas), 1, "mandó dos veces la misma alarma")

    def test_si_ematp_no_esta_la_alarma_queda_pendiente(self):
        """Una alarma de seguridad no se pierde porque el otro sistema se cayó."""
        self.alarma()
        self.responder(False, "sin conexión")

        self.assertEqual(emisor_ematp.despachar(self.conn), {"pendientes": 1})
        fila = self.estado()[0]
        self.assertFalse(fila["enviada_ematp"])
        self.assertEqual(fila["reintentos"], 1)

    def test_cuando_ematp_vuelve_se_envia_lo_acumulado(self):
        for _ in range(3):
            self.alarma()
        self.responder(False)
        emisor_ematp.despachar(self.conn)
        self.assertEqual([f["enviada_ematp"] for f in self.estado()], [False] * 3)

        self.responder(True)
        self.assertEqual(emisor_ematp.despachar(self.conn), {"enviadas": 3})
        self.assertEqual([f["enviada_ematp"] for f in self.estado()], [True] * 3)

    def test_se_manda_lo_mas_viejo_primero(self):
        self.alarma(codigo="MODO_DEGRADADO", hace_min=30)
        self.alarma(codigo="PRESENCIA_SIN_SESION", hace_min=1)
        self.responder(True)

        emisor_ematp.despachar(self.conn)
        self.assertEqual(
            [a["codigo"] for a in self.enviadas],
            ["MODO_DEGRADADO", "PRESENCIA_SIN_SESION"],
        )

    def test_deja_de_insistir_despues_del_tope(self):
        """Si en dos horas EMATP no volvió, el problema no se arregla insistiendo."""
        self.alarma()
        self.conn.execute("UPDATE alarmas SET reintentos = %s",
                          (emisor_ematp.MAX_REINTENTOS,))
        self.responder(True)

        self.assertEqual(emisor_ematp.despachar(self.conn), {})
        self.assertEqual(self.enviadas, [])

    def test_el_lote_acota_cuanto_se_manda_por_vuelta(self):
        for _ in range(5):
            self.alarma()
        self.responder(True)

        emisor_ematp.despachar(self.conn, limite=2)
        self.assertEqual(len(self.enviadas), 2)

    def test_sin_configurar_no_hace_nada(self):
        emisor_ematp.habilitado = self._habilitado_real   # sin EMATP_URL
        self.alarma()
        self.responder(True)

        self.assertEqual(emisor_ematp.despachar(self.conn), {})
        self.assertEqual(self.enviadas, [])
        self.assertFalse(self.estado()[0]["enviada_ematp"])

    def test_la_alarma_viaja_con_lo_que_EMATP_necesita(self):
        self.conn.execute(
            "INSERT INTO alarmas (ubicacion_id, codigo, severidad, timestamp, detalle)"
            " VALUES (%s, 'NODO_SIN_HEARTBEAT', 'alta', now(),"
            "         '{\"nodo_id\": \"panol-lab01-puerta\"}'::jsonb)",
            (LAB01,),
        )
        self.responder(True)
        emisor_ematp.despachar(self.conn)

        enviada = self.enviadas[0]
        for campo in ("id", "ubicacion_id", "codigo", "severidad", "timestamp"):
            self.assertIn(campo, enviada, f"falta {campo}: EMATP lo exige")
        self.assertEqual(enviada["detalle"]["nodo_id"], "panol-lab01-puerta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
