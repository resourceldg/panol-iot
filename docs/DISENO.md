# Diseño — Sistema IoT de Pañol

> Documento de trabajo. Revisa y mejora la **máquina de estados** de la spec v1.0 y define la
> estrategia **a prueba de fallos** (foco: cortes de suministro), con el criterio:
> *profesional, robusto y a prueba de fallos de manera **sencilla**.*

---

## 0. Principio rector

**La seguridad ante fallos no se programa: se diseña en capas, de la más tonta a la más lista.**
La capa que te salva en un corte de luz debe ser la más simple (mecánica), no la más compleja (software).

Orden de confianza (lo de arriba nunca depende de lo de abajo):

| Capa | Mecanismo | Qué garantiza en un fallo total |
|---|---|---|
| 1. **Mecánica** | Picaporte interior siempre operativo | **Evacuación** siempre posible (Ley 19.587). No depende de nada eléctrico. |
| 2. **Eléctrica** | Solenoide *fail-secure* + pull-up externo en el relé | Sin energía: puerta **trabada desde afuera** (seguro), sin pulsos espurios al bootear. |
| 3. **Firmware** | Nodo casi sin estado + watchdog + cola persistente en flash | Reboot → estado seguro; no se pierden eventos de auditoría. |
| 4. **Servidor** | SQLite como única fuente de verdad + recuperación al reinicio | La sesión y las alarmas sobreviven al apagón. |

Regla de oro: **si todo se apaga, el sistema falla en el estado seguro por física, no por lógica.**

---

## 1. Dos máquinas de estados, no una

La spec dibuja una sola FSM, pero en realidad hay **dos**, y separarlas es la mejora más importante
del diseño: aclara qué es volátil, qué es persistente, y por qué el sistema se recupera solo.

### 1.1 FSM del **nodo pañol** (local, volátil, se recupera sola al bootear)

Vive en RAM del ESP32. **No es auditoría** — solo controla el hardware de la puerta. Si el nodo se
apaga, esta FSM se pierde y **no importa**: al bootear arranca en `IDLE` (seguro) y sigue reportando.

```
        ┌──────────────────────── (boot: relé en estado seguro, puerta trabada) ────────┐
        v                                                                                │
     [IDLE] --RFID leído--> [VALIDANDO] --autorizado--> [PULSO+PROMESA ≤10s] --reed abre--┘
        ^                        │                              │
        │                   no autorizado                  timeout 10s
        │                   (reporta DENEGADO)         (reporta SIN_INGRESO)
        └────────────────────────┴──────────────────────────────┘
```

- `VALIDANDO`: decide autorización contra la whitelist en NVS (ver §4, decisión de diseño).
- `PULSO+PROMESA`: pulso de solenoide **800 ms** y luego espera hasta **10 s** el flanco del reed.
  - Reed abre dentro de 10 s → reporta `acceso CON ingreso` (esto **crea sesión** en el server).
  - Timeout → reporta `acceso SIN ingreso` (no crea sesión). Vuelve a `IDLE`.
- El reed, el PIR y (futuro) los IR se reportan **siempre**, en cualquier estado, como eventos sueltos.
  El nodo no interpreta sesiones.

### 1.2 FSM de **sesión** del servidor (persistente en SQLite, es la auditoría)

Solo tiene **dos estados reales**. `EN_CURSO` vive en la tabla `sesiones`; sobrevive a apagones.

```
             evento acceso CON ingreso  (crea sesión)
   ┌───────┐ ─────────────────────────────────────────> ┌──────────────┐
   │  SIN  │                                             │   SESIÓN     │
   │SESIÓN │ <───────────────────────────────────────── │  EN_CURSO    │
   └───────┘   RELEVO / AUSENCIA / CIERRE_SISTEMA        └──────────────┘
      │  ▲                                                   │  ▲
      │  └─ cualquier señal física aquí = ANOMALÍA            │  └─ eventos internos NO cierran
      │     (APERTURA/PRESENCIA/ARMARIO _SIN_SESION)          │     (puerta, PIR, armario → actividad)
```

**El estado `ESPERA_APERTURA` de la spec NO existe en el servidor**: la "promesa" es enteramente local
al nodo. El server solo ve el evento final. Esto es clave para la resiliencia: si el nodo se resetea
durante la promesa, el server nunca creó una sesión a medias.

### 1.3 Tabla de transiciones del servidor (revisada)

