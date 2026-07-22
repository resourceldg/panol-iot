# Configuracion del nodo panol (ESP32 #1).
#
# Todo lo ajustable vive aca: identidad, pines, tiempos y que sensores estan
# realmente cableados. main.py no deberia tener numeros magicos.

# --- Identidad del nodo ---------------------------------------------------
# El sistema cubre VARIOS laboratorios/panoles, asi que cada evento viaja
# etiquetado con su ubicacion. El servidor no puede adivinarlo por la IP:
# un DHCP que reparte otra direccion no debe cambiar a que aula pertenece.
UBICACION_ID = "panol-lab01"
NODO_ID = "panol-lab01-puerta"

# --- Pines ----------------------------------------------------------------
# RC522 por SPI. VCC a 3.3 V exclusivamente (a 5 V se quema).
PIN_RC522 = {"sck": 18, "mosi": 23, "miso": 19, "rst": 4, "cs": 5}

PIN_RELE = 26        # Rele del solenoide
PIN_REED = 27        # Reed switch: una pata aca, la otra a GND. Anduvo OK.
                     # (Evitar GPIO12: es strapping y traba el arranque.)
PIN_PIR = 16         # PIR (contacto seco NC): C a GND, NC a este pin
PIN_LED_ACCESO = 2   # LED interno de la placa
PIN_LED_PUERTA = 33  # Encendido = puerta abierta
PIN_BOTON_GRABAR = 32

# --- Que esta realmente cableado -----------------------------------------
# Permite probar de a un sensor por vez sin tocar la logica. Un sensor en
# False no se lee ni reporta: no ensucia la consola con eventos fantasma.
#
# Un reed declarado presente pero sin cablear lee 1 por el pull-up interno,
# o sea "puerta abierta" para siempre. Por eso conviene apagarlo hasta
# tenerlo puesto, en lugar de convivir con una alarma permanente.
# Estado real del banco (verificado escaneando los GPIO):
#   GPIO16 (PIR)   -> conectado y funcionando (reporta movimiento)
#   GPIO27 (reed)  -> conectado y funcionando (una pata a G27, otra a GND)
#   RC522 (SPI)    -> en diagnostico: MISO no responde (reg 0x37 = 0x00/0xFF)
SENSORES = {
    "rfid": True,
    "reed": True,
    "pir": True,
}

# Polaridad del rele, a confirmar CUANDO SE CABLEE (todavia no lo esta).
#   0 -> modulo activo en LOW  (lo que pide la spec v1.0 seccion 5)
#   1 -> modulo activo en HIGH (lo que asumia el prototipo original)
# El valor de reposo es el contrario, y es el que toma el pin al bootear.
# Recordar el pull-up externo de 10 kOhm: el estado seguro por software
# llega tarde, porque durante el reset el pin flota antes de correr codigo.
RELE_ACTIVO_EN = 0

# --- Tiempos (spec v1.0 seccion 12) --------------------------------------
T_PULSO_SOLENOIDE_MS = 800    # Duracion del pulso sobre el solenoide
T_APERTURA_MS = 10_000        # Ventana de la "promesa": pulso -> reed abre
T_ANTIRREBOTE_REED_MS = 300   # Estabilidad minima para reportar un cambio
THROTTLE_PIR_MS = 30_000      # Maximo un reporte de movimiento cada 30 s
T_MISMA_TARJETA_MS = 2_000    # Ignora la misma tarjeta repetida
T_CICLO_MS = 50               # Periodo del bucle principal

# --- Robustez -------------------------------------------------------------
# El watchdog reinicia el nodo si el firmware se cuelga. En banco molesta
# (cortar con Ctrl+C y quedarse en el REPL dispara el reset), asi que se
# activa recien al montar en la puerta.
USAR_WDT = False
T_WDT_MS = 8_000

# --- Red ------------------------------------------------------------------
# Subred WiFi aislada de IoT. Los nodos NO viven en la red de los alumnos ni
# en la WLAN de admins: solo tienen que llegar al servidor local.
WIFI_SSID = "CAMBIAR"
WIFI_PASS = "CAMBIAR"
SERVER_URL = "http://192.168.100.44:5000"

# Poner en False para trabajar en banco sin WiFi: el nodo funciona igual,
# decide contra la whitelist local y acumula todo en cola.log.
USAR_RED = False

T_CONEXION_WIFI_MS = 15_000     # Espera maxima al asociarse, solo al bootear
T_TIMEOUT_HTTP_S = 3            # Corto: la red nunca debe demorar la puerta
T_HEARTBEAT_MS = 60_000         # Señal de vida (spec seccion 12)
T_REFRESCO_WHITELIST_MS = 900_000   # 15 min
T_REINTENTO_RED_MS = 30_000     # Espera tras un fallo, para no bloquear
                                # el bucle con un timeout en cada vuelta
LOTE_COLA = 10                  # Eventos por POST al vaciar la cola

ARCHIVO_UIDS = "uids.txt"            # Tarjetas grabadas con el boton
ARCHIVO_WHITELIST = "whitelist.txt"  # Copia local de la lista del servidor
ARCHIVO_COLA = "cola.log"
ARCHIVO_BOOT = "boot_id.txt"
