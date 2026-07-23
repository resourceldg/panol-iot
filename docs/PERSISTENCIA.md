# Política de persistencia

El criterio no es "cuánto entra en el disco" sino **qué pregunta tiene que poder
responder cada cosa, y hasta cuándo**. Una vez contestado eso, el espacio sale
solo — y queda acotado, que es lo que importa en un sistema que va a estar años
funcionando sin que nadie lo mire.

## Las tres capas, y qué se guarda en cada una

| Capa | Qué es | Cuánto vive |
|---|---|---|
| **Auditoría** (Postgres) | quién fue responsable, qué pasó, qué se alarmó | de 90 días a para siempre, según la tabla |
| **Logs de operación** (Loki) | qué hicieron los procesos | 48 h, y **apagados** salvo que alguien esté mirando |
| **Cola del nodo** (flash del ESP32) | eventos que todavía no llegaron | hasta que el servidor los confirma |

Son capas distintas a propósito. Un log no es auditoría: si el broker se quedó
sin memoria a las 3 de la mañana es un dato de operación, y a los dos días ya no
le importa a nadie. Que alguien entró al pañol el 12 de marzo, en cambio, hay
que poder responderlo el año que viene.

## Retención de la auditoría

| Tabla | Se conserva | Por qué |
|---|---|---|
| `alarmas` | **para siempre** | Son los tickets de EMATP. El sistema no borra su propio historial de incidentes, y además es la tabla más chica: una fila por episodio, no por muestra. |
| `sesiones` | **para siempre** | Es la respuesta del sistema: quién era responsable. Chica y de valor permanente. |
| `eventos_acceso` | 365 días | Respalda a una alarma y a una sesión. Un año cubre cualquier discusión sobre un incidente. |
| `eventos_puerta` | 365 días | Ídem. |
| `eventos_armario` | 365 días | Ídem. |
| `eventos_pir` | 90 días | Es **muestreo**, no hechos: dos por minuto mientras haya presencia indebida. El hecho ya quedó en la alarma; esto es el detalle fino que lo respalda. |
| `eventos_procesados` | 60 días | No es auditoría, es un libro de recibos para descartar reenvíos. |

Los horizontes se cambian por variable de entorno (`PANOL_RET_PIR_DIAS`,
`PANOL_RET_RECIBOS_DIAS`, …). Ver `server/retencion.py`.

**Cuidado con `eventos_procesados`.** Es el único horizonte que no se puede
bajar por gusto: si se borra un recibo que un nodo todavía tiene en su cola de
flash, ese evento se va a registrar **dos veces** cuando el nodo vuelva. 60 días
es varias veces cualquier corte plausible. Achicarlo no ahorra espacio
significativo y sí arriesga duplicados en la auditoría.

## Minimalismo de escrituras

Lo que gasta disco no es solo lo que queda guardado: es cada escritura, con su
WAL, su índice y su vacuum posterior. Cuatro decisiones que bajan el volumen sin
perder una sola respuesta:

1. **El PIR con sesión abierta no inserta nada.** Con responsable presente, el
   movimiento es señal de vida, no un hecho auditable. `eventos_pir` solo
   acumula movimiento *indebido*, que es el que hay que poder mostrar.
2. **La marca de actividad no se escribe por cada muestra.** El PIR reporta cada
   30 s: una tarde entera son cientos de `UPDATE` sobre la misma fila. Como la
   ausencia se mide en 15 minutos, una marca con hasta 60 s de atraso decide
   exactamente lo mismo (`t_precision_actividad_s`). Se conserva el evento, se
   ahorra el update.
3. **Las alarmas se agrupan por episodio.** Una condición sostenida —presencia
   sin sesión, nodo mudo, puerta abierta— genera **una** alarma y recordatorias
   cada 15 minutos, no una por muestra. Media hora de presencia indebida pasó de
   ~60 filas a 2. Y como cada alarma es un ticket de EMATP, esto no es una
   optimización de disco: es la diferencia entre un incidente y 60.
4. **Los heartbeats no insertan.** Actualizan una fila en `nodos`. Un nodo late
   cada 60 s: insertarlos serían 1.400 filas por día y por nodo para responder
   una pregunta —"¿está vivo?"— que solo necesita el último valor.

## La purga

Corre en el **planificador**, una vez por día a las 04:00 (`PANOL_HORA_PURGA`),
en lotes de 5.000 filas.

- **Una vez por día, no cada minuto**: borrar también escribe.
- **De madrugada**: no hay nadie en el colegio y el autovacuum tiene la noche
  para reciclar el espacio.
- **En lotes**: borrar 200.000 filas en una sentencia toma un lock largo y hace
  un pico de WAL. En lotes, cada transacción es corta.
- **Sin `VACUUM FULL`**: reescribe la tabla entera y necesita el doble de
  espacio libre, justo cuando falta. El autovacuum reutiliza el espacio, que es
  exactamente lo que se quiere en una tabla que crece y se purga todos los días.
- **Con índice por fecha**: sin él, cada pasada sería un scan completo de la
  tabla más grande. Se crean en `esquema.sql`.
- **Con lock de aviso**: si mañana hay dos planificadores, no se pisan.

## Números reales

Medido en el homelab (2026-07-23), con el sistema recién estrenado:

```
base panol: 8,4 MB    volúmenes docker: 881 MB    logs de contenedores: 15 MB
disco: 218 GB, 183 GB libres
```

Proyección de un pañol en régimen (≈60 accesos, 120 cambios de puerta y unas
pocas alarmas por día de clase):

| | Filas en régimen | Espacio |
|---|---|---|
| Tablas purgadas (pir, puerta, acceso, armario, recibos) | ~85.000 | ~20 MB, **estable** |
| Tablas que crecen (alarmas + sesiones) | ~9.000/año | ~3 MB/año |

O sea: la base se estaciona en unas decenas de MB y crece unos pocos MB por año.
Lo que la política garantiza no es que entre —entra de sobra— sino que **está
acotada**: sin purga, `eventos_procesados` crecería para siempre, que es la
forma en que estos sistemas se llenan el disco tres años después, un martes.

## Logs de operación

Fuera de la base, y con criterio distinto:

- **Loki**: retención de 48 h y **apagado por defecto**. Se prende con
  `logs-en-vivo` mientras alguien mira y se apaga solo. Indexar los logs de
  todos los contenedores cuesta RAM y disco de forma permanente para un uso que
  es puntual. Ver `docs/panol-iot.md` en el repo del homelab.
- **Docker (`json-file`)**: 10 MB × 3 archivos por contenedor, configurado en
  `daemon.json` del homelab. Es el piso que siempre está.
- **Mosquitto**: escribe también a un archivo en su volumen, con el mismo
  formato de fecha que el resto.

## Qué NO hace esta política

No archiva. Si mañana hace falta conservar los eventos crudos más de un año
—una auditoría externa, por ejemplo— lo correcto no es subir los horizontes
hasta que la base pese gigas, sino **exportar a un archivo comprimido fuera de
la base** antes de purgar. Un `COPY ... TO` mensual a un `.csv.gz` en el
backup de Borg ocupa una fracción y no le pesa a ninguna consulta. Cuando haga
falta, se agrega; hoy no hace falta y no se paga por adelantado.