| Estado | Evento | Condición | Acción | Estado sig. |
|---|---|---|---|---|
| SIN_SESION | acceso CON ingreso | — | Crear sesión (EN_CURSO), registrar ingreso | EN_CURSO |
| SIN_SESION | acceso SIN ingreso / DENEGADO | — | Registrar en `eventos_acceso` (sin sesión) | SIN_SESION |
| SIN_SESION | reed = ABIERTO | — | Alarma `APERTURA_SIN_CREDENCIAL` | SIN_SESION |
| SIN_SESION | PIR | — | Alarma `PRESENCIA_SIN_SESION` | SIN_SESION |
| SIN_SESION | armario ABIERTO | — | Alarma `ARMARIO_SIN_SESION` | SIN_SESION |
| EN_CURSO | acceso CON ingreso | UID (cualquiera) | Finalizar (RELEVO) + crear sesión nueva | EN_CURSO (nueva) |
| EN_CURSO | reed cambia | — | Registrar movimiento · actualizar actividad | EN_CURSO |
| EN_CURSO | PIR | — | Actualizar actividad | EN_CURSO |
| EN_CURSO | armario ABIERTO | — | Registrar armario **atribuido por timestamp** (§3.3) · actividad | EN_CURSO |
| EN_CURSO | tarea ausencia | sin actividad ≥ T y reed CERRADO | Finalizar (AUSENCIA) | SIN_SESION |
| EN_CURSO | tarea ausencia | sin actividad ≥ T y reed ABIERTO | Alarma `PUERTA_ABIERTA_SIN_GENTE` **(one-shot, §3.2)** | EN_CURSO |
| EN_CURSO | fin de jornada (22:00) | — | Finalizar (CIERRE_SISTEMA) | SIN_SESION |

---

## 2. Resiliencia a cortes de suministro (el corazón del pedido)

Qué pasa exactamente ante cada tipo de corte, y por qué el sistema queda bien parado.

### 2.1 Capa mecánica y eléctrica (lo que te salva sin software)

- **Solenoide *fail-secure* (energizar-para-abrir):** el pulso de 800 ms es lo único que abre.
  Sin energía = **trabado desde afuera**. Es el comportamiento correcto para acceso controlado.
- **Evacuación garantizada:** el picaporte interior es 100% mecánico → *siempre* se sale, haya o no luz.
  Esta es la verdadera red de seguridad, y no depende de un solo transistor.
- **Pull-up externo en la línea de control del relé (mejora sobre la spec):** una resistencia física
  (10 kΩ) que mantiene el relé **des-energizado** mientras el ESP32 arranca, *antes* de que corra una
  sola línea de MicroPython. El `value(1)` por software llega tarde: durante el brownout/reset el pin
  flota. La resistencia lo resuelve por hardware.
  - ⚠️ **Bug latente en el prototipo actual:** `portero.value(0)` en el init. Si el relé es *activo-LOW*
    (como pide la spec), ese `0` lo **activa**. El estado seguro debe ser `value(1)` + pull-up externo.
- **UPS chico para el servidor (opcional, recomendado):** el nodo puede reiniciar sin drama, pero el
  server es la fuente de verdad. Un mini-UPS le da un cierre ordenado. No es imprescindible gracias a
  la recuperación de §2.4, pero eleva el sistema a "profesional".

### 2.2 El nodo se recupera solo (porque casi no tiene estado)

Corte de luz en el nodo pañol → al volver:
1. Relé arranca en estado seguro (§2.1) → puerta trabada, sin pulso espurio.
2. La FSM local nace en `IDLE`. No hay "sesión a medias" que reconstruir: la sesión vive en el server.
3. El nodo lee y **reporta el estado actual** del reed y PIR al bootear (POST de estado inicial), para
   que el server sincronice sin depender de recordar nada.
4. **Watchdog (WDT):** un `WDT` de MicroPython (~8 s) reinicia el nodo si el firmware se cuelga. Una
   línea de código, enorme ganancia de robustez. El nodo "no se queda mudo" nunca.

### 2.3 No perder eventos de auditoría: cola persistente en flash

La spec encola en RAM (y NVS si >20). **Mejora: encolar siempre en flash** (archivo append-only), no en
RAM, porque un corte borra la RAM y con ella la auditoría.

- Cada evento se escribe primero a `cola.log` con su **timestamp original** y un **`event_id` único**.
- Al reconectar, se reenvía en orden y se truncan solo los confirmados por el server (ACK).
- Si el nodo se apaga con eventos pendientes, al volver siguen en `cola.log`. Nada se pierde.
- Simple y barato: es un archivo de texto, no una base de datos.

### 2.4 El servidor se recupera del apagón (SQLite = fuente de verdad)

Al reiniciar el server:
1. **Sesión abierta:** si había una `EN_CURSO`, sigue ahí en SQLite. Se evalúa su `ultima_actividad`:
   - reciente (< T_AUSENCIA) → **se reanuda** `EN_CURSO` normal.
   - vencida → se marca `INCONSISTENTE` + ticket `SESION_INCONSISTENTE` para revisión manual (spec §8).
2. **Alarmas pendientes:** se re-disparan todas las de la tabla `alarmas` con `enviada_ematp = false`
   (el apagón pudo interrumpir el envío). El espejo local garantiza que no se pierda ninguna.
3. **Heartbeats:** los nodos caídos se detectan por ausencia de señal → `NODO_SIN_HEARTBEAT` a los 5 min.
   Un apagón general se ve como un hueco de heartbeats + un `SYSTEM_BOOT` al volver.

### 2.5 Matriz de fallos

