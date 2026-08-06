-- Esquema de auditoría del sistema de pañoles (PostgreSQL).
--
-- Diferencia con la spec v1.0 §8: el modelo de datos de la spec está escrito
-- para UN solo pañol. Acá hay varios laboratorios y pañoles, cada uno con su
-- par de nodos (puerta + armarios), reportando todos al mismo servidor local.
-- Por eso `ubicacion_id` aparece en cada tabla desde el principio: agregarlo
-- después obliga a migrar datos de auditoría, que es justo lo que no se
-- quiere tocar.
--
-- Los timestamps son `timestamptz`: Postgres guarda el instante absoluto y
-- lo devuelve en la zona de la sesión. Con el contenedor en
-- America/Argentina/Buenos_Aires, un evento reportado con offset -03:00
-- vuelve como -03:00.

-- --- Topología -----------------------------------------------------------

CREATE TABLE IF NOT EXISTS ubicaciones (
    id                TEXT PRIMARY KEY,      -- 'panol-lab01'
    nombre            TEXT NOT NULL,
    cantidad_maquinas INTEGER,               -- 8 a 15 CPU por pañol
    activa            BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS nodos (
    id                TEXT PRIMARY KEY,      -- 'panol-lab01-puerta'
    ubicacion_id      TEXT NOT NULL REFERENCES ubicaciones(id),
    rol               TEXT NOT NULL CHECK (rol IN ('puerta', 'armarios')),
    ultimo_heartbeat  TIMESTAMPTZ,
    modo_degradado    BOOLEAN NOT NULL DEFAULT FALSE,
    uptime_s          BIGINT,
    rssi              INTEGER
);

-- Cada ubicación tiene a lo sumo un nodo por rol: una puerta y un nodo de
-- armarios. Dos nodos con el mismo rol en el mismo pañol sería un error de
-- configuración (dos ESP32 flasheados con el mismo ID, típico al clonar).
CREATE UNIQUE INDEX IF NOT EXISTS ix_nodos_rol_unico
    ON nodos (ubicacion_id, rol);

-- --- Personas ------------------------------------------------------------

-- La identidad canónica de las PERSONAS vive en EMATP (tabla users, en Neon).
-- Acá se mantiene un espejo local, porque el pañol necesita resolver el
-- usuario de un uid al crear la sesión sin depender de una consulta por
-- internet. `ematp_user_id` es la clave que une ambos sistemas: viaja en todo
-- lo que el pañol le empuja a EMATP para que allá se enlace sin ambigüedad.
CREATE TABLE IF NOT EXISTS usuarios (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ematp_user_id BIGINT,          -- users.id de EMATP; NULL = alta solo local
    email         TEXT,            -- clave humana, espejo de EMATP
    nombre        TEXT NOT NULL,
    apellido      TEXT NOT NULL,
    rol           TEXT,
    activo        BOOLEAN NOT NULL DEFAULT TRUE
);

-- Para bases ya creadas antes de esta versión (el CREATE de arriba no toca una
-- tabla existente). Idempotente.
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ematp_user_id BIGINT;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email TEXT;

-- Índice único COMPLETO (no parcial): los NULL no chocan entre sí en Postgres,
-- así que varios usuarios solo-locales conviven, y sirve de árbitro para el
-- ON CONFLICT (ematp_user_id) del sync tanto en bases nuevas como existentes.
CREATE UNIQUE INDEX IF NOT EXISTS ix_usuarios_ematp ON usuarios (ematp_user_id);

CREATE TABLE IF NOT EXISTS credenciales (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uid_hex    TEXT NOT NULL,
    usuario_id BIGINT REFERENCES usuarios(id),
    alta       TIMESTAMPTZ,
    baja       TIMESTAMPTZ,
    activa     BOOLEAN NOT NULL DEFAULT TRUE
);

-- Un llavero no puede estar activo dos veces. El índice es parcial: un UID
-- dado de baja puede volver a darse de alta más adelante sin chocar.
CREATE UNIQUE INDEX IF NOT EXISTS ix_credencial_uid_activa
    ON credenciales (uid_hex) WHERE activa;

-- --- Núcleo de la auditoría ----------------------------------------------

CREATE TABLE IF NOT EXISTS sesiones (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ubicacion_id            TEXT NOT NULL REFERENCES ubicaciones(id),
    uid_hex                 TEXT NOT NULL,
    usuario_id              BIGINT REFERENCES usuarios(id),
    hora_inicio             TIMESTAMPTZ NOT NULL,
    hora_fin                TIMESTAMPTZ,
    motivo_cierre           TEXT CHECK (motivo_cierre IN
                                ('RELEVO', 'AUSENCIA', 'CIERRE_SISTEMA')),
    ultima_actividad        TIMESTAMPTZ NOT NULL,
    estado                  TEXT NOT NULL DEFAULT 'EN_CURSO'
                                CHECK (estado IN
                                ('EN_CURSO', 'COMPLETA', 'INCONSISTENTE')),
    alarmada_puerta_abierta BOOLEAN NOT NULL DEFAULT FALSE,
    -- Outbox hacia EMATP (Fase 2): la sesión se empuja al crearla y al
    -- cerrarla. `push_pendiente` se prende en cada cambio de estado que le
    -- importa al tablero (alta, cierre, inconsistente) — NO en cada actividad,
    -- que es constante. El emisor lo apaga cuando EMATP confirma.
    push_pendiente          BOOLEAN NOT NULL DEFAULT TRUE,
    push_reintentos         INTEGER NOT NULL DEFAULT 0,
    push_ultimo_intento     TIMESTAMPTZ
);

-- Para bases ya creadas (el CREATE de arriba no toca una tabla existente).
ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS push_pendiente BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS push_reintentos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS push_ultimo_intento TIMESTAMPTZ;

-- Índice parcial del outbox: el emisor solo mira las que faltan empujar, que
-- son pocas frente al total histórico.
CREATE INDEX IF NOT EXISTS ix_sesiones_push_pendiente
    ON sesiones (hora_inicio) WHERE push_pendiente;

-- "A lo sumo una sesión activa POR UBICACIÓN" no puede depender solo del
-- código: dos requests casi simultáneos podrían leer ambos "no hay sesión"
-- y crear dos. El índice parcial lo vuelve imposible en la base. Importa
-- más con Postgres que con SQLite, porque acá hay concurrencia real: la API
-- y el puente MQTT son dos procesos escribiendo a la vez.
CREATE UNIQUE INDEX IF NOT EXISTS ix_sesion_activa_por_ubicacion
    ON sesiones (ubicacion_id) WHERE estado = 'EN_CURSO';

-- Para resolver "qué sesión estaba vigente en el timestamp T", que es la
-- consulta que hace correcta la atribución de eventos que llegan tarde.
CREATE INDEX IF NOT EXISTS ix_sesion_ubicacion_inicio
    ON sesiones (ubicacion_id, hora_inicio);

-- --- Eventos -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS eventos_puerta (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ubicacion_id TEXT NOT NULL REFERENCES ubicaciones(id),
    sesion_id    BIGINT REFERENCES sesiones(id),  -- NULL = anomalía
    estado_reed  TEXT NOT NULL CHECK (estado_reed IN ('ABIERTO', 'CERRADO')),
    timestamp    TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS eventos_armario (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ubicacion_id TEXT NOT NULL REFERENCES ubicaciones(id),
    sesion_id    BIGINT REFERENCES sesiones(id),  -- NULL = anomalía crítica
    armario_id   INTEGER NOT NULL,   -- único dentro de su ubicación
    timestamp    TIMESTAMPTZ NOT NULL
);

-- El PIR tiene dos naturalezas segun haya sesion o no. CON sesion es pura
-- actividad (solo corre ultima_actividad, spec §7) y NO deja fila aca. SIN
-- sesion es una anomalia que merece su propia huella auditable, ademas de
-- la alarma PRESENCIA_SIN_SESION. Por eso, en la practica, esta tabla solo
-- contiene movimientos sin sesion: es el registro de "presencia indebida".
CREATE TABLE IF NOT EXISTS eventos_pir (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ubicacion_id TEXT NOT NULL REFERENCES ubicaciones(id),
    sesion_id    BIGINT REFERENCES sesiones(id),  -- NULL = movimiento sin sesión
    timestamp    TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS eventos_acceso (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ubicacion_id   TEXT NOT NULL REFERENCES ubicaciones(id),
    uid_hex        TEXT NOT NULL,
    resultado      TEXT NOT NULL CHECK (resultado IN
                       ('CONCEDIDO', 'DENEGADO', 'SIN_INGRESO')),
    timestamp      TIMESTAMPTZ NOT NULL,
    modo_degradado BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_eventos_puerta_sesion  ON eventos_puerta (sesion_id);
CREATE INDEX IF NOT EXISTS ix_eventos_armario_sesion ON eventos_armario (sesion_id);
CREATE INDEX IF NOT EXISTS ix_eventos_pir_ts         ON eventos_pir (ubicacion_id, timestamp);
CREATE INDEX IF NOT EXISTS ix_eventos_acceso_ts      ON eventos_acceso (ubicacion_id, timestamp);

-- --- Alarmas -------------------------------------------------------------

-- Espejo local mínimo de lo que se le notifica a EMATP. Los tickets viven
-- allá; acá solo se guarda lo necesario para garantizar la entrega y para
-- poder reintentar después de un corte.
CREATE TABLE IF NOT EXISTS alarmas (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ubicacion_id   TEXT NOT NULL REFERENCES ubicaciones(id),
    codigo         TEXT NOT NULL,
    severidad      TEXT NOT NULL,
    sesion_id      BIGINT REFERENCES sesiones(id),
    detalle        JSONB,
    timestamp      TIMESTAMPTZ NOT NULL,
    enviada_ematp  BOOLEAN NOT NULL DEFAULT FALSE,
    reintentos     INTEGER NOT NULL DEFAULT 0
);

-- Al arrancar hay que re-disparar lo que quedó sin enviar: un corte puede
-- haber interrumpido el envío justo entre el INSERT y el POST.
CREATE INDEX IF NOT EXISTS ix_alarmas_pendientes
    ON alarmas (timestamp) WHERE NOT enviada_ematp;

-- --- Idempotencia --------------------------------------------------------

-- Los nodos reenvían desde su cola en flash cuando vuelve la red. Un ACK
-- que se perdió hace que el nodo reenvíe algo ya procesado; sin esta tabla
-- ese reenvío crearía una segunda sesión o contaría dos veces un armario.
--
-- Con MQTT esto pasa a ser imprescindible, no opcional: QoS 1 garantiza
-- "al menos una vez", o sea que los duplicados son parte del protocolo.
CREATE TABLE IF NOT EXISTS eventos_procesados (
    event_id   TEXT PRIMARY KEY,
    nodo_id    TEXT,
    tipo       TEXT,
    recibido   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --- Migraciones idempotentes --------------------------------------------
-- El esquema se aplica entero en cada arranque, así que los cambios sobre
-- tablas que ya existen viven acá. `IF NOT EXISTS` los hace inocuos tanto en
-- una base nueva como en una con datos.

-- Cuántas veces se reanudó esta sesión (mismo llavero volviendo del recreo).
-- El contador deja el hueco a la vista en vez de fabricar una continuidad que
-- no existió.
ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS reanudaciones INTEGER NOT NULL DEFAULT 0;

-- La purga de retención borra por fecha: sin estos índices, cada pasada sería
-- un scan completo de la tabla más grande del sistema.
CREATE INDEX IF NOT EXISTS ix_eventos_procesados_recibido ON eventos_procesados (recibido);
CREATE INDEX IF NOT EXISTS ix_eventos_armario_ts          ON eventos_armario (timestamp);
CREATE INDEX IF NOT EXISTS ix_alarmas_ts                  ON alarmas (timestamp);

-- Cuándo se intentó mandar esta alarma a EMATP por última vez. Sin este dato el
-- reintento solo puede ser "cada vuelta", y contra un plan gratuito (Vercel
-- Hobby + Neon) eso despierta una función y una base cada minuto para nada.
ALTER TABLE alarmas ADD COLUMN IF NOT EXISTS ultimo_intento TIMESTAMPTZ;
