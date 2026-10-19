# Enlace MQTT persistente con el servidor (etapa 2b).
#
# Por que existe: con HTTP cada envio abria una conexion HTTPS nueva. En el
# ESP32 un handshake TLS tarda segundos; con timeout de 3 s fallaba seguido,
# el nodo esperaba 30 s y los eventos llegaban con 1-2 minutos de demora.
# Aca se abre UNA conexion TLS y se mantiene: el handshake se paga al
# conectar, y cada evento cuesta lo que tarda un paquete en ir y volver.
#
# Destino: panol.<tailnet>.ts.net:443 (nodo Tailscale "panol" con Funnel ->
# broker del pañol). Es un nombre fijo que funciona desde cualquier red, sin
# depender de la IP ni del lugar del servidor.
#
# Reglas que se respetan del diseño original:
#   - La red nunca esta en el camino critico de la puerta: esto se llama solo
#     con la FSM en IDLE (igual que red.tareas). Lo unico que bloquea es el
#     handshake al (re)conectar; publicar y leer acuses NO bloquean.
#   - Ningun evento se borra de la cola en flash hasta que el broker lo acusa
#     (PUBACK de QoS 1). El broker los guarda en disco y el puente del
#     servidor los recibe con sesion persistente aunque estuviera caido.
#   - Duplicados: tras una reconexion se reenvian los no acusados; el
#     servidor los descarta por event_id.
#
# Cliente MQTT 3.1.1 propio y minimo (CONNECT, PUBLISH QoS 0/1, PUBACK,
# PINGREQ). umqtt.simple espera el PUBACK bloqueando, que es justo lo que
# no se puede hacer en el bucle de la puerta.

import json
import socket
import struct
from time import ticks_ms, ticks_diff, ticks_add
import gc

try:
    import ssl
except ImportError:  # MicroPython viejo
    import ussl as ssl

import config
import cola
import reloj

# Estado de la conexion
_sock = None            # socket TLS
_crudo = None           # socket TCP debajo (para cambiar bloqueo/timeout)
_buf = b""              # bytes recibidos aun sin procesar
_conectado = False
_pid = 0                # packet id de MQTT (1..65535)
_enviados = []          # pids publicados del lote actual, en orden de la cola
_acusados = set()       # pids ya acusados por el broker
_ultimo_tx_ms = 0
_ultimo_rx_ms = 0
_ultimo_hb_ms = None
_proximo_intento_ms = 0
_espera_ms = 1000       # backoff de reconexion: 1, 2, 4 ... hasta el tope
_desconectado_desde = None
_ultimo_error = None
_conectado_desde = None
_conexion_id = 0        # cambia en cada (re)conexion
# Mensajes que llegan por las suscripciones: al_mensaje(topic, payload).
# Lo registra red.py (whitelist); el enlace no sabe que hay adentro.
al_mensaje = None
# Topics a los que suscribirse en cada conexion (QoS 0: lo que importa viaja
# RETENIDO, asi que se recibe de nuevo en cada reconexion).
SUSCRIPCIONES = []


def _log(*args):
    print("[MQTT]", *args)


def conectado():
    return _conectado


def conexion_id():
    return _conexion_id


def conectado_hace_ms(ahora):
    return ticks_diff(ahora, _conectado_desde) if _conectado and _conectado_desde is not None else 0


def caido_hace_ms(ahora):
    """Cuanto hace que no hay enlace (0 si esta conectado)."""
    if _conectado or _desconectado_desde is None:
        return 0
    return ticks_diff(ahora, _desconectado_desde)


def toca_intentar(ahora):
    """True si en esta vuelta se intentaria (re)conectar (handshake TLS)."""
    return (not _conectado and ticks_diff(ahora, _proximo_intento_ms) >= 0
            and (not config.MQTT_VERIFICAR_TLS or reloj.sincronizado()))


def estado():
    return {"conectado": _conectado, "en_vuelo": len(_enviados) - len(_acusados),
            "error": _ultimo_error}


# --- Codificacion MQTT ----------------------------------------------------

def _largo(n):
    """Remaining Length de MQTT (varint de 7 bits)."""
    b = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n:
            d |= 0x80
        b.append(d)
        if not n:
            return bytes(b)


def _cadena(s):
    if isinstance(s, str):
        s = s.encode()
    return struct.pack("!H", len(s)) + s


def _paquete_connect():
    cuerpo = _cadena("MQTT") + bytes([4])           # protocolo 3.1.1
    flags = 0x02 | 0x80 | 0x40                       # clean session, usuario, clave
    cuerpo += bytes([flags]) + struct.pack("!H", config.MQTT_KEEPALIVE_S)
    cuerpo += _cadena(config.NODO_ID)                # client id = id del nodo
    cuerpo += _cadena(config.MQTT_USER) + _cadena(config.MQTT_PASS)
    return b"\x10" + _largo(len(cuerpo)) + cuerpo


def _paquete_publish(topic, payload, qos, pid=0):
    if isinstance(payload, str):
        payload = payload.encode()
    cuerpo = _cadena(topic)
    if qos:
        cuerpo += struct.pack("!H", pid)
    cuerpo += payload
    return bytes([0x30 | (qos << 1)]) + _largo(len(cuerpo)) + cuerpo


