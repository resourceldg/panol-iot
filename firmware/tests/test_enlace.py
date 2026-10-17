"""Pruebas del enlace MQTT del nodo (enlace.py), sin hardware ni MicroPython.

    python -m unittest discover -s firmware/tests -t .

Un broker simulado entiende MQTT de verdad (parsea CONNECT, PUBLISH, PINGREQ
y contesta CONNACK, PUBACK, PINGRESP). Lo que se cuida es lo que en la placa
cuesta ver y lo que no se puede romper en un sistema de auditoría:

- ningún evento se borra de la cola en flash sin el acuse del broker;
- se confirma en orden (FIFO) aunque los acuses lleguen salteados;
- si se corta, se reconecta con espera creciente y se reenvía lo no acusado;
- el keepalive mantiene viva la conexión y detecta un broker mudo;
- publicar no bloquea el bucle de la puerta.
"""
import json
import struct
import sys
import types
import unittest

RUTA = "firmware/nodo_panol/enlace.py"
PERIODO = 1 << 30


class Ticks:
    def __init__(self):
        self.ms = 0

    def ticks_ms(self):
        return self.ms % PERIODO

    @staticmethod
    def ticks_add(a, b):
        return (a + b) % PERIODO

    @staticmethod
    def ticks_diff(a, b):
        d = (a - b) % PERIODO
        return d - PERIODO if d >= PERIODO // 2 else d


class Broker:
    """Del otro lado del socket TLS. Guarda lo publicado y contesta."""

    def __init__(self):
        self.rc_connack = 0
        self.auto_ack = True
        self.publicados = []      # (topic, payload, qos, pid)
        self.conexiones = 0
        self.pings = 0
        self.mudo = False         # no contesta nada (broker colgado)
        self.cerrar = False       # cierra la conexión
        self.salida = b""         # lo que el cliente va a leer
        self._entrada = b""

    def recibir(self, datos):
        self._entrada += datos
        while len(self._entrada) >= 2:
            n, mult, i = 0, 1, 1
            while True:
                d = self._entrada[i]
                n += (d & 0x7F) * mult
                mult *= 128
                i += 1
                if not d & 0x80:
                    break
            if len(self._entrada) < i + n:
                return
            cab, cuerpo = self._entrada[0], self._entrada[i:i + n]
            self._entrada = self._entrada[i + n:]
            self._atender(cab, cuerpo)

    def _atender(self, cab, cuerpo):
        tipo = cab >> 4
        if tipo == 1:                                   # CONNECT
            self.conexiones += 1
            if not self.mudo:                           # colgado: ni CONNACK
                self.salida += bytes([0x20, 2, 0, self.rc_connack])
        elif tipo == 3:                                 # PUBLISH
            qos = (cab >> 1) & 3
            lt = struct.unpack("!H", cuerpo[:2])[0]
            topic = cuerpo[2:2 + lt].decode()
            resto = cuerpo[2 + lt:]
            pid = 0
            if qos:
                pid = struct.unpack("!H", resto[:2])[0]
                resto = resto[2:]
            self.publicados.append((topic, json.loads(resto), qos, pid))
            if qos and self.auto_ack and not self.mudo:
                self.salida += b"\x40\x02" + struct.pack("!H", pid)
        elif tipo == 12:                                # PINGREQ
            self.pings += 1
            if not self.mudo:
                self.salida += b"\xd0\x00"

    def acusar(self, pid):
        self.salida += b"\x40\x02" + struct.pack("!H", pid)


class Crudo:
    def __init__(self, entorno):
        self.e = entorno
        self.bloqueante = True

    def settimeout(self, t):
        self.bloqueante = t is None or t > 0

    def setblocking(self, b):
        self.bloqueante = b

    def connect(self, direccion):
        if self.e.sin_red:
            raise OSError(113)

    def close(self):
        pass


class Tls:
    def __init__(self, crudo, broker):
        self.crudo = crudo
        self.b = broker
        self.cerrado = False

    def write(self, datos):
        self.b.recibir(datos)
        return len(datos)

    def read(self, n):
        if self.b.cerrar:
            return b""
        if not self.b.salida:
            if self.crudo.bloqueante:
                raise OSError(110)                      # timeout
            return None                                 # no bloqueante: nada
        datos, self.b.salida = self.b.salida[:n], self.b.salida[n:]
        return datos

    def close(self):
        self.cerrado = True


