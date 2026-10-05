-- =============================================================================
-- netmon - Esquema PostgreSQL (13+)
-- Estrategia de retención:
--   * Tablas *_min  : resolución 1 minuto, retención corta (48 h por defecto)
--                     -> alimentan las vistas de 5 min / 1 h / 24 h.
--   * Tablas *_hour : rollups horarios, retención larga (90 días por defecto)
--                     -> cumplen el requisito de 30 días y alimentan reportes.
--   El colector hace los rollups y borra lo vencido (no hace falta cron).
-- =============================================================================

-- --- Inventario de dispositivos (clave = MAC real vista en el SPAN) ----------
CREATE TABLE IF NOT EXISTS devices (
    mac         macaddr PRIMARY KEY,
    ip          inet,                                  -- última IP conocida
    hostname    text,
    vendor      text,                                  -- fabricante por OUI
    ad_user     text,                                  -- último usuario AD mapeado
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    is_known    boolean     NOT NULL DEFAULT false,    -- true cuando el operador lo confirma
    notes       text
);
CREATE INDEX IF NOT EXISTS devices_last_seen_idx ON devices (last_seen DESC);

-- --- Cache de resolución IP -> hostname --------------------------------------
-- prio: menor = fuente más confiable. 10=DHCP, 20=PTR (DNS del dominio),
-- 30=nombre disectado por ntopng (mDNS/NetBIOS/DHCP pasivo).
CREATE TABLE IF NOT EXISTS hostnames (
    ip          inet PRIMARY KEY,
    hostname    text NOT NULL,
    source      text NOT NULL,
    prio        smallint NOT NULL DEFAULT 30,
    resolved_at timestamptz NOT NULL DEFAULT now()
);

-- --- Mapeo IP -> usuario AD (eventos Kerberos 4768 del DC) -------------------
CREATE TABLE IF NOT EXISTS ip_user (
    ip        inet PRIMARY KEY,
    username  text NOT NULL,
    seen_at   timestamptz NOT NULL,
    source    text NOT NULL DEFAULT 'kerberos'
);

-- --- Tráfico por host, resolución 1 minuto -----------------------------------
-- bytes_up = host -> resto (sent) / bytes_down = resto -> host (rcvd)
CREATE TABLE IF NOT EXISTS traffic_min (
    ts          timestamptz NOT NULL,   -- inicio del minuto
    ip          inet        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip)
);
CREATE INDEX IF NOT EXISTS traffic_min_ip_idx ON traffic_min (ip, ts);

-- --- Tráfico por host y categoría de negocio, 1 minuto -----------------------
CREATE TABLE IF NOT EXISTS traffic_cat_min (
    ts          timestamptz NOT NULL,
    ip          inet        NOT NULL,
    category    text        NOT NULL,   -- streaming | redes_sociales | descargas | corporativo | web_general | otros
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip, category)
);

-- --- Rollups horarios ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS traffic_hour (
    ts          timestamptz NOT NULL,
    ip          inet        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip)
);
CREATE TABLE IF NOT EXISTS traffic_cat_hour (
    ts          timestamptz NOT NULL,
    ip          inet        NOT NULL,
    category    text        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip, category)
);

-- --- Tier intermedio 5 min (para rangos de 7 días) -----------------------------
CREATE TABLE IF NOT EXISTS traffic_5min (
    ts          timestamptz NOT NULL,
    ip          inet        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip)
);

-- bytes_internet: parte de bytes_up+bytes_down intercambiada con IPs fuera de las
-- redes privadas (non_local de ntopng, que tiene todo RFC1918 como local). La red
-- interna es el resto. Columna agregada el 2026-09-25: antes de eso vale 0.
ALTER TABLE traffic_min  ADD COLUMN IF NOT EXISTS bytes_internet bigint NOT NULL DEFAULT 0;
ALTER TABLE traffic_5min ADD COLUMN IF NOT EXISTS bytes_internet bigint NOT NULL DEFAULT 0;
ALTER TABLE traffic_hour ADD COLUMN IF NOT EXISTS bytes_internet bigint NOT NULL DEFAULT 0;

