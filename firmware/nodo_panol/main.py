# Nodo panol (ESP32 #1) — control de la puerta.
#
# Maquina de estados LOCAL y volatil. No maneja sesiones: eso es del
# servidor. Si el nodo se apaga, esta FSM se pierde y no importa, porque al
# bootear nace en IDLE (estado seguro) y vuelve a reportar.
#
#     IDLE --tarjeta--> VALIDANDO --autorizado--> PULSO --800ms--> PROMESA
#       ^                   |                                        |
#       |              no autorizado                  reed abre (CON INGRESO)
#       |              (DENEGADO)                     o 10 s  (SIN INGRESO)
#       +-------------------+----------------------------------------+
#
# El reed y el PIR se reportan SIEMPRE, en cualquier estado, como eventos
# sueltos. El nodo no los interpreta.

from machine import Pin, WDT
from mfrc522 import MFRC522
from time import sleep_ms, ticks_ms, ticks_diff
import gc

import config
import cola
import red
import reloj

# --- Estados de la FSM ----------------------------------------------------
IDLE = "IDLE"            # Reposo. Unico estado que lee tarjetas.
VALIDANDO = "VALIDANDO"  # Se decide contra la whitelist local.
PULSO = "PULSO"          # Rele energizado, 800 ms.
PROMESA = "PROMESA"      # Se espera el flanco del reed hasta 10 s.


def log(tag, *args):
    """Salida de consola uniforme, con la hora al frente.

    Si el nodo esta en hora (NTP), muestra hora argentina HH:MM:SS. En banco
    sin red no hay reloj real, asi que cae al uptime en ms, que igual sirve
    para medir la secuencia (cuanto tardo la promesa, el antirrebote, etc.).
    """
    hora = reloj.hora_local()
    marca = hora if hora is not None else "{:>8}ms".format(ticks_ms())
    print("[{:>10}] [{}]".format(marca, tag), *args)


# --- Identidad de arranque ------------------------------------------------
def _siguiente_boot_id():
    """Contador que se incrementa en cada arranque.

    Sirve para armar event_id unicos SIN reloj real: el nodo puede bootear
    sin NTP, y dos eventos de arranques distintos no deben colisionar.
    """
    n = 0
    try:
        with open(config.ARCHIVO_BOOT) as f:
            n = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pass
    n += 1
    try:
        with open(config.ARCHIVO_BOOT, "w") as f:
            f.write(str(n))
    except OSError as e:
        log("BOOT", "no se pudo persistir el boot_id:", e)
    return n


BOOT_ID = _siguiente_boot_id()
_secuencia = 0


def emitir(tipo, **datos):
    """Registra un evento: lo imprime y lo deja en la cola persistente.

    El event_id viaja con el evento para que el servidor pueda descartar
    duplicados. Sin el, un reenvio tras un timeout podria crear dos sesiones
    o contar dos veces la apertura de un armario.
    """
    global _secuencia
    _secuencia += 1
    evento = {
        "event_id": "{}-{}-{}".format(config.NODO_ID, BOOT_ID, _secuencia),
        "ubicacion_id": config.UBICACION_ID,
        "nodo_id": config.NODO_ID,
        "tipo": tipo,
        # None si el nodo no esta en hora: el servidor pone la de llegada.
        # El uptime viaja igual, para poder ordenar los eventos de un mismo
        # arranque aunque no haya reloj.
        "timestamp": reloj.ahora_iso(),
        "uptime_ms": ticks_ms(),
        "datos": datos,
    }
    log("EVENTO", tipo, datos)
    cola.encolar(evento)


# --- Rele -----------------------------------------------------------------
# Se deja en reposo ANTES que nada: es la primera instruccion que toca
# hardware de potencia. El pull-up externo cubre el hueco previo, mientras
# el pin todavia flota durante el reset.
_REPOSO = 1 - config.RELE_ACTIVO_EN
rele = Pin(config.PIN_RELE, Pin.OUT)
rele.value(_REPOSO)


def rele_activar():
    rele.value(config.RELE_ACTIVO_EN)


def rele_soltar():
    rele.value(_REPOSO)


# --- LEDs y boton ---------------------------------------------------------
led_acceso = Pin(config.PIN_LED_ACCESO, Pin.OUT)
led_acceso.value(0)
led_puerta = Pin(config.PIN_LED_PUERTA, Pin.OUT)
led_puerta.value(0)
boton_grabar = Pin(config.PIN_BOTON_GRABAR, Pin.IN, Pin.PULL_DOWN)

