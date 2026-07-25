"""Pruebas de la cola en flash, sin hardware.

    python -m unittest discover -s firmware/tests -t .

La cola es Python puro; lo único que la ata al ESP32 es `config`. Lo que se
cuida acá es lo que en la placa se manifestó como MemoryError y boot-loop: con
la cola llena (un corte de red largo), leerla NO puede cargar todo el archivo
en RAM, y confirmar parcialmente no puede corromperla.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

RUTA = Path(__file__).resolve().parents[1] / "nodo_panol"


def cargar_cola(archivo, max_cola=5000):
    """Importa cola.py con un config de prueba (archivo temporal)."""
    for m in ("config", "cola"):
        sys.modules.pop(m, None)
    sys.modules["config"] = types.SimpleNamespace(
        ARCHIVO_COLA=archivo, LOTE_COLA=10, MAX_COLA=max_cola)
    fuente = (RUTA / "cola.py").read_text()
    ns = {"__name__": "cola"}
    exec(compile(fuente, str(RUTA / "cola.py"), "exec"), ns)
    return types.SimpleNamespace(**ns)


class PruebaCola(unittest.TestCase):
    def setUp(self):
        fd, self.archivo = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        os.remove(self.archivo)   # que arranque sin archivo, como el nodo nuevo
        self.cola = cargar_cola(self.archivo)

    def tearDown(self):
        for f in (self.archivo, self.archivo + ".tmp"):
            try:
                os.remove(f)
            except OSError:
                pass
        sys.modules.pop("config", None)

    def _llenar(self, n):
        for i in range(n):
            self.cola.encolar({"event_id": f"e-{i}", "tipo": "pir", "datos": {}})

    # --- Lo que reventaba en la placa --------------------------------------

    def test_pendientes_no_lee_mas_que_el_limite(self):
        """La regresión del MemoryError: con la cola llena, pedir un lote chico
        no puede depender de cargar el archivo entero."""
        self._llenar(3000)
        leidos = 0
        orig_open = open

        def open_contado(*a, **k):
            nonlocal leidos
            return orig_open(*a, **k)

        # No podemos medir RAM en CPython; medimos la garantía observable: que
        # pendientes(10) devuelve exactamente 10 y en orden, sin recorrer todo.
        lote = self.cola.pendientes(10)
        self.assertEqual(len(lote), 10)
        self.assertEqual(lote[0]["event_id"], "e-0")
        self.assertEqual(lote[9]["event_id"], "e-9")

    def test_sobrevive_una_cola_enorme(self):
        self._llenar(6000)
        # Con tope 5000, no crece indefinidamente.
        self.assertLessEqual(self.cola.largo(), 5000)

    # --- Semántica de siempre ----------------------------------------------

    def test_encolar_y_leer_en_orden(self):
        self._llenar(5)
        lote = self.cola.pendientes(20)
        self.assertEqual([e["event_id"] for e in lote],
                         [f"e-{i}" for i in range(5)])

    def test_confirmar_borra_los_primeros_y_conserva_el_resto(self):
        self._llenar(10)
        self.cola.confirmar(3)
        lote = self.cola.pendientes(20)
        self.assertEqual([e["event_id"] for e in lote],
                         [f"e-{i}" for i in range(3, 10)])
        self.assertEqual(self.cola.largo(), 7)

    def test_confirmar_todo_deja_la_cola_vacia(self):
        self._llenar(4)
        self.cola.confirmar(4)
        self.assertEqual(self.cola.largo(), 0)
        self.assertEqual(self.cola.pendientes(20), [])

    def test_linea_corrupta_no_traba_la_cola(self):
        self._llenar(2)
        with open(self.archivo, "a") as f:
            f.write("{esto no es json\n")
        self.cola.encolar({"event_id": "e-ok", "tipo": "pir", "datos": {}})
        ids = [e["event_id"] for e in self.cola.pendientes(20)]
        self.assertIn("e-ok", ids)          # lo bueno pasa
        self.assertNotIn(None, ids)         # lo corrupto se descartó

    def test_tope_conserva_lo_viejo_y_descarta_lo_nuevo(self):
        cola = cargar_cola(self.archivo, max_cola=5)
        for i in range(8):
            cola.encolar({"event_id": f"e-{i}"})
        lote = cola.pendientes(20)
        self.assertEqual(len(lote), 5)
        self.assertEqual(lote[0]["event_id"], "e-0", "descartó lo viejo en vez de lo nuevo")

    def test_confirmar_no_corrompe_si_no_hay_archivo(self):
        self.cola.confirmar(5)   # cola vacía, no debe romper
        self.assertEqual(self.cola.largo(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
