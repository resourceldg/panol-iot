"""Telemetría del pañol en ThingsBoard, para el tablero "Pañol IoT · LAB01".

Corre DENTRO del servidor (servicio sync-tb), leyendo la base local: sin
scripts en una notebook ni SSH. Cada vuelta toma lo que entró desde la última
y lo publica en el dispositivo de ThingsBoard de cada ubicación
("panol/<ubicacion>"), con la hora REAL de cada evento:

    puerta (1/0) · pir + pir_evento · armarioN · acceso + acceso_<resultado>
    sesion, sesion_nueva, sesion_usuario, sesion_rol, sesion_inicio, fin_sesion
    alarma + alarma_evento + alarma_<código> (1 al aparecer, 0 al minuto)
    nodo_en_linea, rssi, modo_degradado (del último latido)

Es el contrato de proyectos/panol-lab01 (repo homelab). Solo lee la base.
"""
import time
import urllib.error
import urllib.parse
from datetime import timedelta

from db import repositorio

HORAS_INICIALES = 24       # al arrancar: historia para que el tablero no nazca vacío
_ultimo = {}               # tabla -> último id publicado
_sesiones = {}             # id -> "EN_CURSO" | "CERRADA" ya publicado
_limpiar = []              # (epoch, ubicacion, clave) alarmas a dar por resueltas
_abiertos = {}             # (ubicacion, armario) -> epoch de apertura
_dispositivos = {}         # nombre -> id en ThingsBoard
_pir_vistos = {}           # event_id -> epoch (PIR con sesión ya publicadas)
_estado = {"primera": True}


def _ms(ts):
    return int(ts.timestamp() * 1000)


def _nuevos(conn, tabla, columnas):
    desde = repositorio.ahora() - timedelta(hours=HORAS_INICIALES)
    filas = conn.execute(
        f"SELECT id, ubicacion_id, timestamp, {columnas} FROM {tabla}"
        " WHERE id > %s AND timestamp > %s ORDER BY id",
        (_ultimo.get(tabla, 0), desde)).fetchall()
    if filas:
        _ultimo[tabla] = filas[-1]["id"]
    return filas


def _dispositivo(tb, ubicacion):
    nombre = "panol/" + ubicacion.replace("panol-", "").upper()
    if nombre not in _dispositivos:
        try:
            r = tb.req("GET", "/api/tenant/devices?deviceName=" + urllib.parse.quote(nombre))
        except urllib.error.HTTPError as e:
            if e.code != 404:              # ThingsBoard responde 404 si no existe
                raise
            r = None
        if not r:
            perfiles = tb.req("GET", "/api/deviceProfiles?pageSize=100&page=0&textSearch=panol")["data"]
            perfil = next((p for p in perfiles if p["name"] == "panol"), None)
            cuerpo = {"name": nombre, "label": "Nodo del pañol " + ubicacion}
            if perfil:
                cuerpo["deviceProfileId"] = perfil["id"]
            r = tb.req("POST", "/api/device", cuerpo)
        _dispositivos[nombre] = r["id"]["id"]
    return _dispositivos[nombre]


