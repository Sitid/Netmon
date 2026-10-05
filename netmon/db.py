"""Pool asyncpg y helpers de acceso a datos compartidos por todos los servicios."""

from __future__ import annotations

import json
import re
import logging
from typing import Any

import asyncpg
from zoneinfo import ZoneInfo

from .config import get_settings

log = logging.getLogger("netmon.db")

# Rangos permitidos para consultas (whitelist: evita interpolar strings del cliente)
RANGE_INTERVALS = {"5m": "5 minutes", "1h": "1 hour", "24h": "24 hours"}


async def create_pool() -> asyncpg.Pool:
    settings = get_settings()

    async def _init(conn: asyncpg.Connection) -> None:
        # jsonb <-> dict transparente
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    return await asyncpg.create_pool(
        dsn=settings.db_dsn, min_size=1, max_size=8, init=_init, command_timeout=30
    )


# ---------------------------------------------------------------------------
# meta_kv: estado interno persistente (marcas de rollup, etc.)
# ---------------------------------------------------------------------------

async def meta_get(pool: asyncpg.Pool, key: str, default: Any = None) -> Any:
    row = await pool.fetchrow("SELECT v FROM meta_kv WHERE k = $1", key)
    return row["v"] if row else default


async def meta_set(pool: asyncpg.Pool, key: str, value: Any) -> None:
    await pool.execute(
        "INSERT INTO meta_kv (k, v) VALUES ($1, $2) "
        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
        key, value,
    )


# ---------------------------------------------------------------------------
# Día local (NETMON_TZ)
# ---------------------------------------------------------------------------
_TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+)*$")


def day_start_sql(tz: str | None = None) -> str:
    """Expresión SQL: medianoche de hoy en la zona del negocio, como timestamptz.

    La zona se valida (nombre IANA existente) antes de interpolarla.
    """
    tz = tz or get_settings().tz
    if not _TZ_RE.match(tz):
        raise ValueError(f"zona horaria inválida: {tz!r}")
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ValueError(f"zona horaria inexistente: {tz!r}") from exc
    return f"(date_trunc('day', now() AT TIME ZONE '{tz}') AT TIME ZONE '{tz}')"


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------

async def insert_alert(
    pool: asyncpg.Pool, kind: str, severity: str, message: str, meta: dict | None = None
) -> int:
    row = await pool.fetchrow(
        "INSERT INTO alerts (kind, severity, message, meta) VALUES ($1,$2,$3,$4) RETURNING id",
        kind, severity, message, meta or {},
    )
    log.warning("ALERTA [%s/%s] %s", kind, severity, message)
    return row["id"]


# ---------------------------------------------------------------------------
# Consultas del dashboard (compartidas entre API y reportes)
# ---------------------------------------------------------------------------

TOP_TALKERS_SQL = """
SELECT host(t.ip)                             AS ip,
       COALESCE(h.hostname, '')              AS hostname,
       COALESCE(u.username, '')              AS ad_user,
       SUM(t.bytes_up)::bigint               AS bytes_up,
       SUM(t.bytes_down)::bigint             AS bytes_down,
       SUM(t.bytes_internet)::bigint         AS bytes_internet
FROM traffic_min t
LEFT JOIN hostnames h ON h.ip = t.ip
LEFT JOIN ip_user  u ON u.ip = t.ip AND u.seen_at > now() - ($2::text)::interval
WHERE t.ts > now() - ($1::text)::interval
GROUP BY 1, 2, 3
ORDER BY (SUM(t.bytes_up) + SUM(t.bytes_down)) DESC
LIMIT $3
"""


async def top_talkers(pool: asyncpg.Pool, range_key: str, limit: int = 50) -> list[dict]:
    interval = RANGE_INTERVALS.get(range_key, "1 hour")
    ttl = f"{get_settings().user_map_ttl_hours} hours"
    rows = await pool.fetch(TOP_TALKERS_SQL, interval, ttl, limit)
    return [dict(r) for r in rows]
