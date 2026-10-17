"""Telemetría del pañol hacia ThingsBoard (adapters/telemetria_tb.py).

Lo que se cuida: el modo prueba del homelab VACÍA la base cada 6 h y los ids
vuelven a empezar. El 17/10 el alimentador siguió esperando ids altos y no
publicó nada más: en el tablero "no registraba" lo que pasaba en la puerta.
"""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import servicio
from adapters import telemetria_tb
from db import repositorio
from engine import modelo as m
from tests.test_escenarios import TABLAS

LAB01 = "panol-lab01"
NODO = "panol-lab01-puerta"


class TBFalso:
    def __init__(self):
        self.publicado = []

    def req(self, metodo, ruta, cuerpo=None):
        if ruta.startswith("/api/tenant/devices"):
            return {"id": {"id": "dev-1"}}
        if "/timeseries/" in ruta:
            self.publicado.extend(cuerpo)
        return {}

    def claves(self, k):
        return [x["values"][k] for x in self.publicado if k in x["values"]]


class TestTelemetria(unittest.TestCase):
    def setUp(self):
        self.conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
        repositorio.inicializar(self.conn)
        self.vaciar()
        for d in (telemetria_tb._ultimo, telemetria_tb._sesiones, telemetria_tb._pir_vistos,
                  telemetria_tb._dispositivos):
            d.clear()
        telemetria_tb._estado["primera"] = True
        self.tb = TBFalso()

    def tearDown(self):
        self.conn.close()

    def vaciar(self):
        self.conn.execute("TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(TABLAS))
        repositorio.registrar_heartbeat(self.conn, nodo_id=NODO, ubicacion_id=LAB01, rol="puerta")

    def ev(self, tipo, seg, eid, **datos):
        t = repositorio.ahora() - timedelta(minutes=2) + timedelta(seconds=seg)
        servicio.ingerir(self.conn, m.Evento(tipo=tipo, ubicacion_id=LAB01, ts=t, event_id=eid,
                                             nodo_id=NODO, datos=datos), via="mqtt")

    def test_publica_y_no_repite(self):
        self.ev("acceso", 0, "a1", uid_hex="AF:05:BE:DE", resultado="CONCEDIDO")
        self.ev("puerta", 0, "a2", estado_reed="ABIERTO")
        self.assertGreater(telemetria_tb.vuelta(self.conn, self.tb), 0)
        self.assertEqual(telemetria_tb.vuelta(self.conn, self.tb), 0)
        self.assertEqual(self.tb.claves("acceso"), ["CONCEDIDO"])

    def test_despues_de_vaciar_la_base_sigue_publicando(self):
        for i in range(5):
            self.ev("puerta", i, f"v{i}", estado_reed="ABIERTO" if i % 2 == 0 else "CERRADO")
        self.ev("acceso", 10, "va", uid_hex="AF:05:BE:DE", resultado="CONCEDIDO")
        telemetria_tb.vuelta(self.conn, self.tb)
        self.vaciar()                                   # reset del modo prueba
        self.tb.publicado.clear()
        self.ev("acceso", 30, "n1", uid_hex="AF:05:BE:DE", resultado="CONCEDIDO")
        self.ev("puerta", 30, "n2", estado_reed="ABIERTO")
        self.assertGreater(telemetria_tb.vuelta(self.conn, self.tb), 0)
        self.assertEqual(self.tb.claves("puerta"), [1])
        self.assertIn(1, self.tb.claves("sesion"), "la sesión nueva (mismo id 1) también se publica")
