#!/usr/bin/env bash
# Graba el firmware del nodo pañol en la placa (ESP32, MicroPython 1.28).
#
#   ./grabar.sh [/dev/ttyUSB0]
#
# Los módulos grandes van PRECOMPILADOS (.mpy): compilar 15-20 KB de .py en la
# placa fragmenta la RAM y después el handshake TLS del enlace MQTT no
# consigue memoria (MBEDTLS_ERR_X509_ALLOC_FAILED). main.py y config.py quedan
# como .py: son el punto de entrada y lo que se ajusta a mano.
#
# config.py de la PLACA no se pisa si ya existe: puede tener ajustes propios
# (pulso del solenoide, modo muestra). Para subirlo igual: SUBIR_CONFIG=1.
# Requiere: pip install mpy-cross==1.28.0.post2 mpremote  (cerrar Thonny antes).
set -euo pipefail
cd "$(dirname "$0")/nodo_panol"
PUERTO="${1:-/dev/ttyUSB0}"
MPY="${MPY_CROSS:-mpy-cross}"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
MODULOS=(enlace red cola reloj mfrc522)
for m in "${MODULOS[@]}"; do
  "$MPY" -march=xtensawin -o "$OUT/$m.mpy" "$m.py"
done
args=(connect "$PUERTO" fs cp main.py ca_isrg_x2.der)
for m in "${MODULOS[@]}"; do args+=("$OUT/$m.mpy"); done
args+=(:)
[ "${SUBIR_CONFIG:-0}" = 1 ] && args+=(+ fs cp config.py :)
for m in "${MODULOS[@]}"; do args+=(+ fs rm ":$m.py"); done
mpremote "${args[@]}" 2>&1 | grep -v "No such file" || true
mpremote connect "$PUERTO" reset
echo "listo: firmware grabado y placa reiniciada"
