"""Punto único para levantar alertas: DB + mail + webhook + dedupe diario."""

from __future__ import annotations

from datetime import datetime

import asyncpg

from . import db, notify


async def raise_alert(
    pool: asyncpg.Pool,
    kind: str,
    severity: str,
    message: str,
    meta: dict | None = None,
    do_notify: bool = True,
) -> int:
    """Registra la alerta y dispara notificaciones externas si corresponde."""
    alert_id = await db.insert_alert(pool, kind, severity, message, meta)
    if do_notify:
        await notify.send_mail(f"{severity.upper()}: {kind}", message)
        await notify.send_webhook({
            "kind": kind,
            "severity": severity,
            "message": message,
            "meta": meta or {},
            "ts": datetime.utcnow().isoformat() + "Z",
            "source": "netmon",
        })
    return alert_id


async def alert_once_per_day(
    pool: asyncpg.Pool,
    kind: str,
    key: str,
    severity: str,
    message: str,
    meta: dict | None = None,
) -> bool:
    """Levanta la alerta solo si no existe otra con el mismo (kind, key) hoy.

    Evita que las reglas de cuota/categoría prohibida/blocklist spameen: una
    alerta por host (o par de IPs) por día. `key` va en meta['key'].
    """
    exists = await pool.fetchval(
        f"""SELECT 1 FROM alerts
           WHERE kind = $1 AND meta->>'key' = $2
             AND ts >= {db.day_start_sql()}
           LIMIT 1""",
        kind, key,
    )
    if exists:
        return False
    meta = dict(meta or {})
    meta["key"] = key
    await raise_alert(pool, kind, severity, message, meta)
    return True


async def load_rules(pool: asyncpg.Pool) -> dict[str, dict]:
    """Reglas de alerta configurables (tabla alert_rules) como dict por id."""
    rows = await pool.fetch("SELECT * FROM alert_rules")
    return {r["id"]: dict(r) for r in rows}