def _paquete_subscribe(pid, topics):
    cuerpo = struct.pack("!H", pid)
    for t in topics:
        cuerpo += _cadena(t) + bytes([0])          # QoS 0 pedido
    return b"\x82" + _largo(len(cuerpo)) + cuerpo


def _siguiente_pid():
    global _pid
    _pid = _pid % 65535 + 1
    return _pid


# --- Socket ---------------------------------------------------------------

def _direccion_ipv4(host, puerto):
    """Primera dirección IPv4 del host. El ESP32 (MicroPython) no maneja
    IPv6: si el DNS devuelve solo AAAA, getaddrinfo trae la entrada con la
    dirección en None. Se pide IPv4 explícitamente y se filtra igual."""
    try:
        resultados = socket.getaddrinfo(host, puerto, socket.AF_INET)
    except TypeError:            # firma sin familia (dobles de prueba)
        resultados = socket.getaddrinfo(host, puerto)
    for r in resultados:
        if r[0] == getattr(socket, "AF_INET", 2) and r[-1] is not None:
            return r[-1]
    raise OSError("el DNS no devolvió IPv4 para " + host)


def _envolver_tls(crudo, host):
    """TLS con SNI (Funnel elige el destino por el nombre) y, si se puede,
    verificando el certificado contra las raices de Let's Encrypt."""
    ca = None
    if config.MQTT_VERIFICAR_TLS:
        try:
            with open(config.ARCHIVO_CA, "rb") as f:
                ca = f.read()
        except OSError:
            _log("AVISO: falta", config.ARCHIVO_CA, "- sin verificar certificado")
    if hasattr(ssl, "SSLContext"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if ca:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=ca)
            ca = None
        else:
            ctx.verify_mode = ssl.CERT_NONE
        gc.collect()   # el handshake necesita un bloque grande y contiguo
        return ctx.wrap_socket(crudo, server_hostname=host)
    return ssl.wrap_socket(crudo, server_hostname=host)


def _escribir(datos):
    """Escritura con timeout acotado (los paquetes son chicos)."""
    global _ultimo_tx_ms
    _crudo.settimeout(config.T_ESCRITURA_MQTT_S)
    try:
        _sock.write(datos)
    finally:
        _crudo.setblocking(False)
    _ultimo_tx_ms = ticks_ms()


def _leer_disponible():
    """Lee lo que haya, sin bloquear. Devuelve False si el broker cerro."""
    global _buf, _ultimo_rx_ms
    while True:
        try:
            datos = _sock.read(512)
        except OSError as e:
            # EAGAIN / "no hay datos": normal en modo no bloqueante.
            if e.args and e.args[0] in (11, 110, -26880, -0x6900):
                return True
            raise
        if datos is None:
            return True
        if datos == b"":
            return False
        _buf += datos
        _ultimo_rx_ms = ticks_ms()
        if len(datos) < 512:
            return True


def _procesar():
    """Saca del buffer los paquetes completos y atiende los que importan."""
    global _buf
    while len(_buf) >= 2:
        cab = _buf[0]
        tipo = cab >> 4
        n, mult, i = 0, 1, 1
        while True:
            if i >= len(_buf):
                return                      # largo incompleto: esperar mas
            d = _buf[i]
            n += (d & 0x7F) * mult
            mult *= 128
            i += 1
            if not d & 0x80:
                break
        if len(_buf) < i + n:
            return                          # paquete incompleto
        cuerpo = _buf[i:i + n]
        _buf = _buf[i + n:]
        if tipo == 4 and n >= 2:            # PUBACK
            _acusados.add(struct.unpack("!H", cuerpo[:2])[0])
        elif tipo == 3 and n >= 2:          # PUBLISH que llega (suscripciones)
            lt = struct.unpack("!H", cuerpo[:2])[0]
            topic = bytes(cuerpo[2:2 + lt]).decode()
            resto = cuerpo[2 + lt:]
            if (cab >> 1) & 3:              # QoS 1/2: viene con packet id
                resto = resto[2:]
            if al_mensaje is not None:
                try:
                    al_mensaje(topic, bytes(resto))
                except Exception as e:      # un mensaje malo no tumba el enlace
                    _log("mensaje", topic, "descartado:", e)
        # SUBACK (9), PINGRESP (13) y otros: solo cuentan como "el broker esta vivo".