def cargar(**ajustes):
    """Carga enlace.py con dobles de socket, ssl, config, cola y reloj."""
    ticks = Ticks()
    broker = Broker()
    entorno = types.SimpleNamespace(sin_red=False, hora=True, cola=[], confirmados=[],
                                    solo_ipv6=False)

    config = types.SimpleNamespace(
        MQTT_HOST="panol.ejemplo.ts.net", MQTT_PORT=443, MQTT_USER="nodo-panol-puerta",
        MQTT_PASS="secreta", MQTT_KEEPALIVE_S=60, MQTT_VERIFICAR_TLS=True,
        ARCHIVO_CA="/no/existe.pem", T_CONEXION_MQTT_S=10, T_ESCRITURA_MQTT_S=2,
        T_REINTENTO_MQTT_MAX_MS=30_000, VENTANA_MQTT=10, T_HEARTBEAT_MS=60_000,
        NODO_ID="panol-lab01-puerta", UBICACION_ID="panol-lab01")
    for k, v in ajustes.items():
        setattr(config, k, v)

    def pendientes(n):
        return [dict(e) for e in entorno.cola[:n]]

    def confirmar(n):
        entorno.confirmados.extend(e["event_id"] for e in entorno.cola[:n])
        del entorno.cola[:n]

    cola = types.SimpleNamespace(pendientes=pendientes, confirmar=confirmar,
                                 largo=lambda: len(entorno.cola))
    reloj = types.SimpleNamespace(sincronizado=lambda: entorno.hora)
    def getaddrinfo(h, p, familia=0):
        # Como el DNS real del router: puede venir primero (o solo) IPv6,
        # que MicroPython en el ESP32 entrega con la dirección en None.
        res = [(10, 1, 0, h, None)]
        if not entorno.solo_ipv6:
            res.append((2, 1, 0, h, ("1.2.3.4", p)))
        return [r for r in res if not familia or r[0] == familia]
    socket = types.SimpleNamespace(getaddrinfo=getaddrinfo, AF_INET=2,
                                   socket=lambda: Crudo(entorno))
    ssl = types.SimpleNamespace(wrap_socket=lambda crudo, server_hostname=None: Tls(crudo, broker))

    sys.modules.update({"config": config, "cola": cola, "reloj": reloj, "socket": socket,
                        "ssl": ssl, "gc": types.SimpleNamespace(collect=lambda: None)})
    with open(RUTA) as f:
        fuente = f.read().replace("from time import ticks_ms, ticks_diff, ticks_add", "")
    ns = {"ticks_ms": ticks.ticks_ms, "ticks_add": ticks.ticks_add,
          "ticks_diff": ticks.ticks_diff, "__name__": "enlace"}
    exec(compile(fuente, RUTA, "exec"), ns)
    m = types.SimpleNamespace(**{k: ns[k] for k in ("tarea", "conectado", "cerrar", "caido_hace_ms",
                                                     "toca_intentar", "estado")})
    m.ns = ns
    return m, broker, entorno, ticks


def evento(i, tipo="pir"):
    return {"event_id": "panol-lab01-puerta-7-%d" % i, "tipo": tipo, "datos": {}}


