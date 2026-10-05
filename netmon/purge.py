"""Borrado manual de consumos de un equipo (función oculta, solo admin).

Siempre:
  - exige IP + rango de fechas (y opcionalmente la MAC de UN equipo);
  - respalda a disco las filas que va a borrar (recuperables);
  - deja traza en purge_log (quién, qué, cuántas filas, dónde está el backup).

No se expone en la navegación; el endpoint exige rol admin.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import asyncpg

from .config import get_settings
from .sites import friendly_site

# (tabla, columna de IP). Se borra el consumo del equipo = filas de SU IP.
# En flows sólo local_ip (la IP como origen); nunca remote_ip (son otros equipos).
PURGE_TABLES: list[tuple[str, str]] = [
    ("traffic_min", "ip"), ("traffic_5min", "ip"), ("traffic_hour", "ip"),
    ("traffic_cat_min", "ip"), ("traffic_cat_hour", "ip"),
    ("traffic_app_host_min", "ip"), ("traffic_app_host_hour", "ip"),
    ("flows_min", "local_ip"), ("flows_hour", "local_ip"),
]


def _where(ipcol: str) -> str:
    """WHERE con $1=ip, $2=desde, $3=hasta, $4=mac (texto o NULL).
    Si viene MAC, limita a las franjas (ip_assignments) de ESE equipo."""
    return (f"{ipcol} = $1::inet AND ts >= $2 AND ts < $3 "
            "AND ($4::text IS NULL OR EXISTS (SELECT 1 FROM ip_assignments a "
            f"WHERE a.ip = $1::inet AND a.mac = $4::macaddr "
            "AND ts >= a.first_seen AND ts < a.last_seen))")


async def preview(pool: asyncpg.Pool, ip: str, desde: datetime, hasta: datetime,
                  mac: str | None, cut: datetime) -> dict:
    """Cuenta filas por tabla (técnico) + resumen friendly de qué se va a borrar."""
    out: dict[str, int] = {}
    for table, ipcol in PURGE_TABLES:
        n = await pool.fetchval(
            f"SELECT count(*) FROM {table} WHERE {_where(ipcol)}", ip, desde, hasta, mac)
        if n:
            out[table] = int(n)
    return {"por_tabla": out, "total": sum(out.values()),
            "resumen": await summary(pool, ip, desde, hasta, mac, cut)}


def _tier_union(hour_t: str, min_t: str, cols: str) -> str:
    """UNION del tier horario (hasta corte $5) + tier de minutos (desde el corte),
    acotado por IP ($1), rango ($2,$3) y, si hay, la MAC del equipo ($4)."""
    seg = _seg("$1", "$4")
    return (f"SELECT {cols} FROM {hour_t} WHERE ip=$1::inet AND ts>=$2 "
            f"AND ts<LEAST($3::timestamptz,$5::timestamptz) AND {seg} "
            f"UNION ALL SELECT {cols} FROM {min_t} WHERE ip=$1::inet "
            f"AND ts>=GREATEST($2::timestamptz,$5::timestamptz) AND ts<$3 AND {seg}")


async def summary(pool: asyncpg.Pool, ip: str, desde: datetime, hasta: datetime,
                  mac: str | None, cut: datetime) -> dict:
    """Resumen legible del consumo a borrar: total, apps, categorías y sitios."""
    args = (ip, desde, hasta, mac, cut)
    total = await pool.fetchval(
        f"WITH u AS ({_tier_union('traffic_hour', 'traffic_min', 'bytes_up+bytes_down AS b')}) "
        "SELECT COALESCE(SUM(b),0)::bigint FROM u", *args)
    apps = await pool.fetch(
        f"WITH u AS ({_tier_union('traffic_app_host_hour', 'traffic_app_host_min', 'app, bytes_up+bytes_down AS b')}) "
        "SELECT app, SUM(b)::bigint t FROM u GROUP BY app ORDER BY 2 DESC LIMIT 12", *args)
    cats = await pool.fetch(
        f"WITH u AS ({_tier_union('traffic_cat_hour', 'traffic_cat_min', 'category, bytes_up+bytes_down AS b')}) "
        "SELECT category, SUM(b)::bigint t FROM u GROUP BY category ORDER BY 2 DESC", *args)
    sitios = await sites(pool, ip, desde, hasta, mac, cut)
    return {"total_bytes": int(total or 0),
            "apps": [{"app": r["app"], "bytes": int(r["t"])} for r in apps],
            "categorias": [{"category": r["category"], "bytes": int(r["t"])} for r in cats],
            "sitios": [{"site": s["site"], "bytes": s["bytes"]} for s in sitios[:12]]}


def _seg(ip_p: str, mac_p: str) -> str:
    return (f"({mac_p}::text IS NULL OR EXISTS (SELECT 1 FROM ip_assignments a "
            f"WHERE a.ip = {ip_p}::inet AND a.mac = {mac_p}::macaddr "
            "AND ts >= a.first_seen AND ts < a.last_seen))")


async def sites(pool: asyncpg.Pool, ip: str, desde: datetime, hasta: datetime,
                mac: str | None, cut: datetime) -> list[dict]:
    """Consumo del equipo por sitio (dominio), como la vista Sitios, para elegir
    qué borrar. $1 ip, $2 desde, $3 hasta, $4 mac, $5 corte rollup."""
    rows = await pool.fetch(
        f"""WITH u AS (
              SELECT domain, bytes FROM flows_hour
              WHERE local_ip=$1::inet AND domain<>'' AND ts>=$2
                AND ts<LEAST($3::timestamptz,$5::timestamptz) AND {_seg('$1', '$4')}
              UNION ALL
              SELECT domain, bytes FROM flows_min
              WHERE local_ip=$1::inet AND domain<>''
                AND ts>=GREATEST($2::timestamptz,$5::timestamptz) AND ts<$3 AND {_seg('$1', '$4')})
            SELECT domain, SUM(bytes)::bigint AS bytes FROM u
            GROUP BY domain ORDER BY 2 DESC LIMIT 300""",
        ip, desde, hasta, mac, cut)
    # agrupar dominios crudos en el nombre amigable (como la vista Sitios)
    groups: dict[str, dict] = {}
    for r in rows:
        name = friendly_site(r["domain"]) or r["domain"]
        g = groups.setdefault(name, {"site": name, "bytes": 0, "domains": []})
        g["bytes"] += int(r["bytes"])
        g["domains"].append(r["domain"])
    return sorted(groups.values(), key=lambda g: g["bytes"], reverse=True)


async def delete_sites(pool: asyncpg.Pool, ip: str, desde: datetime, hasta: datetime,
                       mac: str | None, domains: list[str], admin_user: str,
                       note: str = "") -> dict:
    """Respalda y borra el tráfico (flows) de los sitios elegidos para ese equipo.
    Afecta las vistas Sitios/Conexiones; no cambia el total agregado del equipo."""
    if not domains:
        return {"borradas": {}, "total": 0, "backup": ""}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = Path(get_settings().reports_dir) / "purge-backups"
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"purge-sites-{ip.replace(':', '_')}-{stamp}.json"
    where = (f"local_ip=$1::inet AND ts>=$2 AND ts<$3 AND domain = ANY($5::text[]) "
             f"AND {_seg('$1', '$4')}")
    deleted: dict[str, int] = {}
    dump: dict[str, list] = {}
    async with pool.acquire() as con:
        async with con.transaction():
            for table in ("flows_min", "flows_hour"):
                rows = await con.fetch(
                    f"SELECT * FROM {table} WHERE {where}", ip, desde, hasta, mac, domains)
                if not rows:
                    continue
                dump[table] = [dict(r) for r in rows]
                res = await con.execute(
                    f"DELETE FROM {table} WHERE {where}", ip, desde, hasta, mac, domains)
                deleted[table] = int(res.split()[-1])
            meta = {"ip": ip, "mac": mac, "desde": desde.isoformat(), "hasta": hasta.isoformat(),
                    "sitios": domains, "admin_user": admin_user, "generated": stamp, "rows": dump}
            backup.write_text(json.dumps(meta, default=str, ensure_ascii=False))
            await con.execute(
                """INSERT INTO purge_log (admin_user, ip, mac, desde, hasta,
                                          rows_deleted, backup_path, note)
                   VALUES ($1,$2::inet,$3::macaddr,$4,$5,$6::jsonb,$7,$8)""",
                admin_user, ip, mac, desde, hasta, json.dumps(deleted), str(backup),
                (note + f" | sitios: {', '.join(domains)}").strip(" |"))
    return {"borradas": deleted, "total": sum(deleted.values()), "backup": str(backup)}


async def execute(pool: asyncpg.Pool, ip: str, desde: datetime, hasta: datetime,
                  mac: str | None, admin_user: str, note: str = "") -> dict:
    """Respalda y borra. Devuelve filas borradas por tabla y la ruta del backup."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = Path(get_settings().reports_dir) / "purge-backups"
    bdir.mkdir(parents=True, exist_ok=True)
    safe_ip = ip.replace(":", "_")
    backup = bdir / f"purge-{safe_ip}-{stamp}.json"

    deleted: dict[str, int] = {}
    dump: dict[str, list] = {}
    async with pool.acquire() as con:
        async with con.transaction():
            for table, ipcol in PURGE_TABLES:
                rows = await con.fetch(
                    f"SELECT * FROM {table} WHERE {_where(ipcol)}", ip, desde, hasta, mac)
                if not rows:
                    continue
                dump[table] = [dict(r) for r in rows]
                res = await con.execute(
                    f"DELETE FROM {table} WHERE {_where(ipcol)}", ip, desde, hasta, mac)
                deleted[table] = int(res.split()[-1])   # 'DELETE N'
            # backup a disco ANTES de confirmar la transacción
            meta = {"ip": ip, "mac": mac, "desde": desde.isoformat(),
                    "hasta": hasta.isoformat(), "admin_user": admin_user,
                    "generated": stamp, "rows": dump}
            backup.write_text(json.dumps(meta, default=str, ensure_ascii=False))
            await con.execute(
                """INSERT INTO purge_log (admin_user, ip, mac, desde, hasta,
                                          rows_deleted, backup_path, note)
                   VALUES ($1, $2::inet, $3::macaddr, $4, $5, $6::jsonb, $7, $8)""",
                admin_user, ip, mac, desde, hasta, json.dumps(deleted), str(backup), note)
    return {"borradas": deleted, "total": sum(deleted.values()), "backup": str(backup)}