def _conectar(ahora):
    """Abre TCP + TLS y manda CONNECT. Bloquea (handshake): solo en IDLE."""
    global _sock, _crudo, _buf, _conectado, _enviados, _acusados
    global _espera_ms, _proximo_intento_ms, _ultimo_rx_ms, _desconectado_desde, _ultimo_error
    global _conectado_desde, _conexion_id
    gc.collect()   # el handshake TLS necesita un bloque grande de heap contiguo
    inicio = ticks_ms()
    try:
        direccion = _direccion_ipv4(config.MQTT_HOST, config.MQTT_PORT)
        _crudo = socket.socket()
        _crudo.settimeout(config.T_CONEXION_MQTT_S)
        _crudo.connect(direccion)
        _sock = _envolver_tls(_crudo, config.MQTT_HOST)
        _sock.write(_paquete_connect())
        # CONNACK: 0x20 0x02 <flags> <rc>
        resp = b""
        while len(resp) < 4:
            parte = _sock.read(4 - len(resp))
            if not parte:
                raise OSError("sin CONNACK")
            resp += parte
        if resp[0] != 0x20 or resp[3] != 0:
            raise OSError("CONNACK rechazado rc={}".format(resp[3]))
        if SUSCRIPCIONES:
            _sock.write(_paquete_subscribe(_siguiente_pid(), SUSCRIPCIONES))
    except Exception as e:
        _ultimo_error = repr(e)
        _log("no conecta:", e, "| reintento en", _espera_ms // 1000, "s")
        _cerrar_socket()
        _proximo_intento_ms = ticks_add(ahora, _espera_ms)
        _espera_ms = min(_espera_ms * 2, config.T_REINTENTO_MQTT_MAX_MS)
        return False
    _crudo.setblocking(False)
    _buf = b""
    _enviados, _acusados = [], set()
    _conectado = True
    _conectado_desde = ticks_ms()
    _conexion_id += 1
    _espera_ms = 1000
    _desconectado_desde = None
    _ultimo_error = None
    _ultimo_rx_ms = ticks_ms()
    _log("conectado a", config.MQTT_HOST, "en", ticks_diff(ticks_ms(), inicio), "ms")
    return True


def _cerrar_socket():
    global _sock, _crudo
    for s in (_sock, _crudo):
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    _sock = _crudo = None


def cerrar(motivo="pedido"):
    """Corta el enlace (p. ej. para liberar RAM antes de un HTTPS)."""
    global _conectado, _desconectado_desde
    if _conectado:
        _log("desconectado:", motivo)
    _cerrar_socket()
    if _conectado:
        _desconectado_desde = ticks_ms()
    _conectado = False


def _caida(ahora, motivo):
    global _proximo_intento_ms, _ultimo_error
    _ultimo_error = motivo
    cerrar(motivo)
    _proximo_intento_ms = ticks_add(ahora, _espera_ms)


# --- Trabajo por vuelta del bucle ------------------------------------------

def _topic(sufijo):
    return "panol/{}/{}/{}".format(config.UBICACION_ID, config.NODO_ID, sufijo)


def _drenar_cola():
    """Publica el lote pendiente y confirma en flash lo que el broker acuso."""
    global _enviados, _acusados
    if _enviados:
        # Confirmar el prefijo acusado, en orden (la cola es FIFO).
        n = 0
        for pid in _enviados:
            if pid not in _acusados:
                break
            n += 1
        if n:
            cola.confirmar(n)
            _enviados = _enviados[n:]
            _acusados = set(p for p in _acusados if p in _enviados)
        if _enviados:
            return                          # lote en vuelo: esperar acuses
    lote = cola.pendientes(config.VENTANA_MQTT)
    for evento in lote:
        pid = _siguiente_pid()
        _escribir(_paquete_publish(_topic("evento/" + evento.get("tipo", "otro")),
                                   json.dumps(evento), 1, pid))
        _enviados.append(pid)


def _latido(ahora, rssi, degradado):
    global _ultimo_hb_ms
    if _ultimo_hb_ms is not None and ticks_diff(ahora, _ultimo_hb_ms) < config.T_HEARTBEAT_MS:
        return
    _ultimo_hb_ms = ahora
    _escribir(_paquete_publish(_topic("heartbeat"), json.dumps({
        "nodo_id": config.NODO_ID, "ubicacion_id": config.UBICACION_ID, "rol": "puerta",
        "uptime": ticks_ms() // 1000, "rssi": rssi, "modo_degradado": degradado,
    }), 0))


def tarea(ahora, rssi=None, degradado=False):
    """Una vuelta de trabajo de enlace. Llamar solo con WiFi y en IDLE.

    Devuelve True si hay enlace al salir.
    """
    global _desconectado_desde
    if not _conectado:
        if _desconectado_desde is None:
            _desconectado_desde = ahora
        if ticks_diff(ahora, _proximo_intento_ms) < 0:
            return False
        # Con verificacion de certificado hace falta la hora (vigencia).
        if config.MQTT_VERIFICAR_TLS and not reloj.sincronizado():
            return False
        if not _conectar(ahora):
            return False
    try:
        if not _leer_disponible():
            _caida(ahora, "el broker cerro la conexion")
            return False
        _procesar()
        if ticks_diff(ahora, _ultimo_rx_ms) > config.MQTT_KEEPALIVE_S * 1500:
            _caida(ahora, "sin respuesta del broker")
            return False
        if ticks_diff(ahora, _ultimo_tx_ms) > config.MQTT_KEEPALIVE_S * 500:
            _escribir(b"\xc0\x00")          # PINGREQ
        _latido(ahora, rssi, degradado)
        _drenar_cola()
    except Exception as e:
        _caida(ahora, repr(e))
        return False
    return True
