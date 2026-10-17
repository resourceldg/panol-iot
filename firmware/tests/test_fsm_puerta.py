"""Pruebas de la FSM de la puerta (main.py) con el hardware simulado.

    python -m unittest discover -s firmware/tests -t .

Corre el main.py real: se reemplazan `machine`, el lector RC522, la red, el
reloj y la cola por dobles, y el `while True` por un bucle finito con ticks
simulados (50 ms por vuelta). Lo que se cuida es el contrato con el servidor:

- en un ingreso legítimo el ACCESO CONCEDIDO sale ANTES que la apertura de la
  puerta (al revés, el servidor la veía "sin sesión" y alarmaba en falso);
- se confirma aunque la puerta abra durante el pulso, sin esperar que termine;
- "liberó sin entrar", denegado y apertura sin tarjeta no cambian;
- solo abren las tarjetas de la whitelist del servidor: el botón genera un
  pedido de alta PENDIENTE y las altas locales viejas se reportan así.
"""
import os
import sys
import tempfile
import types
import unittest

RUTA = "firmware/nodo_panol/main.py"


class Pin:
    IN, OUT, PULL_UP, PULL_DOWN = 0, 1, 2, 3
    valores = {}

    def __init__(self, n, modo=None, pull=None):
        self.n = n
        Pin.valores.setdefault(n, 0)

    def value(self, v=None):
        if v is None:
            return Pin.valores[self.n]
        Pin.valores[self.n] = v


class Lector:
    OK, REQIDL = 0, 0x26

    def __init__(self, **kw):
        self.tarjeta = None           # UID (lista de 4 bytes) presente

    def _rreg(self, reg):
        return 0x92

    def request(self, modo):
        return (self.OK if self.tarjeta else 1), 0

    def anticoll(self):
        return self.OK, self.tarjeta + [0]

    def init(self):
        pass


class Sim:
    """Arma el entorno y corre main.py hasta `vueltas`, con un guion de
    acciones por vuelta: {vuelta: callable}."""

    def __init__(self, whitelist=("AA:BB:CC:DD",), uids_locales=()):
        self.dir = tempfile.mkdtemp()
        self.eventos = []
        self.ms = 0
        Pin.valores = {}
        with open(os.path.join(self.dir, "whitelist.txt"), "w") as f:
            f.write("\n".join(whitelist))
        if uids_locales:
            with open(os.path.join(self.dir, "uids.txt"), "w") as f:
                f.write("\n".join(uids_locales))

        import importlib.util
        spec = importlib.util.spec_from_file_location("cfg_real", "firmware/nodo_panol/config.py")
        sys.modules.pop("secrets", None)
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        cfg.ARCHIVO_WHITELIST = os.path.join(self.dir, "whitelist.txt")
        cfg.ARCHIVO_UIDS = os.path.join(self.dir, "uids.txt")
        cfg.ARCHIVO_BOOT = os.path.join(self.dir, "boot_id.txt")
        cfg.SENSORES = {"reed": True, "pir": False, "rfid": True}
        cfg.USAR_WDT = False
        self.cfg = cfg
        self.lector = Lector()

        sim = self
        sys.modules.update({
            "machine": types.SimpleNamespace(Pin=Pin, WDT=None),
            "mfrc522": types.SimpleNamespace(MFRC522=lambda **kw: sim.lector),
            "config": cfg,
            "cola": types.SimpleNamespace(encolar=lambda e: sim.eventos.append(e), largo=lambda: 0),
            "red": types.SimpleNamespace(conectar=lambda: False, degradado=lambda: False,
                                         tareas=lambda *a: None),
            "reloj": types.SimpleNamespace(sincronizar=lambda: None, hora_local=lambda: None,
                                           ahora_iso=lambda: None),
            "gc": types.SimpleNamespace(collect=lambda: None),
        })

    def ticks_ms(self):
        return self.ms

    def correr(self, vueltas, guion=None):
        guion = guion or {}
        with open(RUTA) as f:
            fuente = f.read()
        fuente = fuente.replace("from time import sleep_ms, ticks_ms, ticks_diff", "")
        fuente = fuente.replace("while True:", "for _vuelta in range(%d):\n    _guion(_vuelta)" % vueltas)
        ns = {"__name__": "main", "ticks_ms": self.ticks_ms,
              "ticks_diff": lambda a, b: a - b, "sleep_ms": self._dormir,
              "_guion": lambda v: guion.get(v, lambda: None)()}
        exec(compile(fuente, RUTA, "exec"), ns)
        self.ns = ns
        return self

    def _dormir(self, ms):
        self.ms += ms

    # --- acciones del guion ---
    def tarjeta(self, uid):
        def f():
            self.lector.tarjeta = [int(x, 16) for x in uid.split(":")]
        return f

    def sin_tarjeta(self):
        def f():
            self.lector.tarjeta = None
        return f

    def puerta(self, abierta):
        def f():
            Pin.valores[self.cfg.PIN_REED] = 1 if abierta else 0
        return f

    def resumen(self):
        """[(tipo, dato principal)] sin el estado inicial."""
        out = []
        for e in self.eventos:
            d = e["datos"]
            if e["tipo"] == "puerta" and d.get("inicial"):
                continue
            out.append((e["tipo"], d.get("resultado") or d.get("estado_reed") or d.get("uid_hex")))
        return out


