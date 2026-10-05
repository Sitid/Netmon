"""Atribución en el tiempo: qué equipo y qué usuario tenía cada IP en cada momento.

Con DHCP una IP pasa de un equipo a otro (un iPhone de noche, un sensor de día):
atribuir un período pasado con el mapeo IP -> equipo/usuario de HOY es un dato
incorrecto (auditoría H04/H07). Este módulo mantiene dos historiales:

* ip_assignments: segmentos (ip, mac) con first_seen/last_seen, que el colector
  extiende cada minuto mientras la misma MAC siga con la IP.
* ip_user_log: cada login Kerberos visto (ip, usuario, hora).

y responde "quién era" para un período.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg

# una MAC que se deja de ver con la IP más de este tiempo abre un segmento nuevo
SEGMENT_GAP = "15 minutes"
# margen para ubicar un login dentro de un segmento (el login puede llegar antes
# que el primer ciclo del colector que ve al equipo)
LOGON_MATCH = "15 minutes"


async def record_assignments(pool: asyncpg.Pool, rows: list[tuple[str, str, int, str]],
                             now: datetime) -> None:
    """Registra que cada (ip, mac, vlan, nombre) se vio en `now`.

    Extiende el segmento de esa (ip, mac) si se vio hace menos de SEGMENT_GAP;
    si no, abre uno nuevo. Dos MAC con la misma IP a la vez quedan como dos
    segmentos superpuestos (no se pisan).
    """
    if not rows:
        return
    await pool.executemany(
        f"""WITH upd AS (
                UPDATE ip_assignments
                   SET last_seen = $5::timestamptz, vlan = $3::int,
                       hostname = COALESCE(NULLIF($4::text, ''), hostname)
                 WHERE ip = $1::inet AND mac = $2::macaddr
                   AND last_seen >= $5::timestamptz - interval '{SEGMENT_GAP}'
                   AND last_seen <= $5::timestamptz
                RETURNING 1)
            INSERT INTO ip_assignments (ip, mac, vlan, hostname, first_seen, last_seen)
            SELECT $1::inet, $2::macaddr, $3::int, COALESCE($4::text, ''), $5::timestamptz, $5::timestamptz
            WHERE NOT EXISTS (SELECT 1 FROM upd)
            ON CONFLICT (ip, mac, first_seen) DO UPDATE SET last_seen = EXCLUDED.last_seen""",
        [(ip, mac, int(vlan or 0), name or "", now) for ip, mac, vlan, name in rows],
    )


async def record_logon(pool: asyncpg.Pool, ip: str, user: str, seen_at: datetime) -> None:
    """Un login Kerberos: historial + último usuario de la IP + usuario del equipo.

    El usuario se guarda en devices SÓLO para la MAC que tenía la IP en ese
    momento (antes se escribía en toda MAC que alguna vez tuvo la IP y los
    celulares heredaban el usuario de la PC).
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO ip_user_log (ip, username, seen_at) VALUES ($1::inet, $2, $3)
               ON CONFLICT DO NOTHING""", ip, user, seen_at)
        await conn.execute(
            """INSERT INTO ip_user (ip, username, seen_at, source)
               VALUES ($1::inet, $2, $3, 'kerberos')
               ON CONFLICT (ip) DO UPDATE SET username = EXCLUDED.username,
                 seen_at = EXCLUDED.seen_at
               WHERE EXCLUDED.seen_at >= ip_user.seen_at""", ip, user, seen_at)
        await conn.execute(
            f"""UPDATE devices SET ad_user = $2
                WHERE mac = (SELECT mac FROM ip_assignments
                             WHERE ip = $1::inet
                               AND $3::timestamptz BETWEEN first_seen - interval '{LOGON_MATCH}'
                                          AND last_seen + interval '{LOGON_MATCH}'
                             ORDER BY last_seen DESC LIMIT 1)""", ip, user, seen_at)


async def assignments(pool: asyncpg.Pool, ip: str, start: datetime,
                      end: datetime) -> list[dict]:
    """Equipos que tuvieron la IP en [start, end), con el tráfico de cada tramo.

    El tráfico sale del tier de minutos (retención ~7 días); los minutos se
    asignan al segmento que los contiene.
    """
    rows = await pool.fetch(
        """SELECT a.mac::text AS mac, a.vlan,
                  COALESCE(NULLIF(a.hostname, ''), d.hostname, '') AS hostname,
                  COALESCE(d.vendor, '') AS vendor,
                  a.first_seen, a.last_seen,
                  COALESCE((SELECT SUM(t.bytes_up + t.bytes_down) FROM traffic_min t
                            WHERE t.ip = a.ip
                              AND t.ts >= GREATEST(a.first_seen, $2)
                              AND t.ts <= LEAST(a.last_seen, $3)), 0)::bigint AS bytes,
                  COALESCE((SELECT SUM(t.bytes_internet) FROM traffic_min t
                            WHERE t.ip = a.ip
                              AND t.ts >= GREATEST(a.first_seen, $2)
                              AND t.ts <= LEAST(a.last_seen, $3)), 0)::bigint AS internet
           FROM ip_assignments a
           LEFT JOIN devices d ON d.mac = a.mac
           WHERE a.ip = $1::inet AND a.first_seen < $3 AND a.last_seen >= $2
           ORDER BY a.first_seen""",
        ip, start, end)
    return [dict(r) for r in rows]


async def history_since(pool: asyncpg.Pool) -> datetime | None:
    """Desde cuándo hay historial IP -> equipo (antes no se puede atribuir)."""
    return await pool.fetchval("SELECT min(first_seen) FROM ip_assignments")


# Sesiones de usuario por IP: desde cada login hasta el siguiente login en esa IP,
# el TTL del mapa, o el fin del segmento del equipo que tenía la IP (si la IP pasó
# a otro equipo la sesión terminó). Cada fila de tráfico se asigna a la sesión
# que contiene su punto medio: una fila nunca cuenta para dos usuarios.
_SESSIONS_SQL = f"""
ev AS (
    SELECT ip, username, seen_at FROM ip_user_log WHERE seen_at < $2
    UNION
    SELECT ip, username, seen_at FROM ip_user WHERE seen_at < $2
), ses AS (
    SELECT ip, username, seen_at AS s,
           LEAST(seen_at + ($4::text)::interval,
                 COALESCE(LEAD(seen_at) OVER (PARTITION BY ip ORDER BY seen_at),
                          'infinity'::timestamptz)) AS e
    FROM ev
), sessions AS (
    SELECT s.ip, s.username, s.s,
           LEAST(s.e, COALESCE(seg.last_seen + interval '2 minutes', s.e)) AS e
    FROM ses s
    LEFT JOIN LATERAL (
        SELECT last_seen FROM ip_assignments a
        WHERE a.ip = s.ip
          AND s.s BETWEEN a.first_seen - interval '{LOGON_MATCH}'
                      AND a.last_seen + interval '{LOGON_MATCH}'
        ORDER BY a.last_seen DESC LIMIT 1) seg ON true
    WHERE s.e > $1
)"""

# $1 inicio, $2 fin, $3 corte hora/minuto (NULL = sólo minutos), $4 TTL, $5 usuario (o NULL)
USAGE_BY_USER_SQL = f"""
WITH {_SESSIONS_SQL}, traffic AS (
    SELECT ts, ip, bytes_up, bytes_down, bytes_internet, 3600 AS bucket FROM traffic_hour
    WHERE $3::timestamptz IS NOT NULL AND ts >= $1 AND ts < LEAST($2, $3::timestamptz)
    UNION ALL
    SELECT ts, ip, bytes_up, bytes_down, bytes_internet, 60 AS bucket FROM traffic_min
    WHERE ts >= GREATEST($1, COALESCE($3::timestamptz, $1)) AND ts < $2
)
SELECT se.username, host(t.ip) AS ip,
       SUM(t.bytes_up + t.bytes_down)::bigint AS total,
       SUM(t.bytes_internet)::bigint AS internet,
       MAX(t.ts) AS last
