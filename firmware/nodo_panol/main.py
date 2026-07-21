from machine import Pin
from mfrc522 import MFRC522
from time import sleep, sleep_ms, ticks_ms, ticks_diff
import gc

# --- Lector RFID RC522 (SPI) ---
# sck=18, mosi=23, miso=19, rst=4, cs(SDA)=5
lector = MFRC522(sck=18, mosi=23, miso=19, rst=4, cs=5)

def rc522_ok():
    return lector._rreg(0x37) in (0x91, 0x92)


# Reintenta cada 500ms durante 20s para poder mover jumpers en vivo sin
# tener que resetear la placa cada vez. Sigue igual si nunca conecta.
print("Verificando RC522 (moves los jumpers si hace falta, hasta 20s)...")
_conectado = False
for _ in range(40):
    _ver = lector._rreg(0x37)
    if _ver in (0x91, 0x92):
        print("--- RC522 OK (reg 0x37 =", hex(_ver), ") ---")
        _conectado = True
        break
    sleep_ms(500)

if not _conectado:
    print("[!] RC522 no responde (reg 0x37 =", hex(_ver), "). Revisar VCC=3.3V, GND comun, SDA=5, SCK=18, MOSI=23, MISO=19, RST=4")

# --- Porton / LED ---
PIN_PORTERO = 26
SEGUNDOS_ABIERTO = 3
portero = Pin(PIN_PORTERO, Pin.OUT)
portero.value(0)

led = Pin(2, Pin.OUT)
led.value(0)

# --- Reed switch (sensor magnetico de puerta) ---
# GPIO27. Nunca GPIO1/GPIO3 (TX0/RX0): son la consola serie por USB, y
# reconfigurarlas como GPIO corta la comunicacion con Thonny/REPL.
#   puerta CERRADA -> iman cerca  -> contacto cerrado -> lee 0
#   puerta ABIERTA  -> iman lejos -> contacto abierto -> lee 1
PIN_REED = 27
REED_ABIERTA_EN = 1
reed = Pin(PIN_REED, Pin.IN, Pin.PULL_UP)

# --- LED de estado de puerta (encendido = abierta) ---
led_puerta = Pin(33, Pin.OUT)
led_puerta.value(0)

# --- Boton de grabado: mantenido apretado mientras se acerca una tarjeta
#     nueva, esa tarjeta queda autorizada. Reposo=0, presionado=1. ---
boton_grabar = Pin(32, Pin.IN, Pin.PULL_DOWN)


def puerta_abierta():
    return reed.value() == REED_ABIERTA_EN


def abrir_portero():
    print("  -> Acceso PERMITIDO, abriendo porton...")
    led.value(1)
    portero.value(1)
    sleep(SEGUNDOS_ABIERTO)
    portero.value(0)
    led.value(0)
    print("  -> Porton cerrado.")


# UID autorizados de fabrica, formato "AA:BB:CC:DD". Los grabados con el
# boton se agregan aparte, en ARCHIVO_UIDS, y persisten entre reinicios.
UIDS_AUTORIZADOS_BASE = {
    "DE:AD:BE:EF",
}
ARCHIVO_UIDS = "uids.txt"


def cargar_uids_autorizados():
    uids = set(UIDS_AUTORIZADOS_BASE)
    try:
        with open(ARCHIVO_UIDS) as f:
            for linea in f:
                linea = linea.strip()
                if linea:
                    uids.add(linea)
    except OSError:
        pass
    return uids


def grabar_uid_autorizado(uid):
    UIDS_AUTORIZADOS.add(uid)
    with open(ARCHIVO_UIDS, "a") as f:
        f.write(uid + "\n")


UIDS_AUTORIZADOS = cargar_uids_autorizados()

DEBOUNCE_MS = 2000  # ignora la misma tarjeta repetida durante este tiempo

ultimo_uid = None
ultimo_ms = 0
estado_puerta_previo = puerta_abierta()
led_puerta.value(1 if estado_puerta_previo else 0)

print("Sistema listo. Acerque una tarjeta para abrir la puerta...")
print("UIDs autorizados:", UIDS_AUTORIZADOS)

while True:
    estado_puerta = puerta_abierta()
    if estado_puerta != estado_puerta_previo:
        print("[PUERTA] ABIERTA" if estado_puerta else "[PUERTA] CERRADA")
        estado_puerta_previo = estado_puerta
        led_puerta.value(1 if estado_puerta else 0)

    try:
        (estatus, _tipo) = lector.request(lector.REQIDL)
        if estatus == lector.OK:
            (estatus, raw_uid) = lector.anticoll()
            if estatus == lector.OK:
                uid = "{:02X}:{:02X}:{:02X}:{:02X}".format(*raw_uid[:4])
                ahora = ticks_ms()
                if uid != ultimo_uid or ticks_diff(ahora, ultimo_ms) > DEBOUNCE_MS:
                    ultimo_uid = uid
                    ultimo_ms = ahora
                    print("\n[+] Tarjeta detectada. UID:", uid)
                    if boton_grabar.value() == 1:
                        if uid in UIDS_AUTORIZADOS:
                            print("  -> Ya estaba autorizada.")
                        else:
                            grabar_uid_autorizado(uid)
                            print("  -> Tarjeta GRABADA y autorizada.")
                    elif uid in UIDS_AUTORIZADOS:
                        abrir_portero()
                    else:
                        print("  -> Acceso DENEGADO.")
        else:
            ultimo_uid = None
    except Exception as e:
        # Nunca "except:" a secas: eso taparia tambien Ctrl+C.
        print("  [!] Error de lectura, reiniciando lector:", e)
        try:
            lector.init()
        except Exception:
            pass
        ultimo_uid = None

    gc.collect()
    sleep_ms(150)
