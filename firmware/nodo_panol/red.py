# Puente de red del nodo: WiFi, envio de eventos y refresco de whitelist.
#
# Principio de diseño (DISEÑO seccion 4, opcion B): la red NUNCA esta en el
# camino critico de abrir la puerta. El nodo decide la autorizacion contra
# su whitelist local y solo REPORTA. Un corte de red no demora un acceso
# legitimo, y el "modo degradado" deja de ser un caso especial.
#
# Consecuencia practica: todas las llamadas de este modulo tienen timeout
# corto y solo se ejecutan con la FSM en IDLE. Y si algo falla, se espera
# T_REINTENTO_RED_MS antes de reintentar, para no pagar un timeout en cada
# vuelta del bucle mientras el servidor este caido.

from time import ticks_ms, ticks_diff, ticks_add

import config
import cola
import reloj

try:
    import urequests as requests
except ImportError:  # MicroPython reciente
    import requests

_wlan = None
_degradado = True          # Hasta probar lo contrario, se asume degradado
_proximo_intento_ms = 0    # Backoff tras un fallo
_ultimo_heartbeat_ms = None
_ultima_whitelist_ms = None
_asociando_desde_ms = None   # Cuando empezo el intento de asociacion en curso
_proximo_wifi_ms = 0         # Backoff entre intentos de reasociacion
_proximo_ntp_ms = None       # Cuando toca (re)sincronizar el reloj


def _log(*args):
    print("[RED]", *args)


# --- WiFi -----------------------------------------------------------------


def conectar():
    """Asocia a la WiFi. Bloquea, pero solo al bootear: ahi es inofensivo.

    La puerta esta trabada y no hay nadie esperando una decision, asi que
    conviene arrancar con red si se puede.
    """
    global _wlan
    if not config.USAR_RED:
        _log("deshabilitada por configuracion (USAR_RED = False)")
        return False

    import network

    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(True)
    if not _wlan.isconnected():
        _log("conectando a", config.WIFI_SSID)
        _wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
        inicio = ticks_ms()
        while (not _wlan.isconnected()
               and ticks_diff(ticks_ms(), inicio) < config.T_CONEXION_WIFI_MS):
            pass

    if _wlan.isconnected():
        _log("conectado, IP", _wlan.ifconfig()[0])
        return True
    _log("no se pudo conectar; el nodo sigue funcionando en modo degradado")
    return False


def conectada():
    return bool(_wlan and _wlan.isconnected())


def _asegurar_wifi(ahora):
    """Reasocia si se cayo el WiFi. NO bloquea. Devuelve True si hay red.

    `conectar()` corre una sola vez, al bootear. Sin esto, un AP que se
    reinicia (o un nodo que arranca antes que el router) deja al nodo mudo
    hasta el proximo reset: reportaria a la cola para siempre y nadie se
    enteraria salvo por la falta de heartbeats.

    Es no bloqueante a proposito: se lanza el intento y se vuelve al bucle.
    La asociacion tarda segundos y este codigo corre entre lecturas de
    sensores; esperar aca seria meter la red en el camino de la puerta, que
    es justo lo que el diseño prohibe.
    """
    global _wlan, _asociando_desde_ms, _proximo_wifi_ms, _ultima_whitelist_ms

    if _wlan is None:
        # Solo pasa si `conectar()` no llego a correr. Se crea la interfaz en
        # vez de rendirse: si no, el nodo quedaria mudo sin siquiera intentar.
        import network

        _wlan = network.WLAN(network.STA_IF)

    if _wlan.isconnected():
        if _asociando_desde_ms is not None:
            _log("reasociado, IP", _wlan.ifconfig()[0])
            _asociando_desde_ms = None
            # Al volver la red, la whitelist local puede estar vieja: se fuerza
            # un refresco en la proxima vuelta en vez de esperar los 15 min.
            _ultima_whitelist_ms = None
        return True

    # Hay un intento en curso: darle su ventana antes de reintentar.
    if (_asociando_desde_ms is not None
            and ticks_diff(ahora, _asociando_desde_ms) < config.T_CONEXION_WIFI_MS):
        return False

    if ticks_diff(ahora, _proximo_wifi_ms) < 0:
        return False

    _log("sin WiFi, reasociando a", config.WIFI_SSID)
    try:
        _wlan.active(True)
        # Un disconnect() antes de reintentar saca al driver de un estado de
        # asociacion a medias, que es donde se queda cuando el AP desaparece.
        try:
            _wlan.disconnect()
        except OSError:
            pass
        _wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
    except OSError as e:
        _log("no se pudo lanzar la reasociacion:", e)

    _asociando_desde_ms = ahora
    _proximo_wifi_ms = ticks_add(ahora, config.T_REINTENTO_WIFI_MS)
    return False


