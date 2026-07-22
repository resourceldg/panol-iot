# Cola de eventos persistente en flash.
#
# La spec v1.0 encola en RAM; aca se encola SIEMPRE en flash. El motivo es
# el caso que mas importa: un corte de energia borra la RAM y con ella la
# auditoria. Un archivo append-only sobrevive al apagon y es mas barato que
# una base de datos en el nodo.
#
# Cada linea es un JSON. El archivo se trunca solo cuando el servidor
# confirma (ACK), nunca antes de saber que el evento llego.

import json
import config


def _leer_lineas():
    try:
        with open(config.ARCHIVO_COLA) as f:
            return [l for l in f.read().split("\n") if l.strip()]
    except OSError:
        return []


def encolar(evento):
    """Agrega un evento al final de la cola. Devuelve True si pudo escribir."""
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
    """Devuelve hasta `limite` eventos, en orden de llegada."""
    lineas = _leer_lineas()[:limite]
    eventos = []
    for l in lineas:
        try:
            eventos.append(json.loads(l))
        except ValueError:
            # Linea truncada por un corte de energia justo al escribirla.
            # Se descarta: es un solo evento y no debe trabar toda la cola.
            print("[COLA] linea corrupta descartada")
    return eventos


def confirmar(cantidad):
    """Borra los primeros `cantidad` eventos, ya confirmados por el servidor.

    Reescribe el archivo completo. Con colas chicas (decenas de eventos) es
    mas simple y mas seguro que manipular offsets, y se ejecuta pocas veces.
    """
    if cantidad <= 0:
        return
    resto = _leer_lineas()[cantidad:]
    try:
        with open(config.ARCHIVO_COLA, "w") as f:
            for l in resto:
                f.write(l + "\n")
    except OSError as e:
        print("[COLA] no se pudo truncar:", e)


def largo():
    return len(_leer_lineas())