class PruebaEnlace(unittest.TestCase):
    def correr(self, m, ticks, segundos, paso_ms=50):
        for _ in range(int(segundos * 1000 / paso_ms)):
            ticks.ms += paso_ms
            m.tarea(ticks.ticks_ms(), -60, False)

    def test_publica_al_instante_y_confirma_solo_con_acuse(self):
        m, b, e, t = cargar()
        e.cola = [evento(1)]
        self.correr(m, t, 0.1)
        self.assertTrue(m.conectado())
        topics = [p[0] for p in b.publicados if p[2] == 1]
        self.assertEqual(topics, ["panol/panol-lab01/panol-lab01-puerta/evento/pir"])
        self.assertEqual(e.confirmados, ["panol-lab01-puerta-7-1"])
        self.assertEqual(e.cola, [])

    def test_sin_acuse_no_se_borra_nada_de_la_flash(self):
        m, b, e, t = cargar()
        b.auto_ack = False
        e.cola = [evento(1), evento(2)]
        self.correr(m, t, 5)
        self.assertEqual(e.confirmados, [])
        self.assertEqual(len(e.cola), 2)
        # Mientras el lote está en vuelo no se republica en cada vuelta.
        self.assertEqual(len([p for p in b.publicados if p[2] == 1]), 2)

    def test_acuses_salteados_confirman_en_orden(self):
        m, b, e, t = cargar()
        b.auto_ack = False
        e.cola = [evento(1), evento(2), evento(3)]
        self.correr(m, t, 0.2)
        pids = [p[3] for p in b.publicados if p[2] == 1]
        b.acusar(pids[1])                               # llega el 2 antes que el 1
        self.correr(m, t, 0.2)
        self.assertEqual(e.confirmados, [])             # el 1 sigue sin acuse
        b.acusar(pids[0])
        self.correr(m, t, 0.2)
        self.assertEqual(e.confirmados, ["panol-lab01-puerta-7-1", "panol-lab01-puerta-7-2"])
        b.acusar(pids[2])
        self.correr(m, t, 0.2)
        self.assertEqual(len(e.confirmados), 3)

    def test_corte_reconecta_con_backoff_y_reenvia_lo_no_acusado(self):
        m, b, e, t = cargar()
        b.auto_ack = False
        e.cola = [evento(1)]
        self.correr(m, t, 0.2)
        b.cerrar = True                                 # el broker corta
        self.correr(m, t, 0.2)
        self.assertFalse(m.conectado())
        self.assertGreater(m.caido_hace_ms(t.ticks_ms()), 0)
        b.cerrar = False
        b.auto_ack = True
        self.correr(m, t, 3)
        self.assertTrue(m.conectado())
        self.assertEqual(b.conexiones, 2)
        self.assertEqual(e.confirmados, ["panol-lab01-puerta-7-1"])
        ids = [p[1]["event_id"] for p in b.publicados if p[2] == 1]
        self.assertEqual(ids, ["panol-lab01-puerta-7-1"] * 2)  # reenviado: dedup en el server

    def test_sin_red_espera_creciente_y_no_martilla(self):
        m, b, e, t = cargar()
        e.sin_red = True
        intentos = []
        original = sys.modules["socket"].socket

        def contar():
            intentos.append(t.ms)
            return original()
        m.ns["socket"].socket = contar
        self.correr(m, t, 40)
        esperas = [b2 - a for a, b2 in zip(intentos, intentos[1:])]
        self.assertGreaterEqual(len(intentos), 4)
        self.assertLessEqual(len(intentos), 9)          # 1+2+4+8+16+30... no cada 50 ms
        self.assertEqual(esperas[:4], sorted(esperas[:4]))

    def test_keepalive_y_broker_mudo(self):
        m, b, e, t = cargar()
        self.correr(m, t, 0.1)
        self.correr(m, t, 31)
        self.assertGreaterEqual(b.pings, 1)
        self.assertTrue(m.conectado())
        b.mudo = True
        self.correr(m, t, 95)
        self.assertFalse(m.conectado())                 # 90 s sin respuesta: se cae

    def test_credenciales_rechazadas(self):
        m, b, e, t = cargar()
        b.rc_connack = 5
        e.cola = [evento(1)]
        self.correr(m, t, 2)
        self.assertFalse(m.conectado())
        self.assertEqual([p for p in b.publicados if p[2] == 1], [])
        self.assertIn("rc=5", m.estado()["error"])

    def test_latido_cada_minuto_qos0(self):
        m, b, e, t = cargar()
        self.correr(m, t, 125)
        latidos = [p for p in b.publicados if p[0].endswith("/heartbeat")]
        self.assertEqual(len(latidos), 3)
        self.assertTrue(all(p[2] == 0 for p in latidos))
        self.assertEqual(latidos[0][1]["nodo_id"], "panol-lab01-puerta")

    def test_sin_hora_no_conecta_si_hay_que_verificar(self):
        m, b, e, t = cargar()
        e.hora = False
        self.correr(m, t, 3)
        self.assertEqual(b.conexiones, 0)
        e.hora = True
        self.correr(m, t, 0.2)
        self.assertEqual(b.conexiones, 1)

    def test_usa_ipv4_aunque_el_dns_devuelva_ipv6_primero(self):
        m, b, e, t = cargar()
        self.correr(m, t, 0.1)
        self.assertTrue(m.conectado())

    def test_solo_ipv6_no_conecta_y_lo_dice(self):
        m, b, e, t = cargar()
        e.solo_ipv6 = True
        self.correr(m, t, 1)
        self.assertFalse(m.conectado())
        self.assertIn("IPv4", m.estado()["error"])

    def test_lote_respeta_la_ventana(self):
        m, b, e, t = cargar(VENTANA_MQTT=3)
        b.auto_ack = False
        e.cola = [evento(i) for i in range(10)]
        self.correr(m, t, 0.5)
        self.assertEqual(len([p for p in b.publicados if p[2] == 1]), 3)


if __name__ == "__main__":
    unittest.main()