def _asegurar_hora(ahora):
    """Pone el nodo en hora cuando hay red, y lo mantiene en hora.

    Dos casos que el arranque no cubre: bootear sin WiFi (ahi `sincronizar()`
    ni se intenta, y todos los eventos saldrian sin timestamp para siempre) y
    la deriva del reloj interno, que no tiene pila ni cristal de precision.
    En la cola offline el timestamp es lo unico que permite atribuir un evento
    a la sesion correcta, asi que la hora no es un lujo.
    """
    global _proximo_ntp_ms

    if _proximo_ntp_ms is None:
        # Primera vuelta con red. Si el arranque ya puso el nodo en hora, se
        # agenda el proximo resync; si no (booteo sin WiFi), se intenta ya.
        _proximo_ntp_ms = (ticks_add(ahora, config.T_RESYNC_NTP_MS)
                           if reloj.sincronizado() else ahora)

    if ticks_diff(ahora, _proximo_ntp_ms) < 0:
        return

    # Si falla se espera lo mismo que tras un fallo de HTTP, para no pagar el
    # timeout del NTP en cada vuelta del bucle.
    espera = (config.T_RESYNC_NTP_MS if reloj.sincronizar()
              else config.T_REINTENTO_RED_MS)
    _proximo_ntp_ms = ticks_add(ahora, espera)


def degradado():
    """True si el nodo no logra hablar con el servidor.

    Con la opcion B esto NO significa "no puedo decidir" — el nodo decide
    igual. Significa "no puedo refrescar la whitelist ni reportar ahora".
    """
    return _degradado


def rssi():
    try:
        return _wlan.status("rssi") if conectada() else None
    except Exception:
        return None


# --- HTTP ----------------------------------------------------------------


def _post(ruta, cuerpo):
    """POST con timeout corto. Devuelve el JSON de respuesta o None."""
    global _degradado, _proximo_intento_ms

    if not conectada():
        _degradado = True
        return None

    url = config.SERVER_URL + ruta
    r = None
    try:
        r = requests.post(url, json=cuerpo, timeout=config.T_TIMEOUT_HTTP_S)
        if r.status_code != 200:
            _log("HTTP", r.status_code, "en", ruta)
            _degradado = True
            _proximo_intento_ms = ticks_add(ticks_ms(), config.T_REINTENTO_RED_MS)
            return None
        _degradado = False
        return r.json()
    except Exception as e:
        _log("fallo", ruta, "->", e)
        _degradado = True
        _proximo_intento_ms = ticks_add(ticks_ms(), config.T_REINTENTO_RED_MS)
        return None
    finally:
        # Sin close() se filtran sockets y a las pocas horas el nodo se
        # queda sin memoria. Es la fuga clasica de urequests.
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _get(ruta):
    global _degradado, _proximo_intento_ms

    if not conectada():
        _degradado = True
        return None

    r = None
    try:
        r = requests.get(config.SERVER_URL + ruta, timeout=config.T_TIMEOUT_HTTP_S)
        if r.status_code != 200:
            _degradado = True
            _proximo_intento_ms = ticks_add(ticks_ms(), config.T_REINTENTO_RED_MS)
            return None
        _degradado = False
        return r.json()
    except Exception as e:
        _log("fallo", ruta, "->", e)
        _degradado = True
        _proximo_intento_ms = ticks_add(ticks_ms(), config.T_REINTENTO_RED_MS)
        return None
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


