"""Gestión de credenciales desde ThingsBoard: la base manda, TB es la pantalla.

Quién puede entrar lo decide la base del pañol (es la que arma la whitelist
del nodo y la auditoría). ThingsBoard es la PANTALLA para gestionarlo:

  base del pañol ──(estado)──► TB: un asset "tarjeta-panol" por tarjeta,
                               con estado/persona/fechas como atributos
  TB (botones del tablero) ──(accion)──► base: aprobar / rechazar / baja

Cada vuelta (cada SYNC_TB_INTERVALO_S):
  1. Lee de cada asset el atributo `accion` (y `aprobar_para`) que escribió
     un botón del tablero, lo aplica con las MISMAS funciones que el comando
     `credenciales`, deja el resultado en `resultado` y borra el pedido.
  2. Publica el estado actual de la base: crea/actualiza los assets y borra
     los de tarjetas que ya no existen. Escribe solo lo que cambió.
  3. Publica la lista de personas activas (asset "panol-personas"), para que
     el tablero muestre a quién se le puede asignar una tarjeta.

ThingsBoard no puede autorizar a nadie por su cuenta: solo pide, y la base
valida (persona activa, tarjeta no duplicada...) igual que en la terminal.

    TB_URL=http://thingsboard:8080 TB_USUARIO=... TB_CLAVE=... \\
        python -m adapters.sync_tb            # una vuelta
        python -m adapters.sync_tb --loop     # servicio (también la telemetría
                                              # del tablero del pañol: telemetria_tb.py)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import credenciales as cred
from adapters import telemetria_tb
from db import repositorio

TB_URL = os.environ.get("TB_URL", "").rstrip("/")
TB_USUARIO = os.environ.get("TB_USUARIO", "")
TB_CLAVE = os.environ.get("TB_CLAVE", "")
INTERVALO_S = int(os.environ.get("SYNC_TB_INTERVALO_S", "10"))
# La telemetría del tablero del pañol va más seguido: es lo que se ve en vivo.
INTERVALO_TELEMETRIA_S = int(os.environ.get("SYNC_TB_INTERVALO_TELEMETRIA_S", "2"))
TIPO = "tarjeta-panol"
ASSET_PERSONAS = "panol-personas"
# Rechazadas y bajas se muestran un tiempo (para ver qué pasó) y después salen.
DIAS_HISTORIA = int(os.environ.get("SYNC_TB_DIAS_HISTORIA", "30"))


def log(*args):
    print("[SYNC-TB]", *args, flush=True)


def habilitado() -> bool:
    return bool(TB_URL and TB_USUARIO and TB_CLAVE)


class TB:
    def __init__(self):
        self.token = None

    def _login(self):
        self.token = self._req("POST", "/api/auth/login",
                               {"username": TB_USUARIO, "password": TB_CLAVE}, auth=False)["token"]

    def _req(self, metodo, ruta, cuerpo=None, auth=True):
        datos = None if cuerpo is None else json.dumps(cuerpo).encode()
        r = urllib.request.Request(TB_URL + ruta, data=datos, method=metodo,
                                   headers={"Content-Type": "application/json"})
        if auth:
            r.add_header("X-Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(r, timeout=20) as x:
            t = x.read().decode()
            return json.loads(t) if t else None

    def req(self, metodo, ruta, cuerpo=None):
        if self.token is None:
            self._login()
        try:
            return self._req(metodo, ruta, cuerpo)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self._login()                    # el token vence: nueva sesión
            return self._req(metodo, ruta, cuerpo)

    # --- assets y atributos ---
    def assets(self, tipo):
        out, pagina = [], 0
        while True:
            r = self.req("GET", f"/api/tenant/assets?pageSize=500&page={pagina}&type="
                         + urllib.parse.quote(tipo))
            out += r["data"]
            if not r.get("hasNext"):
                return out
            pagina += 1

    def crear_asset(self, nombre, tipo, etiqueta=""):
        return self.req("POST", "/api/asset", {"name": nombre, "type": tipo, "label": etiqueta})

    def borrar_asset(self, asset_id):
        self.req("DELETE", f"/api/asset/{asset_id}")

    def atributos(self, asset_id, claves):
        r = self.req("GET", f"/api/plugins/telemetry/ASSET/{asset_id}/values/attributes/SERVER_SCOPE"
                     "?keys=" + ",".join(claves))
        return {a["key"]: a["value"] for a in r or []}

    def escribir(self, asset_id, valores):
        self.req("POST", f"/api/plugins/telemetry/ASSET/{asset_id}/attributes/SERVER_SCOPE", valores)

    def borrar_atributos(self, asset_id, claves):
        self.req("DELETE", f"/api/plugins/telemetry/ASSET/{asset_id}/SERVER_SCOPE?keys="
                 + ",".join(claves))


def _ms(ts):
    return int(ts.timestamp() * 1000) if ts else None


def estado_deseado(conn) -> dict:
    """{uid: atributos} con lo que la base dice hoy de cada tarjeta."""
    desde = repositorio.ahora() - timedelta(days=DIAS_HISTORIA)
    out = {}
    for f in conn.execute(
            "SELECT c.uid_hex, c.baja, trim(coalesce(u.nombre, '') || ' ' || coalesce(u.apellido, '')) AS persona,"
            " u.email FROM credenciales c LEFT JOIN usuarios u ON u.id = c.usuario_id"
            " WHERE NOT c.activa AND c.baja > %s"
            "   AND NOT EXISTS (SELECT 1 FROM credenciales a WHERE a.uid_hex = c.uid_hex AND a.activa)"
            " ORDER BY c.baja", (desde,)).fetchall():
        out[f["uid_hex"]] = {"estado": "baja", "persona": f["persona"] or "", "email": f["email"] or "",
                             "desde": _ms(f["baja"]), "nodo": "", "veces": 0}
    for f in cred.listar_pendientes(conn, incluir_rechazadas=True):
        if f["rechazada"] and f["rechazada"] < desde:
            continue
        out[f["uid_hex"]] = {"estado": "rechazada" if f["rechazada"] else "pendiente", "persona": "",
                             "email": "", "desde": _ms(f["rechazada"] or f["primera_vez"]),
                             "nodo": f["nodo_id"] or "", "veces": f["veces"]}
    for f in cred.listar_activas(conn):
        out[f["uid_hex"]] = {"estado": "activa", "persona": f["persona"] or "", "email": f["email"] or "",
                             "desde": _ms(f["alta"]), "nodo": "", "veces": 0}
    return out


def aplicar_pedido(conn, uid, accion, para) -> str:
    """Lo que pidió un botón del tablero, con las reglas del comando credenciales."""
    try:
        if accion == "aprobar":
            return "OK: " + cred.aprobar(conn, uid, para or "")
        if accion == "rechazar":
            return "OK: " + cred.rechazar(conn, uid)
        if accion == "baja":
            return "OK: " + cred.baja(conn, uid)
        return f"ERROR: acción desconocida {accion!r}"
    except cred.ErrorCredencial as e:
        return f"ERROR: {e}"


_escrito = {}   # asset_id -> últimos atributos publicados (para no reescribir)


def vuelta(conn, tb: TB) -> dict:
    resumen = {"pedidos": 0, "creados": 0, "actualizados": 0, "borrados": 0}
    actuales = {a["name"]: a for a in tb.assets(TIPO)}

    # 1. Pedidos hechos desde el tablero.
    for uid, a in actuales.items():
        aid = a["id"]["id"]
        pedido = tb.atributos(aid, ["accion", "aprobar_para"])
        accion = str(pedido.get("accion") or "").strip().lower()
        if not accion:
            continue
        resultado = aplicar_pedido(conn, uid, accion, pedido.get("aprobar_para"))
        log(uid, accion, "->", resultado)
        tb.borrar_atributos(aid, ["accion", "aprobar_para"])
        tb.escribir(aid, {"resultado": resultado, "resultado_ts": _ms(repositorio.ahora())})
        resumen["pedidos"] += 1

    # 2. Estado de la base -> assets.
    deseado = estado_deseado(conn)
    for uid, attrs in deseado.items():
        a = actuales.get(uid)
        if a is None:
            a = tb.crear_asset(uid, TIPO, attrs["persona"] or attrs["estado"])
            resumen["creados"] += 1
        aid = a["id"]["id"]
        if _escrito.get(aid) != attrs:
            tb.escribir(aid, attrs)
            _escrito[aid] = dict(attrs)
            resumen["actualizados"] += 1
    for uid, a in actuales.items():
        if uid not in deseado:
            tb.borrar_asset(a["id"]["id"])
            _escrito.pop(a["id"]["id"], None)
            resumen["borrados"] += 1

    # 3. Personas a las que se puede asignar una tarjeta.
    personas = [{"nombre": f["persona"], "email": f["email"] or "", "rol": f["rol"] or ""}
                for f in conn.execute(
                    "SELECT trim(nombre || ' ' || coalesce(apellido, '')) AS persona, email, rol"
                    " FROM usuarios WHERE activo ORDER BY persona").fetchall()]
    p = next(iter(tb.assets(ASSET_PERSONAS)), None) or tb.crear_asset(ASSET_PERSONAS, ASSET_PERSONAS)
    pid = p["id"]["id"]
    valor = {"personas": json.dumps(personas, ensure_ascii=False)}
    if _escrito.get(pid) != valor:
        tb.escribir(pid, valor)
        _escrito[pid] = valor
    return resumen


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not habilitado():
        log("sin TB_URL/TB_USUARIO/TB_CLAVE: no se sincroniza")
        return
    conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
    tb = TB()
    if "--loop" not in argv:
        print(vuelta(conn, tb))
        return
    log("arrancado. ThingsBoard:", TB_URL, "| credenciales cada", INTERVALO_S,
        "s, telemetría cada", INTERVALO_TELEMETRIA_S, "s")
    ultima_cred = 0
    while True:
        try:
            n = telemetria_tb.vuelta(conn, tb)
            if n:
                log("telemetría:", n, "eventos")
            if time.time() - ultima_cred >= INTERVALO_S:
                ultima_cred = time.time()
                r = vuelta(conn, tb)
                if any(r.values()):
                    log(r)
        except Exception as e:                # TB o la base caídos: reintentar
            log("error:", repr(e))
            if getattr(conn, "closed", False):
                conn = repositorio.conectar(os.environ.get("PANOL_DSN"))
        time.sleep(INTERVALO_TELEMETRIA_S)


if __name__ == "__main__":
    main()
