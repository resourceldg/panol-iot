"""Simulador de escenarios (DISEÑO §5).

Valida la máquina de estados replayando secuencias de eventos y verificando
cómo quedan sesiones y alarmas. No necesita hardware ni Flask ni MQTT: si
esto pasa, la lógica de auditoría es correcta y lo que quede por fallar es
el transporte.

Corre contra un PostgreSQL real, no contra un doble: las garantías que más
importan (el índice único parcial de "una sesión activa por ubicación",
GREATEST sobre timestamptz) las da el motor de base, así que probarlas
contra otra cosa no probaría nada.

    PANOL_DSN=postgresql://panol:panol@localhost:15433/panol_test \
        .venv/bin/python -m unittest discover -s server/tests -t server
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import servicio
from db import repositorio
from engine import modelo as m

LAB01 = "panol-lab01"
LAB02 = "panol-lab02"
ANA = "C1:D1:3D:05"
BETO = "35:2B:AD:75"

T0 = datetime(2026, 7, 21, 9, 0, 0, tzinfo=repositorio.TZ)

# Orden inverso a las dependencias: TRUNCATE ... CASCADE se encarga igual,
# pero listarlas explícitas documenta el modelo.
TABLAS = (
    "eventos_procesados",
    "alarmas",
    "eventos_armario",
    "eventos_pir",
    "eventos_puerta",
    "eventos_acceso",
    "sesiones",
    "credenciales",
    "usuarios",
    "nodos",
    "ubicaciones",
)

_conn = None


def setUpModule():
    global _conn
    _conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
    repositorio.inicializar(_conn)


def tearDownModule():
    if _conn is not None:
        _conn.close()


def en(minutos: float) -> datetime:
    """Instante relativo al inicio de la jornada simulada."""
    return T0 + timedelta(minutes=minutos)


class BaseEscenario(unittest.TestCase):
    def setUp(self):
        self.conn = _conn
        # Cada escenario arranca con la base vacía: los tests no deben
        # depender del orden en que corren.
        self.conn.execute(
            "TRUNCATE %s RESTART IDENTITY CASCADE" % ", ".join(TABLAS)
        )
        self.cfg = m.Config(t_ausencia_s=15 * 60)

    # --- Emisores de eventos ---------------------------------------------

    def _ingerir(self, tipo, ts, ubicacion=LAB01, event_id=None, **datos):
        evento = m.Evento(
            tipo=tipo,
            ubicacion_id=ubicacion,
            ts=ts,
            event_id=event_id,
            nodo_id=f"{ubicacion}-puerta",
            datos=datos,
        )
        return servicio.ingerir(self.conn, evento, self.cfg)

    def ingresa(self, uid, ts, ubicacion=LAB01, event_id=None):
        """Acceso CON ingreso confirmado: lo único que crea sesión."""
        return self._ingerir(
            "acceso", ts, ubicacion, event_id, uid_hex=uid, resultado="CONCEDIDO"
        )

    def puerta(self, estado, ts, ubicacion=LAB01, event_id=None):
        return self._ingerir("puerta", ts, ubicacion, event_id, estado_reed=estado)

    def pir(self, ts, ubicacion=LAB01, event_id=None):
        return self._ingerir("pir", ts, ubicacion, event_id)

    def armario(self, armario_id, ts, ubicacion=LAB01, event_id=None):
        return self._ingerir("armario", ts, ubicacion, event_id, armario_id=armario_id)

    def tarea_ausencia(self, ts, reed="CERRADO", ubicacion=LAB01):
        return self._ingerir("tarea_ausencia", ts, ubicacion, reed_actual=reed)

    # --- Consultas de verificación ---------------------------------------

    def sesiones(self, ubicacion=LAB01):
        return self.conn.execute(
            "SELECT * FROM sesiones WHERE ubicacion_id = %s ORDER BY id",
            (ubicacion,),
        ).fetchall()

    def activa(self, ubicacion=LAB01):
        return repositorio.sesion_en_curso(self.conn, ubicacion)

    def eventos_pir(self):
        return self.conn.execute(
            "SELECT * FROM eventos_pir ORDER BY id"
        ).fetchall()

    def alarmas(self):
        return self.conn.execute("SELECT * FROM alarmas ORDER BY id").fetchall()

    def codigos_alarma(self):
        return [a["codigo"] for a in self.alarmas()]


class TestCicloDeVida(BaseEscenario):
    def test_1_nacimiento(self):
        """Un acceso con ingreso crea una sesión a nombre del UID."""
        self.ingresa(ANA, en(0))

        sesion = self.activa()
        self.assertIsNotNone(sesion)
        self.assertEqual(sesion.uid_hex, ANA)
        self.assertEqual(sesion.estado, m.EN_CURSO)
        self.assertEqual(self.codigos_alarma(), [])

    def test_2_persistencia(self):
        """La puerta cicla por dentro sin cerrar la sesión; la actividad avanza."""
        self.ingresa(ANA, en(0))
        sesion_id = self.activa().id

        self.puerta("CERRADO", en(1))
        self.puerta("ABIERTO", en(5))
        self.pir(en(8))

        sesion = self.activa()
        self.assertEqual(sesion.id, sesion_id, "no debe haberse creado otra sesión")
        self.assertEqual(sesion.ultima_actividad, en(8))
        self.assertEqual(self.codigos_alarma(), [])

    def test_3_relevo(self):
        """Un segundo UID cierra la sesión anterior por RELEVO y abre otra."""
        self.ingresa(ANA, en(0))
        self.ingresa(BETO, en(30))

        primera, segunda = self.sesiones()
        self.assertEqual(primera["motivo_cierre"], m.RELEVO)
        self.assertEqual(primera["estado"], m.COMPLETA)
        self.assertEqual(primera["hora_fin"], en(30))
        self.assertEqual(segunda["uid_hex"], BETO)
        self.assertEqual(segunda["estado"], m.EN_CURSO)

    def test_4_relevo_del_mismo_uid(self):
        """Sin fichaje de salida en v1: repasar el llavero renueva la sesión."""
        self.ingresa(ANA, en(0))
        self.ingresa(ANA, en(30))

        primera, segunda = self.sesiones()
        self.assertEqual(primera["motivo_cierre"], m.RELEVO)
        self.assertEqual(segunda["uid_hex"], ANA)
        self.assertNotEqual(primera["id"], segunda["id"])

    def test_5_ausencia_con_puerta_cerrada(self):
        """Sin actividad ≥ T y puerta cerrada: se cierra por AUSENCIA."""
        self.ingresa(ANA, en(0))
        self.tarea_ausencia(en(10))
        self.assertIsNotNone(self.activa(), "10 min todavía no alcanzan")

        self.tarea_ausencia(en(16))
        self.assertIsNone(self.activa())
        self.assertEqual(self.sesiones()[0]["motivo_cierre"], m.AUSENCIA)

    def test_6_puerta_abierta_sin_gente_es_one_shot(self):
        """La alarma se emite una sola vez por episodio, no una por minuto."""
        self.ingresa(ANA, en(0))
        for minuto in range(16, 25):
            self.tarea_ausencia(en(minuto), reed="ABIERTO")

        self.assertIsNotNone(self.activa(), "la sesión NO se cierra")
        self.assertEqual(self.codigos_alarma(), ["PUERTA_ABIERTA_SIN_GENTE"])

    def test_6b_el_one_shot_se_rearma_tras_actividad(self):
        """Si vuelve a haber gente, un episodio nuevo puede volver a alarmar."""
        self.ingresa(ANA, en(0))
        self.tarea_ausencia(en(16), reed="ABIERTO")
        self.pir(en(20))
        self.tarea_ausencia(en(40), reed="ABIERTO")

        self.assertEqual(
            self.codigos_alarma(),
            ["PUERTA_ABIERTA_SIN_GENTE", "PUERTA_ABIERTA_SIN_GENTE"],
        )


class TestAnomalias(BaseEscenario):
    def test_7_armario_sin_sesion(self):
        """Abrir un armario sin responsable es anomalía crítica."""
        self.armario(7, en(0))

        alarma = self.alarmas()[0]
        self.assertEqual(alarma["codigo"], "ARMARIO_SIN_SESION")
        self.assertEqual(alarma["severidad"], "critica")
        self.assertEqual(alarma["detalle"], {"armario_id": 7})

        # El evento igual se registra, con sesion_id NULL: la apertura pasó
        # y tiene que quedar en la auditoría aunque no haya a quién atribuirla.
        fila = self.conn.execute("SELECT * FROM eventos_armario").fetchone()
        self.assertIsNone(fila["sesion_id"])
        self.assertEqual(fila["armario_id"], 7)

    def test_apertura_sin_credencial(self):
        """Reed abierto sin sesión: forzada o llave física."""
        self.puerta("ABIERTO", en(0))
        self.assertEqual(self.codigos_alarma(), ["APERTURA_SIN_CREDENCIAL"])

    def test_cierre_sin_sesion_no_alarma(self):
        """Solo la apertura es anomalía. Un cierre sin sesión es inocuo."""
        self.puerta("CERRADO", en(0))
        self.assertEqual(self.codigos_alarma(), [])

    def test_8_falso_cierre(self):
        """Persona quieta: el PIR no la ve, la sesión cierra, y al moverse alarma.

        Documentado en spec §9. La auditoría debe leer una PRESENCIA_SIN_SESION
        inmediatamente posterior a un cierre por AUSENCIA como falso cierre,
        no como intrusión. El sistema se autocorrige con la próxima pasada.
        """
        self.ingresa(ANA, en(0))
        self.tarea_ausencia(en(16))
        self.assertIsNone(self.activa())

        self.pir(en(18))
        self.assertEqual(self.codigos_alarma(), ["PRESENCIA_SIN_SESION"])

        self.ingresa(ANA, en(19))
        self.assertIsNotNone(self.activa(), "la próxima lectura RFID corrige")

    def test_pir_con_sesion_es_solo_actividad_sin_registro(self):
        """CON sesión, el movimiento corre la actividad y NO deja fila propia.

        Es la naturaleza "log/actividad" del PIR (spec §7): pura señal de
        presencia. `eventos_pir` debe quedar vacía.
        """
        self.ingresa(ANA, en(0))
        actividad_previa = self.activa().ultima_actividad

        self.pir(en(5))

        self.assertEqual(self.eventos_pir(), [], "con sesión no deja registro")
        self.assertGreater(self.activa().ultima_actividad, actividad_previa)
        self.assertEqual(self.codigos_alarma(), [], "y no alarma")

    def test_pir_sin_sesion_deja_registro_y_alarma(self):
        """SIN sesión, el movimiento es anomalía: registro auditable + alarma.

        Simétrico con el armario. La huella queda en `eventos_pir` con
        sesion_id NULL, y además se dispara PRESENCIA_SIN_SESION.
        """
        self.pir(en(0))

        filas = self.eventos_pir()
        self.assertEqual(len(filas), 1, "sin sesión sí deja registro")
        self.assertIsNone(filas[0]["sesion_id"], "sin sesión = sesion_id NULL")
        self.assertEqual(self.codigos_alarma(), ["PRESENCIA_SIN_SESION"])

    def test_acceso_denegado_no_crea_sesion(self):
        self._ingerir("acceso", en(0), uid_hex="00:00:00:00", resultado="DENEGADO")
        self.assertIsNone(self.activa())
        self.assertEqual(
            self.conn.execute("SELECT resultado FROM eventos_acceso").fetchone()[
                "resultado"
            ],
            "DENEGADO",
        )

    def test_acceso_sin_ingreso_no_crea_sesion(self):
        """Se identificó pero no entró: queda en la auditoría, sin sesión."""
        self._ingerir("acceso", en(0), uid_hex=ANA, resultado="SIN_INGRESO")
        self.assertIsNone(self.activa())
        self.assertEqual(len(self.sesiones()), 0)


class TestResilencia(BaseEscenario):
    def test_9_idempotencia(self):
        """El mismo event_id dos veces produce un solo efecto.

        Con MQTT esto deja de ser un lujo: QoS 1 entrega "al menos una vez",
        así que los duplicados son parte normal del protocolo.
        """
        self.ingresa(ANA, en(0), event_id="nodo-1-1")
        efectos = self.ingresa(ANA, en(0), event_id="nodo-1-1")

        self.assertEqual(efectos, [], "el reenvío no debe producir efectos")
        self.assertEqual(len(self.sesiones()), 1, "no debe relevarse a sí misma")

    def test_9b_idempotencia_de_armario(self):
        """Sin esto, un reenvío contaría dos veces la apertura de un armario."""
        self.ingresa(ANA, en(0))
        self.armario(3, en(5), event_id="armarios-1-1")
        self.armario(3, en(5), event_id="armarios-1-1")

        total = self.conn.execute(
            "SELECT count(*) AS n FROM eventos_armario"
        ).fetchone()["n"]
        self.assertEqual(total, 1)

    def test_10_atribucion_tardia_por_timestamp(self):
        """Un evento que llega tarde se atribuye a quien era responsable entonces.

        Es el escenario que justifica la cola en flash: el nodo de armarios
        estuvo sin red, y cuando vuelve manda un evento viejo. Si se
        atribuyera a la sesión actual, la auditoría culparía a Beto de algo
        que hizo Ana.
        """
        self.ingresa(ANA, en(0))
        sesion_ana = self.activa().id
        self.ingresa(BETO, en(30))
        sesion_beto = self.activa().id

        # Llega ahora, pero ocurrió a los 10 minutos: durante el turno de Ana.
        self.armario(4, en(10))

        fila = self.conn.execute("SELECT * FROM eventos_armario").fetchone()
        self.assertEqual(fila["sesion_id"], sesion_ana)
        self.assertNotEqual(fila["sesion_id"], sesion_beto)

    def test_10b_evento_tardio_no_retrasa_la_actividad(self):
        """La marca de actividad nunca retrocede.

        Si un evento viejo pisara `ultima_actividad`, la tarea de ausencia
        cerraría una sesión que está perfectamente activa.
        """
        self.ingresa(ANA, en(0))
        self.pir(en(10))
        self.pir(en(2), event_id="tardio")

        self.assertEqual(self.activa().ultima_actividad, en(10))

    def test_11_recuperacion_sesion_vencida(self):
        """Tras un corte, una sesión con actividad vencida queda INCONSISTENTE."""
        self.ingresa(ANA, en(0))

        # Simula el apagón: la actividad quedó vieja respecto de "ahora".
        self.conn.execute(
            "UPDATE sesiones SET ultima_actividad = %s",
            (repositorio.ahora() - timedelta(hours=3),),
        )

        servicio.recuperar_al_arrancar(self.conn, self.cfg)

        self.assertEqual(self.sesiones()[0]["estado"], m.INCONSISTENTE)
        self.assertEqual(self.codigos_alarma(), ["SESION_INCONSISTENTE"])
        self.assertIsNone(self.activa())

    def test_11b_recuperacion_sesion_reciente_se_reanuda(self):
        """Un corte corto no debe perder la responsabilidad en curso."""
        self.ingresa(ANA, en(0))
        self.conn.execute(
            "UPDATE sesiones SET ultima_actividad = %s",
            (repositorio.ahora() - timedelta(minutes=2),),
        )

        servicio.recuperar_al_arrancar(self.conn, self.cfg)

        self.assertIsNotNone(self.activa())
        self.assertEqual(self.codigos_alarma(), [])


class TestMultiUbicacion(BaseEscenario):
    """Varios laboratorios y pañoles contra el mismo servidor."""

    def test_sesiones_simultaneas_en_ubicaciones_distintas(self):
        """Dos pañoles pueden tener sesión activa a la vez, sin interferir."""
        self.ingresa(ANA, en(0), ubicacion=LAB01)
        self.ingresa(BETO, en(1), ubicacion=LAB02)

        self.assertEqual(self.activa(LAB01).uid_hex, ANA)
        self.assertEqual(self.activa(LAB02).uid_hex, BETO)

    def test_un_relevo_no_toca_la_otra_ubicacion(self):
        self.ingresa(ANA, en(0), ubicacion=LAB01)
        self.ingresa(ANA, en(1), ubicacion=LAB02)
        self.ingresa(BETO, en(30), ubicacion=LAB01)

        self.assertEqual(self.activa(LAB01).uid_hex, BETO)
        self.assertEqual(self.activa(LAB02).uid_hex, ANA, "LAB02 no se toca")

    def test_anomalia_en_una_ubicacion_no_alarma_por_la_otra(self):
        """Con sesión en LAB01, un armario abierto en LAB02 sigue siendo anomalía."""
        self.ingresa(ANA, en(0), ubicacion=LAB01)
        self.armario(2, en(5), ubicacion=LAB02)

        alarma = self.alarmas()[0]
        self.assertEqual(alarma["codigo"], "ARMARIO_SIN_SESION")
        self.assertEqual(alarma["ubicacion_id"], LAB02)

    def test_la_base_impide_dos_sesiones_activas_en_la_misma_ubicacion(self):
        """La garantía no depende del código: la impone el índice parcial.

        Con la API HTTP y el puente MQTT escribiendo en paralelo, dos
        procesos podrían leer ambos "no hay sesión" y crear dos. El índice
        hace que la segunda inserción falle en vez de corromper la
        auditoría en silencio.
        """
        self.ingresa(ANA, en(0))

        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO sesiones (ubicacion_id, uid_hex, hora_inicio,"
                " ultima_actividad, estado) VALUES (%s, %s, %s, %s, 'EN_CURSO')",
                (LAB01, BETO, en(1), en(1)),
            )


class TestPuertaAbiertaProlongada(BaseEscenario):
    """Puerta abierta mucho tiempo, con o sin gente (independiente de ausencia)."""

    def abrir_puerta(self, ts, ubicacion=LAB01):
        self.puerta("ABIERTO", ts, ubicacion)

    def test_alarma_tras_el_umbral_aunque_haya_actividad(self):
        """Con sesión y movimiento, pero puerta abierta > umbral → alarma.

        Es el caso que PUERTA_ABIERTA_SIN_GENTE no cubre: hay gente, pero la
        puerta quedó abierta (trabada u olvidada).
        """
        self.ingresa(ANA, en(0))
        self.abrir_puerta(en(1))
        self.pir(en(4))  # hay actividad: la tarea de ausencia no dispararía

        # A los 3 min de abierta (umbral 5) todavía no.
        self._tiempo_congelado(en(3))
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        self.assertEqual(self.codigos_alarma(), [])

        # A los 7 min de abierta, sí.
        self._tiempo_congelado(en(8))
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        self.assertIn("PUERTA_ABIERTA_PROLONGADA", self.codigos_alarma())

    def test_one_shot_no_repite_hasta_cerrar(self):
        self.ingresa(ANA, en(0))
        self.abrir_puerta(en(1))
        self._tiempo_congelado(en(10))
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        prolongadas = [c for c in self.codigos_alarma()
                       if c == "PUERTA_ABIERTA_PROLONGADA"]
        self.assertEqual(len(prolongadas), 1, "una sola por episodio")

    def test_se_rearma_tras_cerrar_y_reabrir(self):
        self.ingresa(ANA, en(0))
        self.abrir_puerta(en(1))
        self._tiempo_congelado(en(10))
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        self.puerta("CERRADO", en(11))
        self.abrir_puerta(en(12))
        self._tiempo_congelado(en(20))
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        prolongadas = [c for c in self.codigos_alarma()
                       if c == "PUERTA_ABIERTA_PROLONGADA"]
        self.assertEqual(len(prolongadas), 2, "nuevo episodio, nueva alarma")

    def test_puerta_cerrada_no_alarma(self):
        self.ingresa(ANA, en(0))
        self.puerta("CERRADO", en(1))
        self._tiempo_congelado(en(30))
        servicio.verificar_puertas_abiertas(self.conn, self.cfg)
        self.assertEqual(self.codigos_alarma(), [])

    def _tiempo_congelado(self, momento):
        """La tarea usa repositorio.ahora(); se lo fija al instante deseado."""
        self._orig_ahora = repositorio.ahora
        repositorio.ahora = lambda: momento
        self.addCleanup(setattr, repositorio, "ahora", self._orig_ahora)


class TestModoDegradado(BaseEscenario):
    def hb(self, degradado, nodo="panol-lab01-puerta"):
        return repositorio.registrar_heartbeat(
            self.conn, nodo_id=nodo, ubicacion_id=LAB01, rol="puerta",
            modo_degradado=degradado,
        )

    def test_entrar_en_degradado_devuelve_transicion(self):
        self.assertTrue(self.hb(True), "primer degradado = transición")
        self.assertFalse(self.hb(True), "seguir degradado NO es transición")

    def test_recuperar_y_volver_es_nueva_transicion(self):
        self.assertTrue(self.hb(True))
        self.assertFalse(self.hb(False), "recuperarse no es entrar")
        self.assertTrue(self.hb(True), "volver a degradado = nueva transición")


class TestCierreDeJornada(BaseEscenario):
    def test_fin_de_jornada_cierra_la_sesion(self):
        self.ingresa(ANA, en(0))
        self._ingerir("tarea_fin_jornada", en(13 * 60))

        self.assertIsNone(self.activa())
        self.assertEqual(self.sesiones()[0]["motivo_cierre"], m.CIERRE_SISTEMA)

    def test_fin_de_jornada_sin_sesion_no_hace_nada(self):
        efectos = self._ingerir("tarea_fin_jornada", en(13 * 60))
        self.assertEqual(efectos, [])


class TestContratoDelMotor(BaseEscenario):
    def test_tipo_de_evento_desconocido_falla_ruidosamente(self):
        """Mejor romper que ignorar en silencio un evento mal formado."""
        with self.assertRaises(ValueError):
            self._ingerir("tipo_inventado", en(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