def vuelta(conn, tb) -> int:
    """Publica lo nuevo. Devuelve cuántos eventos se publicaron."""
    lotes = {}                                    # ubicacion -> [ {ts, values} ]

    def pub(ubic, ts_ms, **valores):
        lotes.setdefault(ubic, []).append({"ts": ts_ms, "values": valores})

    for f in _nuevos(conn, "eventos_puerta", "estado_reed"):
        pub(f["ubicacion_id"], _ms(f["timestamp"]), puerta=1 if f["estado_reed"] == "ABIERTO" else 0)
    for f in _nuevos(conn, "eventos_pir", "sesion_id"):
        pub(f["ubicacion_id"], _ms(f["timestamp"]), pir=1, pir_evento=1)
    for f in _nuevos(conn, "eventos_armario", "armario_id"):
        pub(f["ubicacion_id"], _ms(f["timestamp"]), **{f"armario{f['armario_id']}": 1})
        _abiertos[(f["ubicacion_id"], f["armario_id"])] = f["timestamp"].timestamp()
    for f in _nuevos(conn, "eventos_acceso",
                     "resultado, coalesce((SELECT trim(u.nombre || ' ' || coalesce(u.apellido, ''))"
                     " FROM credenciales c JOIN usuarios u ON u.id = c.usuario_id"
                     " WHERE c.uid_hex = eventos_acceso.uid_hex AND c.activa LIMIT 1), uid_hex) AS persona"):
        r = f["resultado"]
        pub(f["ubicacion_id"], _ms(f["timestamp"]), acceso=r, acceso_usuario=f["persona"],
            **{"acceso_" + r.lower(): 1})
    for f in _nuevos(conn, "alarmas", "codigo, anulada"):
        if f["anulada"]:
            continue
        clave = "alarma_" + f["codigo"].lower()
        pub(f["ubicacion_id"], _ms(f["timestamp"]), alarma=f["codigo"], alarma_evento=1, **{clave: 1})
        _limpiar.append((f["timestamp"].timestamp() + 60, f["ubicacion_id"], clave))

    # Las PIR CON sesión no quedan en eventos_pir (son solo actividad, ver el
    # motor): para el tablero salen de eventos_procesados.
    for f in conn.execute(
            "SELECT p.event_id, p.ocurrido, s.ubicacion_id FROM eventos_procesados p"
            " JOIN nodos n ON n.id = p.nodo_id JOIN sesiones s ON s.ubicacion_id = n.ubicacion_id"
            "  AND s.hora_inicio <= p.ocurrido AND (s.hora_fin IS NULL OR s.hora_fin > p.ocurrido)"
            " WHERE p.tipo = 'pir' AND p.ocurrido IS NOT NULL AND p.recibido > %s",
            (repositorio.ahora() - timedelta(hours=HORAS_INICIALES) if _estado["primera"]
             else repositorio.ahora() - timedelta(minutes=10),)).fetchall():
        if f["event_id"] not in _pir_vistos:
            _pir_vistos[f["event_id"]] = time.time()
            pub(f["ubicacion_id"], _ms(f["ocurrido"]), pir=1, pir_evento=1)

    desde = repositorio.ahora() - timedelta(hours=HORAS_INICIALES)
    for s in conn.execute(
            "SELECT s.id, s.ubicacion_id, s.hora_inicio, s.hora_fin, s.motivo_cierre,"
            " coalesce(trim(u.nombre || ' ' || coalesce(u.apellido, '')), s.uid_hex) AS persona,"
            " coalesce(u.rol, '') AS rol FROM sesiones s LEFT JOIN usuarios u ON u.id = s.usuario_id"
            " WHERE s.hora_inicio > %s OR s.estado = 'EN_CURSO'", (desde,)).fetchall():
        previo = _sesiones.get(s["id"])
        if previo is None:
            pub(s["ubicacion_id"], _ms(s["hora_inicio"]), sesion=1, sesion_nueva=1,
                sesion_usuario=s["persona"], sesion_rol=s["rol"].capitalize(),
                sesion_inicio=_ms(s["hora_inicio"]))
            _sesiones[s["id"]] = "EN_CURSO"
        if s["hora_fin"] and _sesiones.get(s["id"]) != "CERRADA":
            pub(s["ubicacion_id"], _ms(s["hora_fin"]), sesion=0, fin_sesion=s["motivo_cierre"] or "")
            _sesiones[s["id"]] = "CERRADA"

    ahora = time.time()
    for item in [x for x in _limpiar if x[0] <= ahora]:
        pub(item[1], int(ahora * 1000), **{item[2]: 0, "alarma": "OK"})
        _limpiar.remove(item)
    for (ubic, n), t0 in list(_abiertos.items()):
        if ahora - t0 > 20:
            pub(ubic, int(ahora * 1000), **{f"armario{n}": 0})
            del _abiertos[(ubic, n)]
    for n in conn.execute("SELECT ubicacion_id, rssi, modo_degradado,"
                          " extract(epoch FROM now() - ultimo_heartbeat) AS hace FROM nodos").fetchall():
        pub(n["ubicacion_id"], int(ahora * 1000),
            nodo_en_linea=1 if n["hace"] is not None and n["hace"] < 150 else 0,
            rssi=n["rssi"], modo_degradado=1 if n["modo_degradado"] else 0)
    for k in [k for k, t in _pir_vistos.items() if ahora - t > 3600]:
        del _pir_vistos[k]

    _estado["primera"] = False
    publicados = 0
    for ubic, lote in lotes.items():
        lote.sort(key=lambda x: x["ts"])
        did = _dispositivo(tb, ubic)
        for i in range(0, len(lote), 500):
            tb.req("POST", f"/api/plugins/telemetry/DEVICE/{did}/timeseries/ANY", lote[i:i + 500])
        publicados += sum(1 for x in lote if "nodo_en_linea" not in x["values"])
    return publicados

