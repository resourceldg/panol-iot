# Debugger del PIR — aislado, para caracterizar el sensor sin la FSM.
#
# El PIR de alarma (DSC, contacto seco NC) no es un HC-SR501: reposo = 0
# (contacto cerrado ata el pin a GND), deteccion = 1 (contacto abre y manda
# el pull-up). Este script muestra el pin CRUDO, sin throttle ni antirrebote,
# para ver falsos positivos, rebotes y cuanto dura cada deteccion.
#
# Correr en Thonny (abrir y F5) o con:
#     .venv/bin/mpremote connect /dev/ttyUSB0 run firmware/nodo_panol/debug_pir.py
#
# No escribe en la flash ni toca main.py.

from machine import Pin
from time import sleep_ms, ticks_ms, ticks_diff
import config

PIN = config.PIN_PIR
DURACION_MS = 60_000     # Cuanto observar
MUESTREO_MS = 20         # Resolucion: 50 lecturas por segundo

# PULL_UP obligatorio: en NC, cuando el contacto ABRE la linea queda al aire
# y sin pull-up leeria basura. Con el contacto seco esto es lo correcto.
pir = Pin(PIN, Pin.IN, Pin.PULL_UP)


def barra(nivel):
    # Ayuda visual: bloque lleno = deteccion, vacio = reposo.
    return "############" if nivel else "............"


print("=" * 60)
print("  DEBUG PIR — GPIO{}  (contacto seco NC, con pull-up)".format(PIN))
print("=" * 60)
print("  reposo esperado = 0 (sin movimiento)")
print("  deteccion       = 1 (movimiento)")
print("  Movete y quedate quieto para ver transiciones y duraciones.")
print("  Ctrl-C para cortar.\n")

inicio = ticks_ms()
previo = pir.value()
t_cambio = inicio
transiciones = 0
tiempo_en = 0            # ms acumulados en deteccion
detecciones = 0

print("  [{:>6} ms] nivel inicial = {}  {}".format(0, previo, barra(previo)))

try:
    while ticks_diff(ticks_ms(), inicio) < DURACION_MS:
        ahora = ticks_ms()
        v = pir.value()
        if v != previo:
            dur = ticks_diff(ahora, t_cambio)
            transiciones += 1
            if v == 1:
                detecciones += 1
                etiqueta = "MOVIMIENTO"
            else:
                tiempo_en += dur   # se estuvo `dur` ms en deteccion
                etiqueta = "reposo (duro {} ms)".format(dur)
            print("  [{:>6} ms] {} -> {}  {}  {}".format(
                ticks_diff(ahora, inicio), previo, v, barra(v), etiqueta))
            previo = v
            t_cambio = ahora
        sleep_ms(MUESTREO_MS)
except KeyboardInterrupt:
    pass

total = ticks_diff(ticks_ms(), inicio)
print("\n" + "=" * 60)
print("  RESUMEN ({:.1f} s observados)".format(total / 1000))
print("  detecciones (flancos 0->1): {}".format(detecciones))
print("  transiciones totales      : {}".format(transiciones))
print("  tiempo en deteccion       : {:.1f} s ({:.0f} %)".format(
    tiempo_en / 1000, 100 * tiempo_en / total if total else 0))
if transiciones == 0:
    print("\n  Sin cambios. Posibles causas:")
    print("   - masa comun ausente (GND fuente 12V y GND ESP32 no unidas)")
    print("   - contacto en NO en vez de NC")
    print("   - PIR calentando (~60 s tras alimentarlo)")
elif detecciones > 20:
    print("\n  Muchas detecciones: puede ser normal si te moviste, o ruido")
    print("  si el sensor esta suelto / sin masa comun firme.")
print("=" * 60)