| Qué se corta | Puerta (afuera) | Evacuación | Auditoría | Recuperación |
|---|---|---|---|---|
| Solo el nodo pañol | Trabada (fail-secure) | OK (mecánica) | Eventos en `cola.log`, se reenvían | Auto al bootear |
| Solo el servidor | Nodo sigue en modo degradado (NVS) | OK | Nodo encola; server recupera de SQLite | Auto al reiniciar |
| Red / WiFi | Nodo en modo degradado, abre igual | OK | Nodo encola con timestamp | Auto al reconectar |
| Todo (apagón general) | Trabada | OK (mecánica) | Hueco = "sistema apagado" (visible por heartbeats) | Nodo→IDLE, server→SQLite |

---

## 3. Mejoras de robustez a la lógica (profesional)

Cuatro ajustes chicos que evitan bugs sutiles de auditoría, sobre todo con la cola offline.

### 3.1 Idempotencia (evita duplicados por reintentos)
Cada evento lleva un **`event_id` único** (uuid/contador+nodo). El server ignora un `event_id` ya visto.
Sin esto, un reenvío tras timeout podría **crear dos sesiones** o **contar dos veces** un armario.

### 3.2 Alarmas de estado sostenido = one-shot (evita spam a EMATP)
`PUERTA_ABIERTA_SIN_GENTE` se evalúa cada minuto mientras dure la condición. Debe emitirse **una sola vez**
por episodio (flag "ya alarmado", se resetea al cambiar la condición). Igual criterio para cualquier
alarma derivada de un estado que persiste.

### 3.3 Atribución por **timestamp del evento**, no por hora de llegada
El pseudocódigo atribuye el armario a `sesion_en_curso()` = sesión *actual*. Con la cola offline, un
evento puede **llegar tarde**: hay que atribuirlo a la sesión que estaba vigente **en su timestamp
original**, no en el de ingestión. Es lo que hace correcta la auditoría "quién abrió qué".

### 3.4 Una sola sesión activa, garantizada por la base
"A lo sumo una `EN_CURSO` por aula" no debe depender solo del código: un índice único parcial en SQLite
(`WHERE estado='EN_CURSO'`) lo vuelve imposible aun ante una carrera de dos lecturas casi simultáneas.

---

## 4. Punto de diseño a cerrar: ¿dónde decide la autorización?

Dos lecturas válidas de la spec. Recomiendo la **B** por simplicidad y resiliencia:

- **A (literal spec):** el nodo consulta al server por cada UID; si no responde en 2 s, usa NVS.
- **B (recomendada):** el nodo **siempre** decide contra la whitelist en NVS (refrescada por
  `GET /api/whitelist` cada 15 min) y **reporta** el resultado. La "promesa" ya es local; el server
  no está en el camino crítico de abrir la puerta. Un corte de red **nunca** demora una apertura
  legítima, y el modo degradado deja de ser un caso especial: es el caso normal.

> Consecuencia: `MODO_DEGRADADO` pasa a significar "no pude refrescar la whitelist / no llega el server
> para reportar", no "no puedo decidir". Más robusto.

---

## 5. Escenarios del simulador (Etapa 1, sin hardware)

La máquina de estados se valida hoy replayando secuencias de eventos y verificando sesión/alarmas:

1. **Nacimiento:** acceso CON ingreso → una `EN_CURSO` a nombre del UID.
2. **Persistencia:** varios reed/PIR internos → misma sesión, `ultima_actividad` avanza.
3. **Relevo:** segundo UID autorizado → sesión 1 `RELEVO`, sesión 2 `EN_CURSO`.
4. **Relevo del mismo UID:** mismo UID otra vez → renueva (nueva `sesion_id`), sin ambigüedad.
5. **Ausencia (puerta cerrada):** sin actividad ≥ T, reed CERRADO → `AUSENCIA`.
6. **Puerta abierta sin gente:** sin actividad ≥ T, reed ABIERTO → alarma one-shot, sesión sigue.
7. **Armario sin sesión:** IR sin `EN_CURSO` → `ARMARIO_SIN_SESION` (crítica).
8. **Falso cierre (spec §9):** AUSENCIA con alguien adentro → próximo PIR = `PRESENCIA_SIN_SESION`;
   la auditoría lo interpreta como falso cierre, no intrusión.
9. **Idempotencia:** mismo `event_id` dos veces → un solo efecto.
10. **Atribución tardía:** armario con timestamp viejo llega después de un relevo → se atribuye a la
    sesión correcta según su timestamp.
11. **Recuperación:** reiniciar con `EN_CURSO` vencida → `INCONSISTENTE` + ticket.

---

## 6. Puntos abiertos

- **Tiempo/NTP:** el nodo necesita hora real para los timestamps de la cola offline. NTP al bootear; si
  no hay red, timestamps best-effort (uptime monótono) que el server reconcilia al recibir. Definir formato
  ISO-8601 con offset `-03:00`.
- **Transporte Etapa 2:** HTTP (spec) vs MQTT (Mosquitto/Node-RED del homelab). El motor es agnóstico;
  se decide al desplegar. Un suscriptor MQTT alimenta el mismo `engine`.
- **Placa:** confirmar WROOM vs WROVER (GPIO16 libre solo en WROOM).