# --- Tareas periodicas ---------------------------------------------------


def _vaciar_cola():
    """Manda un lote de eventos pendientes y trunca solo lo confirmado.

    El truncado usa confirmados + rechazados. Los rechazados son eventos
    mal formados que el servidor nunca va a aceptar: si se dejaran en la
    cola quedarian atascados al frente para siempre y bloquearian todo lo
    que viene atras. Es el problema del "mensaje veneno".
    """
    lote = cola.pendientes(config.LOTE_COLA)
    if not lote:
        return

    respuesta = _post("/api/eventos", {"eventos": lote})
    if respuesta is None:
        _log(len(lote), "eventos siguen pendientes (sin servidor)")
        return

    confirmados = respuesta.get("confirmados", [])
    rechazados = respuesta.get("rechazados", [])
    if rechazados:
        _log(len(rechazados), "eventos rechazados y descartados:", rechazados)

    terminados = min(len(confirmados) + len(rechazados), len(lote))
    cola.confirmar(terminados)
    _log("sincronizados", terminados, "de", len(lote),
         "| quedan", cola.largo())


def _heartbeat():
    _post(
        "/api/heartbeat",
        {
            "nodo_id": config.NODO_ID,
            "ubicacion_id": config.UBICACION_ID,
            "rol": "puerta",
            "uptime": ticks_ms() // 1000,
            "rssi": rssi(),
            "modo_degradado": _degradado,
        },
    )


def _refrescar_whitelist(al_actualizar):
    """Baja la lista blanca del servidor y la deja en flash.

    Se guarda SEPARADA de uids.txt a proposito: uids.txt son las tarjetas
    grabadas con el boton, en el propio pañol. Si el refresco pisara ese
    archivo, cada 15 minutos se perderian las altas locales.
    """
    respuesta = _get("/api/whitelist")
    if respuesta is None:
        return
    uids = respuesta.get("uids", [])
    try:
        with open(config.ARCHIVO_WHITELIST, "w") as f:
            for uid in uids:
                f.write(uid + "\n")
    except OSError as e:
        _log("no se pudo guardar la whitelist:", e)
        return
    _log("whitelist actualizada:", len(uids), "UIDs")
    al_actualizar()


def tareas(ahora, al_actualizar_whitelist):
    """Trabajo de red pendiente. Llamar SOLO con la FSM en IDLE.

    Esa restriccion es la que garantiza que un timeout de 3 s nunca caiga
    en el medio del pulso del solenoide ni de la ventana de la promesa.
    """
    global _ultimo_heartbeat_ms, _ultima_whitelist_ms

    if not config.USAR_RED:
        return
    # Lo primero es tener WiFi. Antes esto era un `return` seco: si la red se
    # caia despues del arranque, el nodo no volvia a intentar nunca.
    if not _asegurar_wifi(ahora):
        return
    # Con red, mantener la hora. Va antes del backoff de HTTP porque un
    # servidor caido no tiene por que dejar al nodo fuera de hora.
    _asegurar_hora(ahora)
    # Backoff: mientras el servidor no responda, no se reintenta en cada
    # vuelta. Si no, el bucle principal se comeria un timeout cada 50 ms.
    if ticks_diff(ahora, _proximo_intento_ms) < 0:
        return

    if (_ultima_whitelist_ms is None
            or ticks_diff(ahora, _ultima_whitelist_ms) >= config.T_REFRESCO_WHITELIST_MS):
        _ultima_whitelist_ms = ahora
        _refrescar_whitelist(al_actualizar_whitelist)

    if (_ultimo_heartbeat_ms is None
            or ticks_diff(ahora, _ultimo_heartbeat_ms) >= config.T_HEARTBEAT_MS):
        _ultimo_heartbeat_ms = ahora
        _heartbeat()

    _vaciar_cola()