# El boton de grabado tiene prioridad sobre abrir la puerta, asi que uno
# trabado en "apretado" convierte el nodo en un dador de altas que NUNCA
# abre: cada tarjeta se daria de alta en silencio en vez de dar acceso.
# Es un modo de falla feo porque no se ve hasta que alguien queda afuera.
#
# En reposo el pin tiene que leer 0 (PULL_DOWN). Si al bootear lee 1, se
# asume trabado o mal cableado y se desactiva la funcion de grabado: es
# preferible perder el alta de tarjetas antes que perder la apertura.
BOTON_CONFIABLE = boton_grabar.value() == 0

# --- Sensores -------------------------------------------------------------
# Cada uno se instancia solo si config.SENSORES lo declara cableado.
reed = None
if config.SENSORES["reed"]:
    #   CERRADA -> iman cerca -> contacto cerrado -> lee 0
    #   ABIERTA -> iman lejos -> contacto abierto -> lee 1
    reed = Pin(config.PIN_REED, Pin.IN, Pin.PULL_UP)

pir = None
if config.SENSORES["pir"]:
    # PIR DSC LC-100-PI: salida por contacto seco N.C., no un HC-SR501.
    #   reposo (sin movimiento) -> contacto cerrado -> pin a GND -> 0
    #   deteccion               -> contacto abre    -> pull-up    -> 1
    # El PULL_UP es obligatorio: sin el, al abrir el contacto la linea
    # flota y el 1 no es firme. Verificado con debug_pir.py: pulsos limpios
    # de ~1.7 s (tiempo de retencion del rele), sin rebotes.
    pir = Pin(config.PIN_PIR, Pin.IN, Pin.PULL_UP)

lector = None
if config.SENSORES["rfid"]:
    lector = MFRC522(**config.PIN_RC522)


def _verificar_rc522():
    """Reintenta 20 s para poder acomodar los jumpers sin resetear la placa."""
    ver = None
    for _ in range(40):
        ver = lector._rreg(0x37)
        if ver in (0x91, 0x92):
            log("RC522", "OK (reg 0x37 =", hex(ver), ")")
            return True
        sleep_ms(500)
    log("RC522", "NO RESPONDE (reg 0x37 =", hex(ver) if ver is not None else "?",
        ") revisar VCC=3.3V, GND comun y el cableado SPI")
    return False


# --- Whitelist local ------------------------------------------------------
# El nodo decide SIEMPRE contra esta lista y despues reporta. El servidor
# nunca esta en el camino critico de abrir la puerta, asi que un corte de
# red no demora un acceso legitimo.
UIDS_BASE = {"DE:AD:BE:EF"}


def _leer_uids(ruta, uids):
    try:
        with open(ruta) as f:
            for linea in f:
                linea = linea.strip()
                if linea:
                    uids.add(linea)
    except OSError:
        pass  # Todavia no existe: es normal en el primer arranque.
    return uids


def cargar_uids():
    """Union de tres fuentes, en orden de menor a mayor volatilidad.

    La whitelist del servidor se refresca cada 15 min y vive en su propio
    archivo. Las tarjetas grabadas con el boton viven en uids.txt y NO se
    pisan al refrescar: son altas hechas en el pañol, que el servidor
    todavia no conoce.
    """
    uids = set(UIDS_BASE)
    _leer_uids(config.ARCHIVO_WHITELIST, uids)
    _leer_uids(config.ARCHIVO_UIDS, uids)
    return uids


def recargar_uids():
    global UIDS_AUTORIZADOS
    UIDS_AUTORIZADOS = cargar_uids()
    log("UIDS", len(UIDS_AUTORIZADOS), "autorizados")


def grabar_uid(uid):
    UIDS_AUTORIZADOS.add(uid)
    try:
        with open(config.ARCHIVO_UIDS, "a") as f:
            f.write(uid + "\n")
    except OSError as e:
        log("UIDS", "no se pudo persistir:", e)


UIDS_AUTORIZADOS = cargar_uids()

# --- Estado del muestreo de sensores --------------------------------------
_reed_estable = None       # Ultimo valor ya confirmado por el antirrebote
_reed_crudo = None         # Ultimo valor leido, aun sin confirmar
_reed_desde = 0            # Cuando empezo a leerse _reed_crudo
_apertura_ms = None        # ticks del ultimo flanco de apertura confirmado

_pir_ultimo_ms = None      # Ultimo movimiento reportado (para el throttle)

