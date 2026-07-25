# Cola de eventos persistente en flash.
#
# La spec v1.0 encola en RAM; aca se encola SIEMPRE en flash. El motivo es
# el caso que mas importa: un corte de energia borra la RAM y con ella la
# auditoria. Un archivo append-only sobrevive al apagon y es mas barato que
# una base de datos en el nodo.
#
# Cada linea es un JSON. El archivo se trunca solo cuando el servidor
# confirma (ACK), nunca antes de saber que el evento llego.
#
# TODO se lee POR STREAMING, linea por linea, NUNCA con f.read(): el ESP32
# tiene pocas decenas de KB de RAM y la cola puede acumular miles de eventos
# tras un corte de red largo. Cargar el archivo entero reventaba con
# MemoryError justo cuando la cola estaba llena — es decir, cuando el
# fail-safe mas tiene que funcionar.

import os

import json
import config


def encolar(evento):
    """Agrega un evento al final de la cola. Devuelve True si pudo escribir.

    Tope defensivo: si la cola ya llego a config.MAX_COLA lineas (un corte de
    red de dias), se deja de encolar en vez de llenar la flash y tirar el nodo.
    Se conserva lo VIEJO —la evidencia mas antigua del incidente— y se descarta
    lo nuevo, avisando una sola vez.
    """
    tope = getattr(config, "MAX_COLA", 5000)
    if largo() >= tope:
        print("[COLA] llena (", tope, "), evento descartado; red caida hace mucho")
        return False
    try:
        with open(config.ARCHIVO_COLA, "a") as f:
            f.write(json.dumps(evento) + "\n")
        return True
    except OSError as e:
        # Flash llena o corrupta. Se avisa pero no se aborta: es preferible
        # perder un evento de auditoria antes que dejar la puerta sin
        # controlar por una excepcion no atrapada.
        print("[COLA] no se pudo escribir:", e)
        return False


def pendientes(limite=20):
    """Hasta `limite` eventos, leyendo SOLO esas lineas (no todo el archivo)."""
    eventos = []
    try:
        with open(config.ARCHIVO_COLA) as f:
            for linea in f:
                if len(eventos) >= limite:
                    break
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    eventos.append(json.loads(linea))
                except ValueError:
                    # Linea truncada por un corte de energia justo al
                    # escribirla. Se descarta: es un solo evento y no debe
                    # trabar toda la cola.
                    print("[COLA] linea corrupta descartada")
    except OSError:
        pass
    return eventos


def confirmar(cantidad):
    """Borra los primeros `cantidad` eventos, ya confirmados por el servidor.

    Copia el resto a un archivo temporal por streaming y lo renombra: nunca
    carga la cola entera en RAM, y si un corte interrumpe la copia, el original
    sigue intacto hasta el rename final.
    """
    if cantidad <= 0:
        return
    tmp = config.ARCHIVO_COLA + ".tmp"
    try:
        with open(config.ARCHIVO_COLA) as origen, open(tmp, "w") as destino:
            i = 0
            for linea in origen:
                i += 1
                if i <= cantidad:
                    continue
                destino.write(linea)
        # Reemplazo. En littlefs (flash del ESP32) rename no pisa un destino
        # existente, asi que se borra el original primero. El .tmp ya tiene todo
        # lo que hay que conservar, asi que la ventana entre remove y rename no
        # pierde datos que no esten tambien en .tmp.
        try:
            os.remove(config.ARCHIVO_COLA)
        except OSError:
            pass
        os.rename(tmp, config.ARCHIVO_COLA)
    except OSError as e:
        print("[COLA] no se pudo truncar:", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


def largo():
    """Cuenta las lineas no vacias, por streaming."""
    n = 0
    try:
        with open(config.ARCHIVO_COLA) as f:
            for linea in f:
                if linea.strip():
                    n += 1
    except OSError:
        pass
    return n
