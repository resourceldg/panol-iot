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

PIN_RELE = 15        # MOSFET del solenoide (activo-LOW, ver abajo). GPIO15 es
                     # STRAPPING y arranca en HIGH por pull-up interno; como el
                     # reposo aca ES HIGH, ese estado inicial YA es el seguro:
                     # el solenoide queda trabado durante el boot sin ayuda
                     # externa. Feliz coincidencia, no accidente.
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
RELE_ACTIVO_EN = 1   # ACTIVO-LOW, verificado en la placa: gate LOW -> energiza
                     # el solenoide; reposo = HIGH. (La prueba con 1 daba la
                     # logica invertida.) Reposo HIGH coincide con el estado
                     # de arranque de GPIO15: seguro por hardware.

# --- Tiempos (spec v1.0 seccion 12) --------------------------------------
T_PULSO_SOLENOIDE_MS = 800    # Duracion del pulso sobre el solenoide
T_APERTURA_MS = 10_000        # Ventana de la "promesa": pulso -> reed abre
T_ANTIRREBOTE_REED_MS = 300   # Estabilidad minima para reportar un cambio
THROTTLE_PIR_MS = 30_000      # Maximo un reporte de movimiento cada 30 s
T_MISMA_TARJETA_MS = 2_000    # Ignora la misma tarjeta repetida
T_PARPADEO_RECHAZO_MS = 900   # Tarjeta rechazada: 3 parpadeos del LED de acceso
T_CICLO_MS = 50               # Periodo del bucle principal

# --- Robustez -------------------------------------------------------------
# El watchdog reinicia el nodo si el firmware se cuelga. En banco molesta
# (cortar con Ctrl+C y quedarse en el REPL dispara el reset), asi que se
# activa recien al montar en la puerta.
USAR_WDT = False
# Tiene que superar la operacion de red mas larga que bloquea el bucle: el
# (re)enganche MQTT (TCP hasta T_CONEXION_MQTT_S + handshake TLS ~5 s) o un
# HTTPS de respaldo (T_TIMEOUT_HTTP_S). Con 8 s reiniciaria la placa en medio
# de un handshake.
T_WDT_MS = 30_000

# --- Red ------------------------------------------------------------------
# Subred WiFi aislada de IoT. Los nodos NO viven en la red de los alumnos ni
# en la WLAN de admins: solo tienen que llegar al servidor local.
#
# Las credenciales viven en secrets.py, que NO va al repositorio. Ahi hay una
# LISTA de redes conocidas (casa, banco, colegio): el nodo escanea y elige la
# de mejor señal que conozca, y prueba la siguiente si falla. Si el archivo no
# esta, se cae a una lista con un placeholder y el nodo arranca igual, en modo
# banco. Se acepta tambien el formato viejo (WIFI_SSID/WIFI_PASS sueltos) para
# no romper una placa ya grabada.
try:
    from secrets import REDES_WIFI
except ImportError:
    try:
        from secrets import WIFI_SSID, WIFI_PASS
        REDES_WIFI = [(WIFI_SSID, WIFI_PASS)]
    except ImportError:
        REDES_WIFI = [("CAMBIAR", "CAMBIAR")]

# IP del servidor EN la red del nodo, y el puerto de la API (18500).
# Etapa 2: el server vive en el homelab (192.168.100.48). Para volver a probar
# contra la notebook, cambiar por la linea comentada.
# SERVER_URL = "http://192.168.100.48:18500"
SERVER_URL = "https://homelab-01.tail4eda13.ts.net:8443"
# SERVER_URL = "http://192.168.100.44:18500"   # notebook, pruebas de banco

# --- Enlace MQTT persistente (etapa 2b) -------------------------------------
# Eventos y latido viajan por UNA conexion MQTT/TLS que queda abierta, al nodo
# Tailscale "panol" del homelab (Funnel -> broker). Es un nombre fijo: funciona
# desde cualquier red, sin depender de la IP ni del lugar del servidor. HTTP
# queda para bajar la whitelist y como respaldo si MQTT no anda (ver red.py).
USAR_MQTT = True
MQTT_HOST = "panol.tail4eda13.ts.net"
MQTT_PORT = 443
MQTT_USER = "nodo-panol-puerta"
# La clave es aleatoria y vive en el server (/etc/panol/secrets/
# mqtt-nodo-panol-puerta.pass). NO va al repo: se carga en secrets.py.
try:
    from secrets import MQTT_PASS
except ImportError:
    MQTT_PASS = ""
