# Diagnostico de banco: verifica que los sensores conectados respondan.
#
# NO escribe nada en la flash de la placa ni toca main.py. Pensado para
# correrse desde la PC con:
#
#     .venv/bin/mpremote connect /dev/ttyUSB0 \
#         mount firmware/nodo_panol run firmware/nodo_panol/prueba_sensores.py
#
# `mount` expone la carpeta local como sistema de archivos del ESP32, asi
# que la placa queda exactamente como estaba al desconectar. Es la forma de
# probar sin comprometer el firmware que ya anda.
#
# Que verifica:
#   1. RC522   — responde el registro de version y lee UIDs
#   2. Reed    — nivel actual y cambios (con antirrebote)
#   3. PIR     — nivel actual y detecciones (con throttle corto)
#   4. Rele    — NO lo activa. Solo informa en que estado quedaria el pin.

from machine import Pin
from time import sleep_ms, ticks_ms, ticks_diff

import config

DURACION_MS = 30_000       # Cuanto dura la prueba
ANTIRREBOTE_REED_MS = 300
THROTTLE_PIR_MS = 2_000    # Mas corto que en produccion: aca queremos verlo


def titulo(texto):
    print("\n=== {} ===".format(texto))


def revisar_rc522():
    titulo("RC522 (RFID)")
    if not config.SENSORES["rfid"]:
        print("  deshabilitado en config.SENSORES")
        return None

    from mfrc522 import MFRC522

    lector = MFRC522(**config.PIN_RC522)
    ver = lector._rreg(0x37)
    if ver in (0x91, 0x92):
        print("  OK  version 0x{:02X} (chip autentico)".format(ver))
        return lector

    # 0x00 o 0xFF casi siempre significan cableado, no chip fallado.
    print("  FALLA  registro 0x37 = 0x{:02X}".format(ver))
    if ver in (0x00, 0xFF):
        print("  0x00/0xFF = no hay comunicacion SPI. Revisar en este orden:")
        print("    1. VCC del RC522 a 3.3 V (NUNCA 5 V: se quema)")
        print("    2. GND comun entre placa y modulo")
        print("    3. SDA={cs} SCK={sck} MOSI={mosi} MISO={miso} RST={rst}".format(
            **config.PIN_RC522))
    return None


def revisar_reed():
    titulo("Reed switch (puerta)")
    if not config.SENSORES["reed"]:
        print("  deshabilitado en config.SENSORES (poner True al cablearlo)")
        return None
    pin = Pin(config.PIN_REED, Pin.IN, Pin.PULL_UP)
    valor = pin.value()
    print("  GPIO{} = {} -> puerta {}".format(
        config.PIN_REED, valor, "ABIERTA" if valor == 1 else "CERRADA"))
    if valor == 1:
        print("  Nota: sin reed cableado el pull-up interno da 1 igual.")
        print("  Acercar y alejar el iman para confirmar que cambia.")
    return pin


def revisar_pir():
    titulo("PIR HC-SR501")
    if not config.SENSORES["pir"]:
        print("  deshabilitado en config.SENSORES (poner True al probarlo)")
        return None
    pin = Pin(config.PIN_PIR, Pin.IN)
    print("  GPIO{} = {}".format(config.PIN_PIR, pin.value()))
    print("  Recordar: jumper en H (retrigger) y delay al minimo.")
    print("  El HC-SR501 necesita ~60 s de calentamiento tras alimentarlo:")
    print("  si dispara solo al principio, es normal.")
    return pin


def revisar_rele():
    titulo("Rele del solenoide")
    reposo = 1 - config.RELE_ACTIVO_EN
    print("  GPIO{} quedaria en {} (reposo) con RELE_ACTIVO_EN = {}".format(
        config.PIN_RELE, reposo, config.RELE_ACTIVO_EN))
    print("  NO se activa en esta prueba: el rele todavia no esta conectado.")


def monitorear(lector, reed, pir):
    titulo("Monitoreo {} s — acerque tarjetas, mueva el iman, camine".format(
        DURACION_MS // 1000))

    inicio = ticks_ms()
    reed_estable = reed.value() if reed else None
    reed_crudo = reed_estable
    reed_desde = inicio
    pir_ultimo = None
    uid_ultimo = None
    lecturas = 0

    while ticks_diff(ticks_ms(), inicio) < DURACION_MS:
        ahora = ticks_ms()

        if reed is not None:
            valor = reed.value()
            if valor != reed_crudo:
                reed_crudo = valor
                reed_desde = ahora
            elif (valor != reed_estable
                    and ticks_diff(ahora, reed_desde) >= ANTIRREBOTE_REED_MS):
                reed_estable = valor
                print("  [{:>6}] REED  -> {}".format(
                    ahora - inicio, "ABIERTA" if valor == 1 else "CERRADA"))

        if pir is not None and pir.value() == 1:
            if pir_ultimo is None or ticks_diff(ahora, pir_ultimo) >= THROTTLE_PIR_MS:
                pir_ultimo = ahora
                print("  [{:>6}] PIR   -> movimiento".format(ahora - inicio))

        if lector is not None:
            try:
                (estado, _) = lector.request(lector.REQIDL)
                if estado == lector.OK:
                    (estado, raw) = lector.anticoll()
                    if estado == lector.OK:
                        uid = "{:02X}:{:02X}:{:02X}:{:02X}".format(*raw[:4])
                        if uid != uid_ultimo:
                            uid_ultimo = uid
                            lecturas += 1
                            autorizada = uid in _uids_conocidos()
                            print("  [{:>6}] RFID  -> {} ({})".format(
                                ahora - inicio, uid,
                                "AUTORIZADA" if autorizada else "desconocida"))
                else:
                    uid_ultimo = None
            except Exception as e:
                print("  [{:>6}] RFID  -> error: {}".format(ahora - inicio, e))

        sleep_ms(50)

    return lecturas


def _uids_conocidos():
    uids = set()
    for ruta in (config.ARCHIVO_WHITELIST, config.ARCHIVO_UIDS):
        try:
            with open(ruta) as f:
                for linea in f:
                    if linea.strip():
                        uids.add(linea.strip())
        except OSError:
            pass
    return uids


def main():
    print("\n" + "=" * 58)
    print("  DIAGNOSTICO DE SENSORES — nodo {}".format(config.NODO_ID))
    print("  ubicacion: {}".format(config.UBICACION_ID))
    print("=" * 58)

    lector = revisar_rc522()
    reed = revisar_reed()
    pir = revisar_pir()
    revisar_rele()

    if lector is None and reed is None and pir is None:
        print("\nNo hay ningun sensor habilitado. Revisar config.SENSORES.")
        return

    lecturas = monitorear(lector, reed, pir)

    titulo("Resumen")
    print("  RC522 : {}".format("responde" if lector else "NO responde"))
    print("  Reed  : {}".format("cableado" if reed else "no habilitado"))
    print("  PIR   : {}".format("cableado" if pir else "no habilitado"))
    print("  Tarjetas leidas: {}".format(lecturas))
    print("  UIDs conocidos : {}".format(len(_uids_conocidos())))


main()
