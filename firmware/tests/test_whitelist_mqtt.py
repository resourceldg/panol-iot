"""Whitelist por MQTT (red.py), sin hardware ni MicroPython.

    python -m unittest discover -s firmware/tests -t .

El servidor publica la lista retenida en panol/<ubic>/<nodo>/cmd/whitelist.
Lo que se cuida:
- llega por el enlace y se guarda en flash; una lista mal formada se ignora;
- con MQTT andando NO se baja por HTTPS (eso cortaba el enlace ~9 s cada
  15 min y dejaba la placa sin mirar los sensores);
- HTTPS queda de respaldo: MQTT caído hace rato, o un servidor que no publica.
"""
import json
import os
import sys
import tempfile
import types
import unittest

from firmware.tests.test_red import RUTA_RED, RelojFalso, RespuestaFalsa, Ticks, WLANFalsa

TOPIC = "panol/panol-lab01/panol-lab01-puerta/cmd/whitelist"


class EnlaceFalso:
    def __init__(self):
        self.SUSCRIPCIONES = []
        self.al_mensaje = None
        self.arriba = True
        self.id = 1
        self.desde_ms = 0
        self.caido_ms = 0
        self.cierres = 0

    def conectado(self):
        return self.arriba

    def conexion_id(self):
        return self.id

    def conectado_hace_ms(self, ahora):
        return self.desde_ms if self.arriba else 0

    def caido_hace_ms(self, ahora):
        return 0 if self.arriba else self.caido_ms

    def toca_intentar(self, ahora):
        return False

    def tarea(self, ahora, rssi=None, degradado=False):
        return self.arriba

    def cerrar(self, motivo=""):
        self.cierres += 1


def cargar():
    ticks, wlan, reloj, peticiones = Ticks(), WLANFalsa(), RelojFalso(), []
    wlan.visibles, wlan.claves, reloj.hay_ntp = {"casa": -50}, {"casa": "clave-casa"}, True
    archivo = os.path.join(tempfile.mkdtemp(), "whitelist.txt")

    def peticion(metodo):
        def f(url, **kw):
            peticiones.append((metodo, url))
            return RespuestaFalsa()
        return f

    config = types.SimpleNamespace(
        USAR_RED=True, USAR_MQTT=True, REDES_WIFI=[("casa", "clave-casa")],
        T_CONEXION_WIFI_MS=15_000, T_REINTENTO_WIFI_MS=30_000, T_RESYNC_NTP_MS=6 * 3_600_000,
        T_TIMEOUT_NTP_S=2, T_TIMEOUT_HTTP_S=8, T_HEARTBEAT_MS=60_000,
        T_REFRESCO_WHITELIST_MS=900_000, T_REINTENTO_RED_MS=30_000, T_RESPALDO_HTTP_MS=120_000,
        T_ESPERA_WHITELIST_MQTT_MS=60_000, LOTE_COLA=10, SERVER_URL="http://servidor:18500",
        NODO_ID="panol-lab01-puerta", UBICACION_ID="panol-lab01", ARCHIVO_WHITELIST=archivo)
    enlace = EnlaceFalso()
    sys.modules.update({
        "network": types.SimpleNamespace(WLAN=lambda *a: wlan, STA_IF=0),
        "urequests": types.SimpleNamespace(post=peticion("POST"), get=peticion("GET")),
        "config": config, "reloj": reloj, "enlace": enlace,
        "cola": types.SimpleNamespace(pendientes=lambda n: [], confirmar=lambda n: None, largo=lambda: 0),
    })
    with open(RUTA_RED) as f:
        fuente = f.read().replace("from time import ticks_ms, ticks_diff, ticks_add", "")
    ns = {"ticks_ms": ticks.ticks_ms, "ticks_add": ticks.ticks_add,
          "ticks_diff": ticks.ticks_diff, "__name__": "red"}
    exec(compile(fuente, RUTA_RED, "exec"), ns)
    return ns, ticks, enlace, peticiones, archivo


class PruebaWhitelistMqtt(unittest.TestCase):
    def setUp(self):
        self.red, self.ticks, self.enlace, self.pet, self.archivo = cargar()
        self.recargas = 0

    def tearDown(self):
        for m in ("network", "urequests", "config", "cola", "reloj", "enlace"):
            sys.modules.pop(m, None)

    def girar(self, ms, paso=50):
        for _ in range(ms // paso):
            self.red["tareas"](self.ticks.ticks_ms(), self._recargar)
            self.ticks.ms += paso

    def _recargar(self):
        self.recargas += 1

    def llega(self, payload):
        self.enlace.al_mensaje(TOPIC, json.dumps(payload).encode())

    def https(self):
        return [u for _, u in self.pet if "whitelist" in u]

    def test_se_suscribe_al_topic_del_nodo(self):
        self.assertEqual(self.enlace.SUSCRIPCIONES, [TOPIC])

    def test_la_lista_llega_se_guarda_y_se_recarga(self):
        self.girar(1000)                         # registra el callback de recarga
        self.llega({"uids": ["CF:8C:11:0E", "C1:D1:3D:05"]})
        with open(self.archivo) as f:
            self.assertEqual(f.read().split(), ["CF:8C:11:0E", "C1:D1:3D:05"])
        self.assertEqual(self.recargas, 1)

    def test_lista_mal_formada_se_ignora(self):
        self.girar(1000)
        self.llega({"uids": ["CF:8C:11:0E"]})
        self.llega({"uids": ["no-es-un-uid"]})
        with open(self.archivo) as f:
            self.assertEqual(f.read().split(), ["CF:8C:11:0E"])

    def test_con_mqtt_andando_no_baja_por_https_ni_corta_el_enlace(self):
        self.girar(1000)
        self.llega({"uids": ["CF:8C:11:0E"]})
        self.enlace.desde_ms = 10_000_000
        self.girar(40 * 60_000)                  # 40 minutos
        self.assertEqual(self.https(), [])
        self.assertEqual(self.enlace.cierres, 0)

    def test_servidor_que_no_publica_cae_a_https(self):
        self.enlace.desde_ms = 61_000            # conectado hace más de 1 min, sin lista
        self.girar(5000)
        self.assertEqual(len(self.https()), 1)

    def test_mqtt_caido_hace_rato_usa_https(self):
        self.enlace.arriba, self.enlace.caido_ms = False, 130_000
        self.girar(5000)
        self.assertEqual(len(self.https()), 1)


if __name__ == "__main__":
    unittest.main()