-- --- Tráfico por aplicación nDPI, a nivel red (no por host) --------------------
-- Mantenerlo por host multiplicaría filas por 100; para las series por app
-- alcanza el agregado de toda la red. category se congela al momento de escribir.
CREATE TABLE IF NOT EXISTS app_min (
    ts          timestamptz NOT NULL,
    app         text        NOT NULL,
    category    text        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, app)
);
CREATE TABLE IF NOT EXISTS app_hour (
    ts          timestamptz NOT NULL,
    app         text        NOT NULL,
    category    text        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, app)
);

-- --- Tráfico por host y aplicación nDPI ------------------------------------------
-- Responde "la IP X gastó N GB en la app Y". Filas por (host, app) activos en
-- el minuto; misma retención que los demás tiers *_min / *_hour.
CREATE TABLE IF NOT EXISTS traffic_app_host_min (
    ts          timestamptz NOT NULL,
    ip          inet        NOT NULL,
    app         text        NOT NULL,
    category    text        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip, app)
);
CREATE INDEX IF NOT EXISTS traffic_app_host_min_ip_idx ON traffic_app_host_min (ip, ts);
CREATE TABLE IF NOT EXISTS traffic_app_host_hour (
    ts          timestamptz NOT NULL,
    ip          inet        NOT NULL,
    app         text        NOT NULL,
    category    text        NOT NULL,
    bytes_up    bigint      NOT NULL DEFAULT 0,
    bytes_down  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip, app)
);
CREATE INDEX IF NOT EXISTS traffic_app_host_hour_ip_idx ON traffic_app_host_hour (ip, ts);
CREATE INDEX IF NOT EXISTS traffic_cat_min_ip_idx ON traffic_cat_min (ip, ts);
CREATE INDEX IF NOT EXISTS traffic_cat_hour_ip_idx ON traffic_cat_hour (ip, ts);
CREATE INDEX IF NOT EXISTS traffic_hour_ip_idx ON traffic_hour (ip, ts);

-- --- Registro de conexiones (flujos) por minuto y por hora ----------------------
-- "Con quién habla cada equipo": una fila por (minuto, IP local, IP remota,
-- puerto de servicio, app L7). bytes = tráfico de esa conexión en el minuto
-- (delta de los contadores de ntopng). scope: internet | interno.
-- Se excluye RTSP (cámaras). Retención igual a los demás tiers: min 48 h, hora 30 d.
CREATE TABLE IF NOT EXISTS flows_min (
    ts          timestamptz NOT NULL,
    local_ip    inet        NOT NULL,
    remote_ip   inet        NOT NULL,
    srv_port    integer     NOT NULL,
    l7          text        NOT NULL,
    scope       text        NOT NULL,   -- internet | interno
    direction   text        NOT NULL,   -- saliente (local inició) | entrante
    bytes       bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, local_ip, remote_ip, srv_port, l7)
);
CREATE INDEX IF NOT EXISTS flows_min_local_idx  ON flows_min (local_ip, ts);
CREATE INDEX IF NOT EXISTS flows_min_remote_idx ON flows_min (remote_ip, ts);

CREATE TABLE IF NOT EXISTS flows_hour (
    ts          timestamptz NOT NULL,
    local_ip    inet        NOT NULL,
    remote_ip   inet        NOT NULL,
    srv_port    integer     NOT NULL,
    l7          text        NOT NULL,
    scope       text        NOT NULL,
    direction   text        NOT NULL,
    bytes       bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, local_ip, remote_ip, srv_port, l7)
);
CREATE INDEX IF NOT EXISTS flows_hour_local_idx  ON flows_hour (local_ip, ts);
CREATE INDEX IF NOT EXISTS flows_hour_remote_idx ON flows_hour (remote_ip, ts);

-- --- Dominios (SNI) de internet, a nivel red -----------------------------------
ALTER TABLE flows_min  ADD COLUMN IF NOT EXISTS domain text NOT NULL DEFAULT '';
ALTER TABLE flows_hour ADD COLUMN IF NOT EXISTS domain text NOT NULL DEFAULT '';
-- sitio (dominio) -> equipos: vistas Sitio / Usuario
CREATE INDEX IF NOT EXISTS flows_min_domain_idx  ON flows_min  (domain, ts) WHERE domain <> '';
CREATE INDEX IF NOT EXISTS flows_hour_domain_idx ON flows_hour (domain, ts) WHERE domain <> '';
CREATE TABLE IF NOT EXISTS domain_min (
    ts     timestamptz NOT NULL,
    domain text        NOT NULL,
    bytes  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, domain)
);
CREATE TABLE IF NOT EXISTS domain_hour (
    ts     timestamptz NOT NULL,
    domain text        NOT NULL,
    bytes  bigint      NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, domain)
);

