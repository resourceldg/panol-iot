"""Pruebas del puente MQTT.

Ejercitan los callbacks directamente, con un cliente y un mensaje falsos.
Lo que se valida es lo que escribimos nosotros: el ruteo por topic, el
armado del evento, la idempotencia y la republicación hacia Node-RED. La
capa de red de paho no se prueba acá — es código de la librería, y para eso
hace falta el broker del compose.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import puente_mqtt
from db import repositorio
from tests.test_escenarios import TABLAS

LAB01 = "panol-lab01"
NODO = "panol-lab01-puerta"
ANA = "C1:D1:3D:05"
T0 = datetime(2026, 7, 21, 9, 0, 0, tzinfo=repositorio.TZ)


class ClienteFalso:
    """Registra las publicaciones en vez de mandarlas a un broker."""

    def __init__(self):
        self.publicados = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.publicados.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )

    def topics(self):
        return [p["topic"] for p in self.publicados]


class MensajeFalso:
    def __init__(self, topic, sobre):
        self.topic = topic
        self.payload = json.dumps(sobre).encode()


def sobre(tipo, datos, event_id, ts=T0):
    """Sobre tal como lo arma emitir() en el firmware."""
    return {
        "event_id": event_id,
        "ubicacion_id": LAB01,
        "nodo_id": NODO,
        "tipo": tipo,
        "timestamp": ts.isoformat(),
        "uptime_ms": 1000,
        "datos": datos,
    }


class TestPuenteMQTT(unittest.TestCase):
    def setUp(self):
        self.conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
        repositorio.inicializar(self.conn)
        self.conn.execute(
            "TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(TABLAS)
        )
        # El puente usa su propia conexión global; se fuerza a la de la prueba.
        puente_mqtt._conn = self.conn
        self.cliente = ClienteFalso()

    def tearDown(self):
        puente_mqtt._conn = None
        self.conn.close()

    def recibir(self, topic, cuerpo):
        puente_mqtt.al_recibir(self.cliente, None, MensajeFalso(topic, cuerpo))

    def test_un_acceso_por_mqtt_crea_sesion(self):
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/acceso",
            sobre("acceso", {"uid_hex": ANA, "resultado": "CONCEDIDO"}, "m1"),
        )
        sesion = repositorio.sesion_en_curso(self.conn, LAB01)
        self.assertIsNotNone(sesion)
        self.assertEqual(sesion.uid_hex, ANA)

    def test_qos1_duplicado_no_duplica_la_sesion(self):
        """QoS 1 entrega 'al menos una vez': los duplicados son normales.

        Sin idempotencia, una reentrega del broker relevaría la sesión
        consigo misma y ensuciaría la auditoría con un RELEVO inventado.
        """
        cuerpo = sobre("acceso", {"uid_hex": ANA, "resultado": "CONCEDIDO"}, "m1")
        self.recibir(f"panol/{LAB01}/{NODO}/evento/acceso", cuerpo)
        self.recibir(f"panol/{LAB01}/{NODO}/evento/acceso", cuerpo)

        total = self.conn.execute(
            "SELECT count(*) AS n FROM sesiones"
        ).fetchone()["n"]
        self.assertEqual(total, 1)

    def test_el_topic_completa_la_identidad_faltante(self):
        """Un nodo puede publicar el payload mínimo: el topic dice el resto."""
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/pir",
            {"event_id": "m2", "datos": {}, "timestamp": T0.isoformat()},
        )
        fila = self.conn.execute("SELECT * FROM alarmas").fetchone()
        self.assertEqual(fila["codigo"], "PRESENCIA_SIN_SESION")
        self.assertEqual(fila["ubicacion_id"], LAB01)

    def test_pir_sin_sesion_por_mqtt_deja_registro_y_alarma(self):
        """La doble naturaleza del PIR también vale por MQTT (mismo motor)."""
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/pir",
            sobre("pir", {}, "mp1"),
        )
        fila = self.conn.execute("SELECT * FROM eventos_pir").fetchone()
        self.assertIsNotNone(fila, "sin sesión deja registro")
        self.assertIsNone(fila["sesion_id"])
        topics = self.cliente.topics()
        self.assertIn(f"panol/{LAB01}/alarma/PRESENCIA_SIN_SESION", topics)

    def test_una_alarma_se_republica_para_nodered(self):
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/armario",
            sobre("armario", {"armario_id": 5}, "m3"),
        )
        topics = self.cliente.topics()
        self.assertIn(f"panol/{LAB01}/alarma/ARMARIO_SIN_SESION", topics)

        publicado = json.loads(self.cliente.publicados[0]["payload"])
        self.assertEqual(publicado["severidad"], "critica")
        self.assertEqual(publicado["detalle"], {"armario_id": 5})
        self.assertEqual(self.cliente.publicados[0]["qos"], 1, "las alarmas van QoS 1")

    def test_el_cambio_de_sesion_se_republica(self):
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/acceso",
            sobre("acceso", {"uid_hex": ANA, "resultado": "CONCEDIDO"}, "m4"),
        )
        sesion = [p for p in self.cliente.publicados
                  if p["topic"] == f"panol/{LAB01}/sesion"]
        self.assertEqual(len(sesion), 1)
        cuerpo = json.loads(sesion[0]["payload"])
        self.assertEqual(cuerpo["cambio"], "CrearSesion")
        self.assertEqual(cuerpo["uid_hex"], ANA)
        self.assertTrue(sesion[0]["retain"], "el estado actual debe quedar retenido")

    def test_heartbeat_registra_el_nodo(self):
        self.recibir(
            f"panol/{LAB01}/{NODO}/heartbeat",
            {"nodo_id": NODO, "ubicacion_id": LAB01, "rol": "puerta",
             "uptime": 3600, "rssi": -58, "modo_degradado": True},
        )
        fila = self.conn.execute("SELECT * FROM nodos").fetchone()
        self.assertEqual(fila["id"], NODO)
        self.assertTrue(fila["modo_degradado"])
        self.assertEqual(fila["rssi"], -58)

    def test_payload_ilegible_no_tumba_el_puente(self):
        """Un mensaje corrupto no puede dejar el sistema ciego."""
        class Basura:
            topic = f"panol/{LAB01}/{NODO}/evento/pir"
            payload = b"{esto no es json"

        puente_mqtt.al_recibir(self.cliente, None, Basura())  # no debe lanzar
        self.assertEqual(self.cliente.publicados, [])

    def test_evento_invalido_no_tumba_el_puente(self):
        """Un evento mal formado se descarta y el puente sigue vivo."""
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/acceso",
            sobre("acceso", {"uid_hex": ANA}, "m5"),   # falta `resultado`
        )
        total = self.conn.execute("SELECT count(*) AS n FROM sesiones").fetchone()["n"]
        self.assertEqual(total, 0)

        # Y el siguiente evento, ya bien formado, se procesa normalmente.
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/acceso",
            sobre("acceso", {"uid_hex": ANA, "resultado": "CONCEDIDO"}, "m6"),
        )
        self.assertIsNotNone(repositorio.sesion_en_curso(self.conn, LAB01))

    def test_atribucion_tardia_tambien_por_mqtt(self):
        """El puente usa el mismo motor, así que hereda la atribución por ts."""
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/acceso",
            sobre("acceso", {"uid_hex": ANA, "resultado": "CONCEDIDO"}, "m7"),
        )
        sesion_ana = repositorio.sesion_en_curso(self.conn, LAB01).id
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/acceso",
            sobre("acceso", {"uid_hex": "OTRO", "resultado": "CONCEDIDO"}, "m8",
                  ts=T0 + timedelta(minutes=30)),
        )
        # Evento retenido en la cola del nodo, con timestamp del turno de Ana.
        self.recibir(
            f"panol/{LAB01}/{NODO}/evento/armario",
            sobre("armario", {"armario_id": 9}, "m9", ts=T0 + timedelta(minutes=10)),
        )

        fila = self.conn.execute("SELECT * FROM eventos_armario").fetchone()
        self.assertEqual(fila["sesion_id"], sesion_ana)


if __name__ == "__main__":
    unittest.main(verbosity=2)
