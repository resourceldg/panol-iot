# Prueba aislada del solenoide (MOSFET). Verifica el pulso SIN la FSM ni
# tarjetas: solo energiza la compuerta el tiempo configurado y la suelta.
#
# ANTES DE CORRER, confirmar:
#   1. Diodo 1N4007 en antiparalelo con la bobina (banda hacia +12V). Sin el,
#      el primer corte puede quemar el MOSFET. OBLIGATORIO.
#   2. Pull-DOWN de 10k en la compuerta del MOSFET (GPIO15 arranca en HIGH).
#   3. Fuente de 12 V del solenoide con masa comun con el ESP32.
#
# Correr en Thonny (abrir y F5) o con:
#     .venv/bin/mpremote connect /dev/ttyUSB0 run firmware/nodo_panol/prueba_solenoide.py
#
# No escribe en la flash ni toca main.py.

from machine import Pin
from time import sleep_ms, ticks_ms, ticks_diff
import config

PULSOS = 2          # Cuantos pulsos de prueba
PAUSA_MS = 3000     # Descanso entre pulsos (deja enfriar la bobina)

REPOSO = 0 - config.RELE_ACTIVO_EN
rele = Pin(config.PIN_RELE, Pin.OUT)
rele.value(REPOSO)   # Estado seguro ANTES que nada

print("=" * 58)
print("  PRUEBA SOLENOIDE — GPIO{}  (MOSFET activo en {})".format(
    config.PIN_RELE, config.RELE_ACTIVO_EN))
print("=" * 58)
print("  reposo  = {}  (solenoide TRABADO)".format(REPOSO))
print("  activo  = {}  (solenoide LIBERADO)".format(config.RELE_ACTIVO_EN))
print("  pulso   = {} ms".format(config.T_PULSO_SOLENOIDE_MS))
print()
print("  Si escuchas un 'clic' y la bobina tira, el MOSFET conmuta bien.")
print("  Si NO pasa nada: revisar 12V, masa comun y la compuerta.")
print("  Si queda pegado (no vuelve): la polaridad esta invertida.")
print()

# Margen para leer los avisos y alejar las manos.
for n in range(3, 0, -1):
    print("  arrancando en {}...".format(n))
    sleep_ms(1000)

for i in range(1, PULSOS + 1):
    print("\n  --- pulso {}/{} ---".format(i, PULSOS))
    t0 = ticks_ms()
    rele.value(config.RELE_ACTIVO_EN)
    print("  [{:>6} ms] ACTIVADO  (gate {})".format(0, config.RELE_ACTIVO_EN))
    # Espera activa: mide el pulso real, no confia en sleep_ms exacto.
    while ticks_diff(ticks_ms(), t0) < config.T_PULSO_SOLENOIDE_MS:
        pass
    rele.value(REPOSO)
    print("  [{:>6} ms] SOLTADO   (gate {})".format(
        ticks_diff(ticks_ms(), t0), REPOSO))
    if i < PULSOS:
        sleep_ms(PAUSA_MS)

# Garantiza el estado seguro al terminar, pase lo que pase.
rele.value(REPOSO)
print("\n  Fin. Rele en reposo (solenoide trabado).")