_ultimo_uid = None
_ultimo_uid_ms = 0


def poll_reed(ahora):
    """Reporta cambios estables del reed. Corre en cualquier estado.

    El antirrebote evita que un golpe o la vibracion del cierre generen una
    rafaga de eventos: hay que ver el mismo valor durante 300 ms seguidos.
    """
    global _reed_estable, _reed_crudo, _reed_desde, _apertura_ms
    if reed is None:
        return

    valor = reed.value()
    if valor != _reed_crudo:
        _reed_crudo = valor
        _reed_desde = ahora
        return

    if valor == _reed_estable:
        return
    if ticks_diff(ahora, _reed_desde) < config.T_ANTIRREBOTE_REED_MS:
        return

    _reed_estable = valor
    abierta = valor == 1
    led_puerta.value(1 if abierta else 0)
    if abierta:
        _apertura_ms = ahora
    log("REED", "ABIERTA" if abierta else "CERRADA")
    emitir("puerta", estado_reed="ABIERTO" if abierta else "CERRADO")


def poll_pir(ahora):
    """Reporta movimiento con throttle de 30 s.

    El HC-SR501 va en modo retrigger (jumper H) con el delay al minimo: la
    temporizacion real la decide el servidor, el nodo solo avisa que hubo
    movimiento sin inundar la red.
    """
    global _pir_ultimo_ms
    if pir is None or pir.value() != 1:
        return
    if (_pir_ultimo_ms is not None
            and ticks_diff(ahora, _pir_ultimo_ms) < config.THROTTLE_PIR_MS):
        return
    _pir_ultimo_ms = ahora
    log("PIR", "movimiento")
    emitir("pir")


def leer_tarjeta():
    """Devuelve el UID leido, o None. Filtra la misma tarjeta repetida."""
    global _ultimo_uid, _ultimo_uid_ms
    if lector is None:
        return None
    try:
        (estatus, _tipo) = lector.request(lector.REQIDL)
        if estatus != lector.OK:
            _ultimo_uid = None
            return None
        (estatus, raw) = lector.anticoll()
        if estatus != lector.OK:
            return None
        uid = "{:02X}:{:02X}:{:02X}:{:02X}".format(*raw[:4])
        ahora = ticks_ms()
        if (uid == _ultimo_uid
                and ticks_diff(ahora, _ultimo_uid_ms) <= config.T_MISMA_TARJETA_MS):
            return None
        _ultimo_uid = uid
        _ultimo_uid_ms = ahora
        return uid
    except Exception as e:
        # Nunca "except:" a secas: eso taparia tambien Ctrl+C.
        log("RC522", "error de lectura, reiniciando lector:", e)
        try:
            lector.init()
        except Exception:
            pass
        _ultimo_uid = None
        return None


# --- Maquina de estados ---------------------------------------------------
estado = IDLE
_estado_desde = 0
_uid_en_curso = None
_promesa_desde = 0   # Inicio del pulso: origen de la ventana de 10 s


def ir_a(nuevo, ahora):
    global estado, _estado_desde
    log("FSM", "{} -> {}".format(estado, nuevo))
    estado = nuevo
    _estado_desde = ahora