MQTT_KEEPALIVE_S = 60             # PINGREQ si no hubo trafico en 30 s
MQTT_VERIFICAR_TLS = True         # verificar el certificado (necesita hora NTP)
# Raiz ISRG X2 en DER (543 bytes, vence en 2040). Los certificados de
# Tailscale son ECDSA y encadenan a X2 (Let's Encrypt YE2 -> Root YE -> X2).
# En DER y una sola: con X1+X2 en PEM, el ESP32 se quedaba sin memoria al
# parsear (MBEDTLS_ERR_X509_ALLOC_FAILED).
ARCHIVO_CA = "ca_isrg_x2.der"
T_CONEXION_MQTT_S = 5             # tope de cada paso de red al (re)conectar
T_ESCRITURA_MQTT_S = 2            # tope para escribir un paquete
T_REINTENTO_MQTT_MAX_MS = 15_000  # backoff de reconexion: 1, 2, 4 ... 15 s
VENTANA_MQTT = 10                 # eventos publicados sin acuse a la vez
# Respaldo HTTP para EVENTOS: apagado. Probado en la placa: un POST por
# Funnel con el enlace inestable bloqueo el bucle 87 s (urequests aplica el
# timeout por operacion, no al total) y el PIR no detecto nada en ese lapso.
# Sin MQTT, los eventos esperan en la cola en flash y salen al reconectar.
USAR_RESPALDO_HTTP = False
T_RESPALDO_HTTP_MS = 120_000      # sin MQTT por 2 min: whitelist (y eventos, si se activa) por HTTP
# La whitelist llega por MQTT, retenida, apenas conecta. Si en este tiempo no
# llego (servidor viejo que no la publica), se baja por HTTPS como antes.
T_ESPERA_WHITELIST_MQTT_MS = 60_000

# Token para autenticar contra la API cuando se llega por internet (HTTPS via
# Caddy). En la LAN, con la API sin token, se deja vacío. Es el mismo valor que
# el homelab deja en /etc/panol/secrets/api.token. NO va al repo: ponerlo en
# secrets.py junto con las redes.
try:
    from secrets import API_TOKEN
except ImportError:
    API_TOKEN = ""

# True: intenta WiFi + NTP + hablar con el server. Si el server no esta, el
# nodo igual anda: asocia el WiFi, sincroniza la hora y encola en cola.log
# hasta que el server aparezca (modo degradado).
USAR_RED = True

T_CONEXION_WIFI_MS = 15_000     # Espera maxima al asociarse, solo al bootear
                                # (y ventana de cada reintento en caliente)
T_REINTENTO_WIFI_MS = 30_000    # Entre reasociaciones cuando se cae el AP.
                                # Reintentar mas seguido no acelera nada: el
                                # driver tarda lo suyo y el nodo tiene que
                                # seguir mirando los sensores mientras tanto.
T_RESYNC_NTP_MS = 6 * 3_600_000  # Resync de reloj cada 6 h. El ESP32 no tiene
                                # RTC con pila y deriva; en la cola offline el
                                # timestamp es lo que atribuye el evento a su
                                # sesion.
T_TIMEOUT_NTP_S = 2             # Acotado: sin esto, un NTP inalcanzable puede
                                # bloquear el bucle principal ~30 s
T_TIMEOUT_HTTP_S = 8            # El handshake TLS del ESP32 por Funnel tarda 4-5 s: con 3 nunca
                                # completaba. Solo corre en IDLE (nunca en la promesa)
                                # y con MQTT andando solo se usa para la whitelist.
T_HEARTBEAT_MS = 60_000         # Señal de vida (spec seccion 12)
T_REFRESCO_WHITELIST_MS = 900_000   # 15 min
T_REINTENTO_RED_MS = 30_000     # Espera tras un fallo, para no bloquear
                                # el bucle con un timeout en cada vuelta
LOTE_COLA = 10                  # Eventos por POST al vaciar la cola
MAX_COLA = 5000                 # Tope de la cola en flash. Un corte de red de
                                # dias no debe llenar el almacenamiento ni tirar
                                # el nodo: pasado esto se descartan los eventos
                                # NUEVOS y se conserva la evidencia mas vieja.

ARCHIVO_UIDS = "uids.txt"            # Tarjetas grabadas con el boton
ARCHIVO_WHITELIST = "whitelist.txt"  # Copia local de la lista del servidor
ARCHIVO_COLA = "cola.log"
ARCHIVO_BOOT = "boot_id.txt"
