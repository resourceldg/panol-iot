# Escaner de GPIO: descubre en que pin esta cada cosa, sin leer rotulos.
#
# Util cuando el cableado no coincide con la documentacion, o cuando el
# sensor es de contacto seco y no se sabe donde cae. Todos los pines se
# leen con PULL_UP, que es como hay que leer un contacto seco:
#
#     contacto CERRADO -> el pin queda atado a GND -> 0
#     contacto ABIERTO -> manda el pull-up          -> 1
#
# Correr desde Thonny (abrir y F5) o desde la PC con:
#     .venv/bin/mpremote connect /dev/ttyUSB0 \
#         run firmware/nodo_panol/escanear_gpio.py
#
# No escribe nada en la flash ni toca main.py.

from machine import Pin
from time import sleep_ms, ticks_ms, ticks_diff

DURACION_MS = 45_000

# Se omiten 0 (boot), 1 y 3 (consola serie por USB) y 6-11 (flash interna).
# Los 34-39 son solo-entrada y NO tienen pull-up interno, asi que no sirven
# para un contacto seco sin una resistencia externa.
GPIOS = (2, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33)


def main():
    pines = {}
    for g in GPIOS:
        try:
            pines[g] = Pin(g, Pin.IN, Pin.PULL_UP)
        except Exception as e:
            print("GPIO{} no utilizable: {}".format(g, e))
    sleep_ms(100)

    estado = {g: p.value() for g, p in pines.items()}
    print("\nNivel en reposo (con pull-up interno):")
    en_cero = [g for g in sorted(estado) if estado[g] == 0]
    print("  en 0 (algo lo ata a GND):", en_cero or "ninguno")
    print("  en 1 (libre o en alto)  :", [g for g in sorted(estado) if estado[g] == 1])

    print("\n--- {} s de monitoreo ---".format(DURACION_MS // 1000))
    print("MOVETE frente al sensor y despues QUEDATE QUIETO.")
    print("El pin que conmute es el del contacto.\n")

    inicio = ticks_ms()
    cambios = {}
    while ticks_diff(ticks_ms(), inicio) < DURACION_MS:
        t = ticks_diff(ticks_ms(), inicio)
        for g, p in pines.items():
            v = p.value()
            if v != estado[g]:
                estado[g] = v
                cambios[g] = cambios.get(g, 0) + 1
                print("  [{:>6} ms] GPIO{:<2} -> {}".format(t, g, v))
        sleep_ms(30)

    print("\n=== Resultado ===")
    if cambios:
        for g in sorted(cambios):
            print("  GPIO{:<2}: {} transiciones (final={})".format(
                g, cambios[g], estado[g]))
        print("\n  El pin con transiciones es la salida del sensor.")
    else:
        print("  Ningun pin conmuto.")
        print("  Posibles causas, en orden de probabilidad:")
        print("    1. GND del ESP32 y GND de la fuente de 12 V no estan unidas")
        print("    2. el contacto usado es NO en vez de NC (o el jumper)")
        print("    3. el PIR todavia esta calentando (~60 s tras alimentarlo)")
        print("    4. una resistencia EOL en serie enmascara el contacto")


main()
