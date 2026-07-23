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
    """Doble del driver WiFi.

    `visibles` es {ssid: rssi} de los AP en el aire. `connect()` asocia solo si
    el SSID está visible y la clave coincide con la configurada.
    """

    def __init__(self):
        self.up = False
        self.pedidos = 0
        self.visibles = {}          # ssid -> rssi
        self.claves = {}            # ssid -> clave correcta
        self.ssid_actual = None
        self.scan_rompe = False
        self.escaneos = 0

    def active(self, *a):
        pass

    def isconnected(self):
        return self.up

    def scan(self):
        self.escaneos += 1
        if self.scan_rompe:
            raise OSError("scan no disponible")
        # (ssid, bssid, canal, rssi, seguridad, hidden)
        return [(s.encode(), b"", 1, rssi, 3, False) for s, rssi in self.visibles.items()]

    def connect(self, ssid, clave):
        self.pedidos += 1
        self.ssid_actual = ssid
        self.up = (ssid in self.visibles and self.claves.get(ssid) == clave)

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
        USAR_RED=True,
        REDES_WIFI=[("casa", "clave-casa"), ("colegio", "clave-colegio")],
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
        # Ninguna red visible: no hay a qué asociarse.
        self.girar(250)
        self.assertEqual(self.peticiones, [], "intentó HTTP sin WiFi")

    def test_reintenta_con_backoff(self):
        # Una red conocida presente pero con clave mala: el connect no asocia.
        self.wlan.visibles = {"casa": -50}
        self.wlan.claves = {"casa": "otra"}
        self.girar(5 * 60_000)
        # ~1 intento cada 30 s, no uno por vuelta del bucle.
        self.assertGreaterEqual(self.wlan.pedidos, 8)
        self.assertLessEqual(self.wlan.pedidos, 12)

    # --- Recuperación ------------------------------------------------------

    def test_al_volver_el_ap_se_pone_en_hora_y_refresca_whitelist(self):
        self.girar(60_000)
        self.wlan.visibles = {"casa": -50}
        self.wlan.claves = {"casa": "clave-casa"}
        self.reloj.hay_ntp = True
        self.girar(60_000)

        self.assertTrue(self.reloj.sincronizado(), "no se puso en hora al volver")
        urls = [u for _, u in self.peticiones]
        self.assertTrue(any("whitelist" in u for u in urls), "no refrescó la whitelist")
        self.assertTrue(any("heartbeat" in u for u in urls), "no mandó heartbeat")

    def test_ap_que_se_cae_en_caliente_se_reintenta(self):
        """La regresión que motivó todo esto: antes el nodo quedaba mudo.

        Con el AP caído no hay a qué asociarse, así que el nodo no martilla
        connect() a ciegas: re-escanea esperando que vuelva una red conocida.
        Lo que importa es que sigue buscando y que reconecta al volver."""
        self.wlan.visibles = {"casa": -50}
        self.wlan.claves = {"casa": "clave-casa"}
        self.reloj.hay_ntp = True
        self.girar(60_000)
        self.assertTrue(self.wlan.isconnected())

        self.wlan.visibles = {}      # se cae el AP
        self.wlan.up = False
        antes = self.wlan.escaneos
        self.girar(2 * 60_000)
        self.assertGreaterEqual(self.wlan.escaneos - antes, 3, "dejó de buscar")

        self.wlan.visibles = {"casa": -50}   # vuelve el AP
        self.girar(60_000)
        self.assertTrue(self.wlan.isconnected(), "no reconectó al volver la red")

    def test_ntp_caido_no_se_martilla(self):
        self.wlan.visibles = {"casa": -50}
        self.wlan.claves = {"casa": "clave-casa"}
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
        self.wlan.visibles = {"casa": -50}
        self.wlan.claves = {"casa": "otra"}   # presente pero no asocia
        self.ticks.ms = PERIODO - 10_000
        antes = self.wlan.pedidos
        self.girar(2 * 60_000)
        self.assertGreaterEqual(self.wlan.pedidos - antes, 3)

    # --- Multi-red ---------------------------------------------------------

    def test_elige_la_de_mejor_senal(self):
        # Las dos conocidas presentes; colegio con mejor RSSI.
        self.wlan.visibles = {"casa": -80, "colegio": -40}
        self.wlan.claves = {"casa": "clave-casa", "colegio": "clave-colegio"}
        self.girar(30_000)
        self.assertTrue(self.wlan.isconnected())
        self.assertEqual(self.wlan.ssid_actual, "colegio", "no eligió la de mejor señal")

    def test_si_la_mejor_falla_prueba_la_siguiente(self):
        # colegio tiene mejor señal pero la clave guardada no sirve; cae a casa.
        self.wlan.visibles = {"casa": -70, "colegio": -40}
        self.wlan.claves = {"casa": "clave-casa", "colegio": "cambió-la-clave"}
        self.girar(2 * 60_000)
        self.assertTrue(self.wlan.isconnected())
        self.assertEqual(self.wlan.ssid_actual, "casa", "no rotó a la otra red")

    def test_ignora_redes_que_no_conoce(self):
        # Hay una red ajena con señal fuerte: no se intenta.
        self.wlan.visibles = {"vecino": -30, "casa": -60}
        self.wlan.claves = {"casa": "clave-casa"}
        self.girar(30_000)
        self.assertEqual(self.wlan.ssid_actual, "casa", "intentó una red ajena")

    def test_se_muda_de_red_sin_reset(self):
        # Arranca en casa; casa desaparece y aparece colegio. El nodo migra.
        self.wlan.visibles = {"casa": -50}
        self.wlan.claves = {"casa": "clave-casa", "colegio": "clave-colegio"}
        self.girar(60_000)
        self.assertEqual(self.wlan.ssid_actual, "casa")

        self.wlan.visibles = {"colegio": -50}   # se cambió de ubicación
        self.wlan.up = False
        self.girar(3 * 60_000)
        self.assertTrue(self.wlan.isconnected())
        self.assertEqual(self.wlan.ssid_actual, "colegio", "no descubrió la red nueva")

    def test_scan_roto_prueba_a_ciegas(self):
        self.wlan.scan_rompe = True
        self.wlan.visibles = {"casa": -50}       # está, aunque el scan no la vea
        self.wlan.claves = {"casa": "clave-casa"}
        self.girar(60_000)
        self.assertTrue(self.wlan.isconnected(), "no probó a ciegas con el scan roto")


if __name__ == "__main__":
    unittest.main()
