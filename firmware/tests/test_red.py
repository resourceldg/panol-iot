"""Pruebas de la capa de red del nodo, sin hardware ni MicroPython.

    python -m unittest discover -s firmware/tests -t .

`red.py` es código Python puro: lo único que lo ata al ESP32 son `network`,
`urequests` y los `ticks_*` de MicroPython. Se sustituyen por dobles y se
simula el bucle principal a 50 ms por vuelta, que es lo que hace `main.py`.

Lo que se cuida acá es lo que en la placa cuesta ver: que un AP caído no deje
al nodo mudo para siempre, que el reloj se recupere, y que nada de esto
bloquee el bucle que atiende la puerta.
"""
import sys
import types
import unittest

RUTA_RED = "firmware/nodo_panol/red.py"

# El contador de MicroPython desborda (2^30 ms ~ 12.4 días de uptime). Se
# simula con el mismo período para que el test cruce ese desborde de verdad.
PERIODO = 1 << 30


class Ticks:
    def __init__(self):
        self.ms = 0

    def ticks_ms(self):
        return self.ms % PERIODO

    def ticks_add(self, t, d):
        return (t + d) % PERIODO

    def ticks_diff(self, a, b):
        d = (a - b) % PERIODO
        return d - PERIODO if d >= PERIODO // 2 else d


class WLANFalsa:
    """Doble del driver WiFi. `ap` es si el access point existe ahora."""

    def __init__(self):
        self.up = False
        self.ap = False
        self.pedidos = 0

    def active(self, *a):
        pass

    def isconnected(self):
        return self.up

    def connect(self, ssid, clave):
        self.pedidos += 1
        self.up = self.ap

    def disconnect(self):
        self.up = False

    def ifconfig(self):
        return ("192.168.100.77", "255.255.255.0", "192.168.100.1", "8.8.8.8")

    def status(self, clave):
        return -50


class RelojFalso:
    def __init__(self):
        self.ok = False
        self.hay_ntp = False
        self.intentos = 0

    def sincronizar(self):
        self.intentos += 1
        self.ok = self.hay_ntp
        return self.ok

    def sincronizado(self):
        return self.ok


class RespuestaFalsa:
    status_code = 200

    def json(self):
        return {"uids": [], "confirmados": [], "rechazados": []}

    def close(self):
        pass


def cargar_red():
    """Importa red.py con los módulos del ESP32 sustituidos por dobles."""
    ticks = Ticks()
    wlan = WLANFalsa()
    reloj = RelojFalso()
    peticiones = []

    def peticion(metodo):
        def f(url, **kw):
            peticiones.append((metodo, url))
            return RespuestaFalsa()
        return f

    config = types.SimpleNamespace(
        USAR_RED=True, WIFI_SSID="ssid", WIFI_PASS="clave",
        T_CONEXION_WIFI_MS=15_000, T_REINTENTO_WIFI_MS=30_000,
        T_RESYNC_NTP_MS=6 * 3_600_000, T_TIMEOUT_NTP_S=2,
        T_TIMEOUT_HTTP_S=3, T_HEARTBEAT_MS=60_000,
        T_REFRESCO_WHITELIST_MS=900_000, T_REINTENTO_RED_MS=30_000,
        LOTE_COLA=10, SERVER_URL="http://servidor:18500",
        NODO_ID="panol-lab01-puerta", UBICACION_ID="panol-lab01",
        ARCHIVO_WHITELIST="/dev/null",
    )
    cola = types.SimpleNamespace(
        pendientes=lambda n: [], confirmar=lambda n: None, largo=lambda: 0)
    requests = types.SimpleNamespace(post=peticion("POST"), get=peticion("GET"))

    sys.modules["network"] = types.SimpleNamespace(
        WLAN=lambda *a: wlan, STA_IF=0)
    sys.modules["urequests"] = requests
    sys.modules["config"] = config
    sys.modules["cola"] = cola
    sys.modules["reloj"] = reloj

    # Los ticks se inyectan en el espacio de nombres en vez de importarse.
    with open(RUTA_RED) as f:
        fuente = f.read().replace(
            "from time import ticks_ms, ticks_diff, ticks_add", "")
    ns = {
        "ticks_ms": ticks.ticks_ms, "ticks_add": ticks.ticks_add,
        "ticks_diff": ticks.ticks_diff, "__name__": "red",
    }
    exec(compile(fuente, RUTA_RED, "exec"), ns)
    return ns, ticks, wlan, reloj, peticiones