def juntar(*pares):
    g = {}
    for v, f in pares:
        anterior = g.get(v)
        g[v] = (lambda a, b: (lambda: (a(), b())))(anterior, f) if anterior else f
    return g


AUT = "AA:BB:CC:DD"


class PruebaFsm(unittest.TestCase):
    def test_ingreso_abriendo_en_la_promesa_acceso_antes_que_puerta(self):
        s = Sim()
        # pulso 800 ms = 16 vueltas: abre en la vuelta 40 (promesa)
        s.correr(80, juntar((5, s.tarjeta(AUT)), (8, s.sin_tarjeta()), (40, s.puerta(True)),
                            (70, s.puerta(False))))
        self.assertEqual(s.resumen(), [("acceso", "CONCEDIDO"), ("puerta", "ABIERTO"),
                                       ("puerta", "CERRADO")])

    def test_ingreso_abriendo_durante_el_pulso_confirma_en_el_acto(self):
        s = Sim()
        s.cfg.T_PULSO_SOLENOIDE_MS = 3000          # como la placa: 60 vueltas de pulso
        s.correr(40, juntar((5, s.tarjeta(AUT)), (8, s.sin_tarjeta()), (12, s.puerta(True))))
        self.assertEqual(s.resumen(), [("acceso", "CONCEDIDO"), ("puerta", "ABIERTO")])
        self.assertEqual(s.ns["estado"], "IDLE")
        self.assertEqual(Pin.valores[s.cfg.PIN_RELE], 1 - s.cfg.RELE_ACTIVO_EN, "relé suelto")

    def test_libero_sin_entrar(self):
        s = Sim()
        s.correr(260, juntar((5, s.tarjeta(AUT)), (8, s.sin_tarjeta())))
        self.assertEqual(s.resumen(), [("acceso", "SIN_INGRESO")])

    def test_abrir_despues_de_la_promesa_es_apertura_sola(self):
        s = Sim()
        s.correr(300, juntar((5, s.tarjeta(AUT)), (8, s.sin_tarjeta()), (260, s.puerta(True))))
        self.assertEqual(s.resumen(), [("acceso", "SIN_INGRESO"), ("puerta", "ABIERTO")])

    def test_denegado(self):
        s = Sim()
        s.correr(30, juntar((5, s.tarjeta("11:22:33:44")), (8, s.sin_tarjeta())))
        self.assertEqual(s.resumen(), [("acceso", "DENEGADO")])

    def test_apertura_sin_tarjeta(self):
        s = Sim()
        s.correr(30, {10: s.puerta(True)})
        self.assertEqual(s.resumen(), [("puerta", "ABIERTO")])

    def test_rechazo_parpadea_el_led_y_despues_lo_apaga(self):
        s = Sim()
        led = s.cfg.PIN_LED_ACCESO
        estados = []
        guion = juntar((5, s.tarjeta("11:22:33:44")), (8, s.sin_tarjeta()))
        for v in range(7, 40):
            guion = juntar(*[(k, f) for k, f in guion.items()],
                           (v, (lambda: estados.append(Pin.valores.get(led, 0)))))
        s.correr(40, guion)
        cambios = sum(1 for a, b in zip(estados, estados[1:]) if a != b)
        self.assertGreaterEqual(cambios, 4, "tiene que parpadear (prender/apagar varias veces)")
        self.assertEqual(Pin.valores[led], 0, "y quedar apagado")

    def test_el_uid_de_prueba_ya_no_abre(self):
        s = Sim()
        s.correr(30, juntar((5, s.tarjeta("DE:AD:BE:EF")), (8, s.sin_tarjeta())))
        self.assertEqual(s.resumen(), [("acceso", "DENEGADO")])

    def test_boton_pide_alta_pendiente_y_no_abre(self):
        s = Sim()
        boton = s.cfg.PIN_BOTON_GRABAR

        def apretar():
            Pin.valores[boton] = 1
        s.correr(40, juntar((3, apretar), (5, s.tarjeta("11:22:33:44")), (8, s.sin_tarjeta()),
                            (10, lambda: Pin.valores.__setitem__(boton, 0)),
                            (20, s.tarjeta("11:22:33:44")), (23, s.sin_tarjeta())))
        self.assertEqual(s.resumen(), [("alta_pendiente", "11:22:33:44"), ("acceso", "DENEGADO")])

    def test_altas_locales_viejas_se_reportan_como_pendientes(self):
        s = Sim(whitelist=(AUT,), uids_locales=(AUT, "0D:E5:00:BD", "1D:0E:2C:BC"))
        s.correr(5)
        self.assertEqual(sorted(s.resumen()), [("alta_pendiente", "0D:E5:00:BD"),
                                               ("alta_pendiente", "1D:0E:2C:BC")])
        self.assertFalse(os.path.exists(s.cfg.ARCHIVO_UIDS))
        self.assertTrue(os.path.exists(s.cfg.ARCHIVO_UIDS + ".migrado"))
        # Y no abren.
        s2 = Sim(whitelist=(AUT,))
        s2.correr(30, juntar((5, s2.tarjeta("0D:E5:00:BD")), (8, s2.sin_tarjeta())))
        self.assertEqual(s2.resumen(), [("acceso", "DENEGADO")])


if __name__ == "__main__":
    unittest.main()
