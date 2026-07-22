# Sonda del RC522: aisla si el problema es comunicacion o contacto.
#
# Lee el registro de version 60 veces seguidas y clasifica el resultado.
# Correr en Thonny (abrir y F5) o con:
#     .venv/bin/mpremote connect /dev/ttyUSB0 run firmware/nodo_panol/probe_rc522.py
#
# No escribe en la flash ni toca main.py.

from machine import Pin, SPI
from time import sleep_ms
import config

P = config.PIN_RC522
print("pines RC522:", P)

# Reset por hardware, igual que el driver: bajar RST y subirlo.
rst = Pin(P["rst"], Pin.OUT)
cs = Pin(P["cs"], Pin.OUT)
cs.value(1)
rst.value(0); sleep_ms(50)
rst.value(1); sleep_ms(100)

spi = SPI(2, baudrate=1_000_000, polarity=0, phase=0,
          sck=Pin(P["sck"]), mosi=Pin(P["mosi"]), miso=Pin(P["miso"]))


def leer(reg):
    cs.value(0)
    spi.write(bytes([((reg << 1) & 0x7e) | 0x80]))
    v = spi.read(1)
    cs.value(1)
    return v[0]


vistos = {}
for _ in range(60):
    v = leer(0x37)
    vistos[v] = vistos.get(v, 0) + 1
    sleep_ms(20)

print("\nlecturas de reg 0x37 (VersionReg):")
for v in sorted(vistos):
    print("  0x{:02X} -> {} veces".format(v, vistos[v]))

print("\ndiagnostico:")
if 0x91 in vistos or 0x92 in vistos:
    print("  CHIP DETECTADO. Si aparece mezclado con 0x00/0xFF, el contacto")
    print("  es intermitente: reasentar los jumpers, sobre todo MISO y 3.3V.")
elif len(vistos) == 1 and 0xFF in vistos:
    print("  SIEMPRE 0xFF = MISO al aire. El modulo no maneja el bus.")
    print("  Sospechar, en orden:")
    print("   1. 3.3V no llega al modulo (medir con tester en el pin del RC522)")
    print("   2. MISO (G19) sin contacto o cruzado con MOSI (G23)")
    print("   3. RST no llega a G{} (el chip sigue en reset)".format(P["rst"]))
elif len(vistos) == 1 and 0x00 in vistos:
    print("  SIEMPRE 0x00 = MISO pegado a masa. Revisar cruce MISO/GND.")
else:
    print("  VALOR INESTABLE = contacto flojo. Reasentar TODOS los jumpers;")
    print("  en protoboard, probar otra fila. Un RC522 sano da un valor fijo.")