class PruebaRed(unittest.TestCase):
    def setUp(self):
        self.red, self.ticks, self.wlan, self.reloj, self.peticiones = cargar_red()

    def tearDown(self):
        # Los dobles se inyectan en sys.modules: sacarlos evita que se filtren
        # a otras suites que corran en el mismo proceso.
        for m in ("network", "urequests", "config", "cola", "reloj"):
            sys.modules.pop(m, None)

    def girar(self, ms, paso=50):
        """Simula el bucle principal durante `ms`, atendiendo red en cada vuelta."""
        for _ in range(ms // paso):
            self.red["tareas"](self.ticks.ticks_ms(), lambda: None)
            self.ticks.ms += paso

    # --- Arranque sin AP ---------------------------------------------------

    def test_sin_ap_no_bloquea_ni_habla_http(self):
        self.girar(250)
        self.assertEqual(self.wlan.pedidos, 1, "reintenta demasiado seguido")
        self.assertEqual(self.peticiones, [], "intentó HTTP sin WiFi")

    def test_sin_ap_reintenta_con_backoff(self):
        self.girar(5 * 60_000)
        # ~1 intento cada 30 s, no uno por vuelta del bucle.
        self.assertGreaterEqual(self.wlan.pedidos, 8)
        self.assertLessEqual(self.wlan.pedidos, 12)

    # --- Recuperación ------------------------------------------------------

    def test_al_volver_el_ap_se_pone_en_hora_y_refresca_whitelist(self):
        self.girar(60_000)
        self.wlan.ap = True
        self.reloj.hay_ntp = True
        self.girar(60_000)

        self.assertTrue(self.reloj.sincronizado(), "no se puso en hora al volver")
        urls = [u for _, u in self.peticiones]
        self.assertTrue(any("whitelist" in u for u in urls), "no refrescó la whitelist")
        self.assertTrue(any("heartbeat" in u for u in urls), "no mandó heartbeat")

    def test_ap_que_se_cae_en_caliente_se_reintenta(self):
        """La regresión que motivó todo esto: antes el nodo quedaba mudo."""
        self.wlan.ap = True
        self.reloj.hay_ntp = True
        self.girar(60_000)
        self.assertTrue(self.wlan.isconnected())

        self.wlan.ap = False
        self.wlan.up = False
        antes = self.wlan.pedidos
        self.girar(2 * 60_000)
        self.assertGreaterEqual(self.wlan.pedidos - antes, 3)

    def test_ntp_caido_no_se_martilla(self):
        self.wlan.ap = True
        self.girar(60_000)
        self.reloj.intentos = 0
        self.girar(60_000)
        # Un intento cada T_REINTENTO_RED_MS (30 s), no uno cada 50 ms.
        self.assertLessEqual(self.reloj.intentos, 3)

    # --- Uptime largo ------------------------------------------------------

    def test_sigue_reintentando_tras_el_desborde_de_ticks(self):
        """A los ~12.4 días ticks_ms() vuelve a cero. Con sumas comunes el
        backoff queda fuera de rango y las comparaciones dejan de tener
        sentido; con ticks_add el nodo sigue reintentando igual."""
        self.ticks.ms = PERIODO - 10_000
        antes = self.wlan.pedidos
        self.girar(2 * 60_000)
        self.assertGreaterEqual(self.wlan.pedidos - antes, 3)


if __name__ == "__main__":
    unittest.main()