def paso_fsm(ahora):
    global _uid_en_curso, _promesa_desde

    if estado == IDLE:
        uid = leer_tarjeta()
        if uid is None:
            return
        log("RFID", "tarjeta", uid)
        # El boton de grabado tiene prioridad sobre abrir: mantenerlo
        # apretado y acercar una tarjeta la da de alta. Es como se puebla
        # la whitelist sin depender del servidor.
        if BOTON_CONFIABLE and boton_grabar.value() == 1:
            if uid in UIDS_AUTORIZADOS:
                log("RFID", "ya estaba autorizada")
            else:
                grabar_uid(uid)
                log("RFID", "tarjeta GRABADA y autorizada")
            return
        _uid_en_curso = uid
        ir_a(VALIDANDO, ahora)

    elif estado == VALIDANDO:
        # Decision local e inmediata contra la whitelist en flash.
        if _uid_en_curso in UIDS_AUTORIZADOS:
            log("ACCESO", "PERMITIDO", _uid_en_curso)
            led_acceso.value(1)
            rele_activar()
            # La ventana de la promesa arranca con el pulso, no cuando
            # termina: si alguien esta esperando del otro lado, la puerta
            # puede abrirse durante los 800 ms y ese flanco tambien vale.
            _promesa_desde = ahora
            if not config.SENSORES["reed"]:
                log("AVISO", "sin reed cableado no se puede confirmar el "
                             "ingreso: la promesa va a expirar siempre")
            ir_a(PULSO, ahora)
        else:
            log("ACCESO", "DENEGADO", _uid_en_curso)
            emitir("acceso", uid_hex=_uid_en_curso, resultado="DENEGADO",
                   modo_degradado=red.degradado())
            _uid_en_curso = None
            ir_a(IDLE, ahora)

    elif estado == PULSO:
        # Pulso NO bloqueante. Con un sleep() aca el reed quedaria ciego
        # justo en la ventana en la que hay que vigilarlo.
        if ticks_diff(ahora, _estado_desde) >= config.T_PULSO_SOLENOIDE_MS:
            rele_soltar()
            log("RELE", "pulso terminado, esperando apertura")
            ir_a(PROMESA, ahora)

    elif estado == PROMESA:
        # La "promesa": el acceso recien crea sesion si la puerta se abre
        # de verdad. Solo cuenta un flanco POSTERIOR al inicio del pulso.
        if _apertura_ms is not None and ticks_diff(_apertura_ms, _promesa_desde) >= 0:
            log("ACCESO", "ingreso CONFIRMADO por el reed")
            emitir("acceso", uid_hex=_uid_en_curso, resultado="CONCEDIDO",
                   modo_degradado=red.degradado())
            led_acceso.value(0)
            _uid_en_curso = None
            ir_a(IDLE, ahora)
        elif ticks_diff(ahora, _promesa_desde) >= config.T_APERTURA_MS:
            log("ACCESO", "timeout: concedido pero SIN ingreso")
            emitir("acceso", uid_hex=_uid_en_curso, resultado="SIN_INGRESO",
                   modo_degradado=red.degradado())
            led_acceso.value(0)
            _uid_en_curso = None
            ir_a(IDLE, ahora)


# --- Arranque -------------------------------------------------------------
log("BOOT", "nodo", config.NODO_ID, "| ubicacion", config.UBICACION_ID,
    "| boot_id", BOOT_ID)
log("BOOT", "sensores cableados:",
    ",".join(k for k, v in config.SENSORES.items() if v) or "ninguno")
log("BOOT", "rele en reposo (activo en {})".format(config.RELE_ACTIVO_EN))
if not BOTON_CONFIABLE:
    log("AVISO", "GPIO{} leyo 1 al arrancar: boton de grabado DESACTIVADO."
                 " Revisar cableado (reposo debe ser 0)."
        .format(config.PIN_BOTON_GRABAR))

_pendientes = cola.largo()
if _pendientes:
    log("COLA", _pendientes, "eventos pendientes de un arranque anterior")

if lector is not None:
    _verificar_rc522()

# Red y hora. Se hace ANTES de reportar el estado inicial para que ese
# primer evento salga ya con timestamp real. Que falle no impide arrancar:
# la puerta funciona igual y todo queda en cola.log.
#
# gc.collect() antes de levantar el WiFi: el driver necesita un bloque de
# RAM contiguo y, tras leer la cola y la whitelist, la memoria queda
# fragmentada. Sin esto, con la cola grande, falla con "WiFi Out of Memory".
gc.collect()
if red.conectar():
    reloj.sincronizar()
    recargar_uids()

# Estado inicial de la puerta, para que el servidor sincronice sin tener
# que recordar nada de antes del corte.
if reed is not None:
    _reed_estable = reed.value()
    _reed_crudo = _reed_estable
    _abierta_inicial = _reed_estable == 1
    led_puerta.value(1 if _abierta_inicial else 0)
    emitir("puerta",
           estado_reed="ABIERTO" if _abierta_inicial else "CERRADO",
           inicial=True)

wdt = WDT(timeout=config.T_WDT_MS) if config.USAR_WDT else None
log("BOOT", "listo. Acerque una tarjeta.")

while True:
    ahora = ticks_ms()
    if wdt is not None:
        wdt.feed()

    poll_reed(ahora)
    poll_pir(ahora)
    paso_fsm(ahora)

    # La red se atiende SOLO en reposo. Un POST puede tardar hasta 3 s, y
    # ese timeout no puede caer en medio del pulso del solenoide ni de la
    # ventana de la promesa: ahi el nodo tiene que estar mirando el reed.
    if estado == IDLE:
        red.tareas(ahora, recargar_uids)

    gc.collect()
    sleep_ms(config.T_CICLO_MS)
