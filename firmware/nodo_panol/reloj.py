# Hora real del nodo.
#
# El ESP32 no tiene RTC con pila: al bootear no sabe que hora es. Eso
# importa para la cola offline, donde un evento puede reportarse horas
# despues de ocurrir y el timestamp es lo unico que permite atribuirlo a la
# sesion correcta.
#
# Politica: se intenta NTP al bootear. Si hay hora, los eventos viajan con
# su timestamp real. Si no la hay, viajan SIN timestamp y el servidor usa el
# de llegada. Es peor, pero es honesto: preferible una hora aproximada y
# declarada como tal antes que una hora inventada en un registro de
# auditoria.

import time

import config

# Argentina no tiene horario de verano, asi que alcanza un offset fijo.
OFFSET_S = -3 * 3600
SUFIJO_TZ = "-03:00"

_sincronizado = False


def sincronizar():
    """Pide la hora por NTP. Devuelve True si quedo en hora.

    Se llama al bootear y despues cada tanto desde `red.tareas()`, que corre
    con la FSM en reposo. Por eso el timeout importa: con el valor por defecto
    de MicroPython, un NTP inalcanzable bloquea el bucle decenas de segundos y
    en ese rato el nodo no mira ni el reed ni el lector.
    """
    global _sincronizado
    try:
        import ntptime

        try:
            ntptime.timeout = config.T_TIMEOUT_NTP_S
        except AttributeError:
            # Puertos viejos de MicroPython no exponen el timeout. Se sigue
            # igual: mejor la hora con riesgo de demora que sin hora.
            pass
        ntptime.settime()
        # Sanidad: una respuesta NTP corrupta (o un rollover de era) puede
        # devolver una fecha imposible, como el 2036 que se vio en el banco.
        # Aceptarla corrompe TODOS los timestamps del arranque, y como el
        # timestamp es lo que atribuye cada evento a su sesion, es peor que no
        # tener hora. Si el año no es creible, se descarta y se reporta sin
        # timestamp (el servidor pone el de llegada).
        anio = time.gmtime()[0]
        if not (2024 <= anio <= 2035):
            _sincronizado = False
            print("[RELOJ] NTP devolvio un año imposible (", anio,
                  "); se descarta y se usaran timestamps del servidor")
        else:
            _sincronizado = True
            print("[RELOJ] en hora por NTP:", ahora_iso())
    except Exception as e:
        # Sin red, sin DNS o servidor NTP caido. No es fatal.
        _sincronizado = False
        print("[RELOJ] sin NTP:", e, "- se usaran timestamps del servidor")
    return _sincronizado


def sincronizado():
    return _sincronizado


def hora_local():
    """Hora argentina "HH:MM:SS" para los logs, o None si no hay NTP.

    En banco (sin red) devuelve None y el log cae al uptime.
    """
    if not _sincronizado:
        return None
    t = time.localtime(time.time() + OFFSET_S)
    return "%02d:%02d:%02d" % (t[3], t[4], t[5])


def ahora_iso():
    """Timestamp ISO-8601 con offset, o None si el nodo no esta en hora.

    Devolver None a proposito, en vez de una fecha del año 2000, para que el
    servidor sepa que tiene que poner la hora de llegada.
    """
    if not _sincronizado:
        return None
    t = time.localtime(time.time() + OFFSET_S)
    return "%04d-%02d-%02dT%02d:%02d:%02d%s" % (
        t[0], t[1], t[2], t[3], t[4], t[5], SUFIJO_TZ,
    )