-- --- Reglas de alerta configurables desde la UI --------------------------------
CREATE TABLE IF NOT EXISTS alert_rules (
    id          text PRIMARY KEY,        -- new_device | quota_daily | banned_category | blocklist
    enabled     boolean NOT NULL DEFAULT true,
    severity    text NOT NULL DEFAULT 'warning',
    params      jsonb NOT NULL DEFAULT '{}'::jsonb,
    description text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO alert_rules (id, enabled, severity, params, description) VALUES
  ('new_device',      true,  'info',     '{}',
   'Dispositivo nuevo detectado en la red'),
  ('quota_daily',     true,  'warning',  '{"gb": 15}',
   'Host que supera N GB de tráfico en el día'),
  ('banned_category', true,  'warning',  '{"categories": ["p2p"], "min_mb": 10}',
   'Uso de categoría prohibida (ej. torrents)'),
  ('blocklist',       true,  'critical', '{}',
   'Host interno conectado a IP de mala reputación (FireHOL level1)'),
  ('disk_usage',      true,  'warning',  '{"pct": 80, "crit_pct": 90, "paths": ["/"]}',
   'Espacio en disco del servidor (incluye Wazuh, ntopng, Suricata y la base de netmon)')
  ,
  ('internal_fanout', true,  'warning',  '{"max_destinos": 50}',
   'Escaneo / movimiento lateral: un equipo habla con muchos internos en 1 h'),
  ('upload_spike',    true,  'warning',  '{"min_mb": 500, "factor": 5}',
   'Subida anómala respecto del promedio del equipo (posible exfiltración)'),
  ('unusual_country', false, 'warning',  '{"paises_ok": ["AR","US"], "min_mb": 200}',
   'Tráfico a países fuera de la lista permitida')
ON CONFLICT (id) DO NOTHING;

-- --- Overrides del mapeo aplicación -> categoría (editable en Configuración) ----
CREATE TABLE IF NOT EXISTS category_map (
    app        text PRIMARY KEY,         -- nombre nDPI en minúsculas
    category   text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- --- Monitoreo de latencia / pérdida ------------------------------------------
-- target es una clave semántica: 'gateway', 'internet:8.8.8.8', 'dns_interno'
CREATE TABLE IF NOT EXISTS ping_min (
    ts          timestamptz NOT NULL,
    target      text        NOT NULL,
    rtt_avg_ms  real,
    rtt_max_ms  real,
    loss_pct    real NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, target)
);

-- Estado instantáneo de cada target (lo actualiza el pinger cada ciclo,
-- lo lee la API para las luces ON/OFF del dashboard sin esperar al minuto).
CREATE TABLE IF NOT EXISTS target_state (
    target        text PRIMARY KEY,
    address       text NOT NULL,
    up            boolean NOT NULL DEFAULT true,
    since         timestamptz NOT NULL DEFAULT now(),  -- desde cuándo está en este estado
    last_rtt_ms   real,
    last_loss_pct real,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- --- Alertas -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id        bigserial PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    kind      text NOT NULL,            -- new_device | link_down | link_up | degraded | collector
    severity  text NOT NULL DEFAULT 'warning',   -- info | warning | critical
    message   text NOT NULL,
    meta      jsonb NOT NULL DEFAULT '{}'::jsonb,
    acked     boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS alerts_acked_ts_idx ON alerts (acked, ts DESC);

-- --- Usuarios de la interfaz web --------------------------------------------------
-- admin: todo (configuración, reglas, usuarios). viewer: sólo lectura de consumos,
-- gráficos y reportes. La clave NETMON_ADMIN_PASSWORD del .env sigue valiendo como
-- acceso de emergencia del usuario 'admin'.
CREATE TABLE IF NOT EXISTS users (
    username      text PRIMARY KEY,
    password_hash text NOT NULL,                -- scrypt$n$r$p$salt$hash (base64)
    role          text NOT NULL CHECK (role IN ('admin', 'viewer')),
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login    timestamptz
);

-- --- Estado interno del colector (marcas de rollup, etc.) ----------------------
CREATE TABLE IF NOT EXISTS meta_kv (
    k text PRIMARY KEY,
    v jsonb NOT NULL
);

-- --- Historial IP -> equipo y logins (auditoría H04/H07) ----------------------
-- Con DHCP la IP no identifica a un equipo: el colector registra cada minuto qué
-- MAC tenía cada IP. Un segmento se extiende mientras la misma MAC siga viéndose
-- con esa IP (tolerancia 15 min); si cambia o vuelve más tarde, segmento nuevo.
CREATE TABLE IF NOT EXISTS ip_assignments (
    ip         inet        NOT NULL,
    mac        macaddr     NOT NULL,
    vlan       integer     NOT NULL DEFAULT 0,
    hostname   text        NOT NULL DEFAULT '',
    first_seen timestamptz NOT NULL,
    last_seen  timestamptz NOT NULL,
    PRIMARY KEY (ip, mac, first_seen)
);
CREATE INDEX IF NOT EXISTS ip_assignments_ip_idx  ON ip_assignments (ip, last_seen DESC);
CREATE INDEX IF NOT EXISTS ip_assignments_mac_idx ON ip_assignments (mac, last_seen DESC);

-- cada login Kerberos (4768) visto; ip_user sólo guarda el último por IP
CREATE TABLE IF NOT EXISTS ip_user_log (
    ip       inet        NOT NULL,
    username text        NOT NULL,
    seen_at  timestamptz NOT NULL,
    PRIMARY KEY (ip, username, seen_at)
);
CREATE INDEX IF NOT EXISTS ip_user_log_ip_idx   ON ip_user_log (ip, seen_at DESC);
CREATE INDEX IF NOT EXISTS ip_user_log_user_idx ON ip_user_log (lower(username), seen_at DESC);

-- --- Registro de consultas de datos personales (auditoría H10) -----------------
-- Quién (usuario/rol/IP) pidió datos de qué equipo, usuario o sitio y cuándo.
CREATE TABLE IF NOT EXISTS access_log (
    id        bigserial   PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    username  text        NOT NULL DEFAULT '',
    role      text        NOT NULL DEFAULT '',
    client_ip text        NOT NULL DEFAULT '',
    method    text        NOT NULL,
    path      text        NOT NULL,
    query     text        NOT NULL DEFAULT '',
    status    integer     NOT NULL
);
CREATE INDEX IF NOT EXISTS access_log_ts_idx ON access_log (ts DESC);

-- --- Mapa DNS pasivo (sitios para conexiones sin SNI: QUIC/ECH) -----------------
-- netmon-dnsmap lee las respuestas DNS que registra Suricata (eve.json) y guarda
-- la última "cliente preguntó por <nombre> y recibió <ip>". El colector usa el
-- mapa para nombrar conexiones de internet que no traen SNI. Retención 2 días.
CREATE TABLE IF NOT EXISTS dns_map (
    client_ip inet        NOT NULL,
    answer_ip inet        NOT NULL,
    domain    text        NOT NULL,
    seen_at   timestamptz NOT NULL,
    PRIMARY KEY (client_ip, answer_ip)
);
CREATE INDEX IF NOT EXISTS dns_map_answer_idx ON dns_map (answer_ip, seen_at DESC);
-- de dónde salió el nombre de sitio de una conexión: 'sni' | 'dns' | '' (sin nombre)
ALTER TABLE flows_min  ADD COLUMN IF NOT EXISTS domain_src text NOT NULL DEFAULT '';
ALTER TABLE flows_hour ADD COLUMN IF NOT EXISTS domain_src text NOT NULL DEFAULT '';

-- Auditoría de borrados manuales de consumo (función oculta de admin).
-- Cada purga deja traza: quién, qué IP/equipo/rango, cuántas filas y el backup.
CREATE TABLE IF NOT EXISTS purge_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    admin_user  text        NOT NULL DEFAULT '',
    ip          inet        NOT NULL,
    mac         macaddr,
    desde       timestamptz NOT NULL,
    hasta       timestamptz NOT NULL,
    rows_deleted jsonb      NOT NULL DEFAULT '{}'::jsonb,
    backup_path text        NOT NULL DEFAULT '',
    note        text        NOT NULL DEFAULT ''
);