FROM traffic t
JOIN sessions se ON se.ip = t.ip
 AND t.ts + make_interval(secs => t.bucket / 2.0) >= se.s
 AND t.ts + make_interval(secs => t.bucket / 2.0) <  se.e
WHERE $5::text IS NULL OR lower(se.username) = lower($5::text)
GROUP BY se.username, t.ip
"""


async def usage_by_user(pool: asyncpg.Pool, start: datetime, end: datetime,
                        cut: datetime | None, ttl: str,
                        username: str | None = None) -> list[dict]:
    """Consumo por (usuario, ip) en [start, end) según las sesiones de cada IP."""
    rows = await pool.fetch(USAGE_BY_USER_SQL, start, end, cut, ttl, username)
    return [dict(r) for r in rows]


# Sitios de un usuario (registro de conexiones), con la misma regla de sesiones.
SITES_BY_USER_SQL = f"""
WITH {_SESSIONS_SQL}, fl AS (
    SELECT ts, local_ip AS ip, domain, bytes, 3600 AS bucket FROM flows_hour
    WHERE $3::timestamptz IS NOT NULL AND ts >= $1 AND ts < LEAST($2, $3::timestamptz)
      AND scope = 'internet' AND domain <> ''
    UNION ALL
    SELECT ts, local_ip, domain, bytes, 60 FROM flows_min
    WHERE ts >= GREATEST($1, COALESCE($3::timestamptz, $1)) AND ts < $2
      AND scope = 'internet' AND domain <> ''
)
SELECT f.domain, SUM(f.bytes)::bigint AS bytes
FROM fl f
JOIN sessions se ON se.ip = f.ip
 AND f.ts + make_interval(secs => f.bucket / 2.0) >= se.s
 AND f.ts + make_interval(secs => f.bucket / 2.0) <  se.e
WHERE lower(se.username) = lower($5::text)
GROUP BY f.domain
"""


async def sites_by_user(pool: asyncpg.Pool, start: datetime, end: datetime,
                        cut: datetime | None, ttl: str, username: str) -> list[dict]:
    rows = await pool.fetch(SITES_BY_USER_SQL, start, end, cut, ttl, username)
    return [dict(r) for r in rows]
