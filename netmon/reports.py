"""Generación de reportes CSV / PDF de consumo para gerencia y auditoría.

Dos tipos de reporte:
  * General (día/semana): top de equipos con sus principales aplicaciones,
    aplicaciones de toda la red con sus principales equipos, y categorías.
  * Por equipo (día/semana/rango): consumo hora por hora, picos (hora y
    minuto) y en qué aplicaciones se gastó cada hora. Pensado para poder
    afirmar en una auditoría "el equipo X consumió N en tal hora, en tal app".

Las consultas combinan la tabla horaria (histórico consolidado) con la tabla
de minutos SOLO desde la marca 'rollup_until' en adelante, así el período puede
incluir el día en curso sin contar bytes dos veces. Todas las horas se
presentan en la zona horaria local del servidor.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from xml.sax.saxutils import escape

import asyncpg

from . import db
from .config import get_settings
from .categories import CATEGORY_LABELS, CATEGORY_ORDER
from .sites import friendly_site

MAX_CUSTOM_DAYS = 31

TOP_PERIOD_SQL = """
WITH unified AS (
    SELECT ts, ip, bytes_up, bytes_down, bytes_internet FROM traffic_hour
    WHERE ts >= $1::timestamptz AND ts < LEAST($2::timestamptz, $3::timestamptz)
    UNION ALL
    SELECT ts, ip, bytes_up, bytes_down, bytes_internet FROM traffic_min
    WHERE ts >= GREATEST($1::timestamptz, $3::timestamptz) AND ts < $2::timestamptz
), per_ip AS (
    SELECT ip, SUM(bytes_up)::bigint AS bytes_up, SUM(bytes_down)::bigint AS bytes_down,
           SUM(bytes_internet)::bigint AS bytes_internet,
           -- antes de 'internet_since' ($5) no se distinguía internet de red interna
           SUM(CASE WHEN ts < COALESCE($5::timestamptz, 'infinity') THEN bytes_up + bytes_down
                    ELSE 0 END)::bigint AS bytes_unclassified
    FROM unified GROUP BY ip
    ORDER BY SUM(bytes_up + bytes_down) DESC LIMIT $4
)
-- se suma por IP antes de unir: una IP con varias MAC en devices duplicaba la fila.
-- Equipo y usuario SE TOMAN DEL PERÍODO (auditoría H04): los equipos que tuvieron
-- la IP en [inicio, fin) según ip_assignments y los logins de esa IP en el
-- período ($6 = TTL del mapa). Sin historial para el período, el nombre actual
-- se marca "(actual)" y el usuario queda vacío.
SELECT host(p.ip)                 AS ip,
       COALESCE(seg.names, NULLIF(h.hostname, '') || ' (actual)', '') AS hostname,
       COALESCE(us.users, '')    AS ad_user,
       COALESCE(seg.vendors, d.vendor, '') AS vendor,
       COALESCE(seg.n, 0)        AS equipos,
       p.bytes_up, p.bytes_down, p.bytes_internet, p.bytes_unclassified
FROM per_ip p
LEFT JOIN hostnames h ON h.ip = p.ip
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT COALESCE(NULLIF(a.hostname, ''), a.mac::text), ' / ') AS names,
           string_agg(DISTINCT NULLIF(dd.vendor, ''), ' / ') AS vendors,
           count(DISTINCT a.mac) AS n
    FROM ip_assignments a LEFT JOIN devices dd ON dd.mac = a.mac
    WHERE a.ip = p.ip AND a.first_seen < $2::timestamptz AND a.last_seen >= $1::timestamptz
) seg ON true
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT l.username, ', ') AS users FROM (
        SELECT username, seen_at FROM ip_user_log WHERE ip = p.ip
        UNION SELECT username, seen_at FROM ip_user WHERE ip = p.ip) l
    WHERE l.seen_at >= $1::timestamptz - ($6::text)::interval AND l.seen_at < $2::timestamptz
) us ON true
LEFT JOIN LATERAL (SELECT vendor FROM devices WHERE ip = p.ip
                   ORDER BY last_seen DESC LIMIT 1) d ON true
ORDER BY p.bytes_up + p.bytes_down DESC
"""

CAT_PERIOD_SQL = """
WITH unified AS (
    SELECT category, bytes_up, bytes_down FROM traffic_cat_hour
    WHERE ts >= $1::timestamptz AND ts < LEAST($2::timestamptz, $3::timestamptz)
    UNION ALL
    SELECT category, bytes_up, bytes_down FROM traffic_cat_min
    WHERE ts >= GREATEST($1::timestamptz, $3::timestamptz) AND ts < $2::timestamptz
)
SELECT category, SUM(bytes_up + bytes_down)::bigint AS total
FROM unified GROUP BY 1 ORDER BY 2 DESC
"""

# Consumo de TODA la red por día calendario local (para "qué días consumió más").
# $1 inicio, $2 fin, $3 corte rollup_until, $4 nombre de zona, $5 internet_since.
DAILY_SQL = """
WITH unified AS (
    SELECT ts, bytes_up, bytes_down, bytes_internet FROM traffic_hour
    WHERE ts >= $1::timestamptz AND ts < LEAST($2::timestamptz, $3::timestamptz)
    UNION ALL
    SELECT ts, bytes_up, bytes_down, bytes_internet FROM traffic_min
    WHERE ts >= GREATEST($1::timestamptz, $3::timestamptz) AND ts < $2::timestamptz
)
SELECT (ts AT TIME ZONE $4::text)::date AS day,
       SUM(bytes_up)::bigint   AS bytes_up,
       SUM(bytes_down)::bigint AS bytes_down,
       SUM(bytes_internet)::bigint AS bytes_internet,
       SUM(CASE WHEN ts < COALESCE($5::timestamptz, 'infinity')
                THEN bytes_up + bytes_down ELSE 0 END)::bigint AS bytes_unclassified
FROM unified GROUP BY 1 ORDER BY 1
"""

# Cantidad de equipos (IPs) con tráfico en el rango.
ACTIVE_HOSTS_SQL = """
SELECT count(DISTINCT ip) FROM (
    SELECT ip FROM traffic_hour
    WHERE ts >= $1::timestamptz AND ts < LEAST($2::timestamptz, $3::timestamptz)
    UNION
    SELECT ip FROM traffic_min
    WHERE ts >= GREATEST($1::timestamptz, $3::timestamptz) AND ts < $2::timestamptz
) s
"""


def _unified(cols_hour: str, cols_min: str, hour: str, minute: str,
             where: str = "TRUE") -> str:
    """Tier horario hasta el corte ($3) + tier de minutos desde el corte.

    $1 = inicio, $2 = fin, $3 = corte 'rollup_until'. Todo sale de constantes
    del módulo, nunca de entrada del cliente.
    """
    return f"""
    SELECT {cols_hour}, bytes_up, bytes_down FROM {hour}
    WHERE {where} AND ts >= $1::timestamptz AND ts < LEAST($2::timestamptz, $3::timestamptz)
    UNION ALL
    SELECT {cols_min}, bytes_up, bytes_down FROM {minute}
    WHERE {where} AND ts >= GREATEST($1::timestamptz, $3::timestamptz) AND ts < $2::timestamptz"""


# (equipo, app) de toda la red: alimenta "apps por equipo" y "equipos por app"
APP_HOST_PERIOD_SQL = f"""
WITH unified AS ({_unified("ip, app, category", "ip, app, category",
                           "traffic_app_host_hour", "traffic_app_host_min")})
SELECT host(u.ip) AS ip, COALESCE(h.hostname, '') AS hostname, u.app,
       MAX(u.category) AS category, SUM(u.bytes_up + u.bytes_down)::bigint AS total
FROM unified u LEFT JOIN hostnames h ON h.ip = u.ip
GROUP BY u.ip, h.hostname, u.app
"""

def _seg_pred(ip_p: str, mac_p: str) -> str:
    """Predicado opcional por equipo. Si el parámetro de MAC es NULL no filtra
    (reporte de la IP completa); si trae una MAC, restringe el tráfico a las
    franjas (ip_assignments) en que ESA MAC tuvo la IP. Permite extraer el
    reporte de un solo equipo cuando varios compartieron la misma IP."""
    return (f"({mac_p}::text IS NULL OR EXISTS (SELECT 1 FROM ip_assignments a "
            f"WHERE a.ip = {ip_p}::inet AND a.mac = {mac_p}::macaddr "
            f"AND ts >= a.first_seen AND ts < a.last_seen))")


# hourly usa $5 para internet_since, así que la MAC va en $6; apps/cat usan $5.
_HOST_WHERE_HOURLY = f"ip = $4::inet AND {_seg_pred('$4', '$6')}"
_HOST_WHERE = f"ip = $4::inet AND {_seg_pred('$4', '$5')}"

HOST_HOURLY_SQL = f"""
WITH unified AS ({_unified("ts, bytes_internet, ts AS raw_ts",
                           "date_trunc('hour', ts), bytes_internet, ts",
                           "traffic_hour", "traffic_min", _HOST_WHERE_HOURLY)})
SELECT ts AS hour, SUM(bytes_up)::bigint AS bytes_up, SUM(bytes_down)::bigint AS bytes_down,
       SUM(bytes_internet)::bigint AS bytes_internet,
       SUM(CASE WHEN raw_ts < COALESCE($5::timestamptz, 'infinity') THEN bytes_up + bytes_down
                ELSE 0 END)::bigint AS bytes_unclassified
FROM unified GROUP BY 1 ORDER BY 1
"""

HOST_HOUR_APPS_SQL = f"""
WITH unified AS ({_unified("ts, app, category", "date_trunc('hour', ts), app, category",
                           "traffic_app_host_hour", "traffic_app_host_min", _HOST_WHERE)})
SELECT ts AS hour, app, MAX(category) AS category,
       SUM(bytes_up)::bigint AS bytes_up, SUM(bytes_down)::bigint AS bytes_down
FROM unified GROUP BY 1, 2 ORDER BY 1, SUM(bytes_up + bytes_down) DESC
"""

HOST_CAT_SQL = f"""
WITH unified AS ({_unified("category", "category", "traffic_cat_hour", "traffic_cat_min",
                           _HOST_WHERE)})
SELECT category, SUM(bytes_up + bytes_down)::bigint AS total
FROM unified GROUP BY 1 ORDER BY 2 DESC
"""


# ---------------------------------------------------------------------------
# Períodos
# ---------------------------------------------------------------------------

def period_bounds(range_kind: str, ref: date) -> tuple[datetime, datetime, str]:
    """(inicio, fin, etiqueta) en la zona del negocio (NETMON_TZ) para 'day' o 'week'
    (semana lun-dom de ref): el día del reporte es el día calendario local."""
    if range_kind == "month":
        first = ref.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        start = datetime.combine(first, time.min, tzinfo=_zone())
        end = datetime.combine(nxt, time.min, tzinfo=_zone())
        label = f"Mes de {first.strftime('%Y-%m')} (del {first.isoformat()} al "\
                f"{(nxt - timedelta(days=1)).isoformat()})"
    elif range_kind == "week":
        monday = ref - timedelta(days=ref.weekday())
        start = datetime.combine(monday, time.min, tzinfo=_zone())
        end = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=_zone())
        label = f"Semana del {monday.isoformat()} al {(monday + timedelta(days=6)).isoformat()}"
    else:
        start = datetime.combine(ref, time.min, tzinfo=_zone())
        end = datetime.combine(ref + timedelta(days=1), time.min, tzinfo=_zone())
        label = f"Día {ref.isoformat()}"
    return start, end, label


def period_bounds_custom(desde: date, hasta: date) -> tuple[datetime, datetime, str]:
    """Rango de días calendario locales [desde, hasta], ambos inclusive."""
    if hasta < desde:
        raise ValueError("la fecha 'hasta' es anterior a 'desde'")
    if (hasta - desde).days + 1 > MAX_CUSTOM_DAYS:
        raise ValueError(f"el rango no puede superar {MAX_CUSTOM_DAYS} días")
    start = datetime.combine(desde, time.min, tzinfo=_zone())
    end = datetime.combine(hasta + timedelta(days=1), time.min, tzinfo=_zone())
    return start, end, f"Del {desde.isoformat()} al {hasta.isoformat()}"


async def _rollup_until(pool: asyncpg.Pool) -> datetime:
    raw = await db.meta_get(pool, "rollup_until")
    return datetime.fromisoformat(raw) if raw else datetime(1970, 1, 1, tzinfo=timezone.utc)


async def _apps_since(pool: asyncpg.Pool) -> datetime | None:
    return await pool.fetchval(
        """SELECT LEAST((SELECT min(ts) FROM traffic_app_host_hour),
                        (SELECT min(ts) FROM traffic_app_host_min))""")


async def _internet_since(pool: asyncpg.Pool) -> datetime | None:
    raw = await db.meta_get(pool, "internet_since")
    return datetime.fromisoformat(raw) if raw else None


def _internal(row: dict) -> int:
    """Red interna = lo clasificado que no fue internet."""
    total = row["bytes_up"] + row["bytes_down"]
    return max(total - row["bytes_internet"] - row["bytes_unclassified"], 0)


def _inet_cell(row: dict, fmt) -> str:
    """Internet de una fila; '—' si todo su tráfico es anterior a la clasificación."""
    total = row["bytes_up"] + row["bytes_down"]
    return "—" if total and row["bytes_unclassified"] >= total else fmt(row["bytes_internet"])


def _zone():
    """Zona del negocio (NETMON_TZ): días y horas de los reportes (auditoría H18)."""
    return get_settings().zone()


def _local(ts: datetime) -> datetime:
    return ts.astimezone(_zone())


def _fmt_ts(ts: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return _local(ts).strftime(fmt)


def _fmt_window(pk: dict) -> str:
    """'2026-09-25 11:50–11:55' para una ventana de pico."""
    end = _local(pk["ts"]) + timedelta(seconds=pk["seconds"])
    return f"{_fmt_ts(pk['ts'])}–{end.strftime('%H:%M')}"


def _tz_name() -> str:
    return f"{get_settings().tz} (UTC{datetime.now(_zone()).strftime('%z')})"


# ---------------------------------------------------------------------------
# Reporte general
# ---------------------------------------------------------------------------

async def gather_report_data(
    pool: asyncpg.Pool, range_kind: str, ref: date, limit: int = 50
) -> dict:
    start, end, label = period_bounds(range_kind, ref)
    cut = await _rollup_until(pool)
    since = await _internet_since(pool)
    ttl = f"{get_settings().user_map_ttl_hours} hours"
    top = [dict(r) for r in await pool.fetch(TOP_PERIOD_SQL, start, end, cut, limit, since, ttl)]
    cats = [dict(r) for r in await pool.fetch(CAT_PERIOD_SQL, start, end, cut)]
    pairs = await pool.fetch(APP_HOST_PERIOD_SQL, start, end, cut)

    # apps por equipo (para los equipos del top) y equipos por app (toda la red)
    host_apps: dict[str, list[dict]] = {}
    apps: dict[str, dict] = {}
    for r in pairs:
        host_apps.setdefault(r["ip"], []).append(
            {"app": r["app"], "category": r["category"], "total": r["total"]})
        a = apps.setdefault(r["app"], {"app": r["app"], "category": r["category"],
                                       "total": 0, "hosts": []})
        a["total"] += r["total"]
        a["hosts"].append({"ip": r["ip"], "hostname": r["hostname"], "total": r["total"]})
    for lst in host_apps.values():
        lst.sort(key=lambda x: x["total"], reverse=True)
    top_apps = sorted(apps.values(), key=lambda a: a["total"], reverse=True)[:30]
    for a in top_apps:
        a["hosts"].sort(key=lambda h: h["total"], reverse=True)
    top_ips = {r["ip"] for r in top}
    for r in top:
        r["total"] = r["bytes_up"] + r["bytes_down"]
    daily, totals = await _daily_series(pool, start, end)
    active = await pool.fetchval(ACTIVE_HOSTS_SQL, start, end, cut)
    return {"label": label, "start": start, "end": end, "top": top, "categories": cats,
            "host_apps": {ip: v for ip, v in host_apps.items() if ip in top_ips},
            "top_apps": top_apps, "apps_since": await _apps_since(pool),
            "daily": daily, "totals": totals, "active_hosts": int(active or 0),
            "internet_since": await db.meta_get(pool, "internet_since")}


async def _daily_series(pool: asyncpg.Pool, start: datetime,
                        end: datetime) -> tuple[list[dict], dict]:
    """Consumo de toda la red por día calendario local en [start, end), con los
    días sin tráfico rellenados en 0, y los totales del rango."""
    cut = await _rollup_until(pool)
    since = await _internet_since(pool)
    tz = get_settings().tz
    by_day = {r["day"]: dict(r)
              for r in await pool.fetch(DAILY_SQL, start, end, cut, tz, since)}
    daily: list[dict] = []
    totals = {"bytes_up": 0, "bytes_down": 0, "bytes_internet": 0, "bytes_unclassified": 0}
    d = _local(start).date()
    last = (_local(end) - timedelta(days=1)).date()
    while d <= last:
        r = by_day.get(d) or {"bytes_up": 0, "bytes_down": 0,
                              "bytes_internet": 0, "bytes_unclassified": 0}
        total = r["bytes_up"] + r["bytes_down"]
        daily.append({
            "day": d.isoformat(),
            "bytes_up": r["bytes_up"], "bytes_down": r["bytes_down"], "total": total,
            "bytes_internet": r["bytes_internet"],
            "bytes_internal": max(total - r["bytes_internet"] - r["bytes_unclassified"], 0),
            "bytes_unclassified": r["bytes_unclassified"],
        })
        for k in totals:
            totals[k] += r[k]
        d += timedelta(days=1)
    totals["total"] = totals["bytes_up"] + totals["bytes_down"]
    totals["bytes_internal"] = max(
        totals["total"] - totals["bytes_internet"] - totals["bytes_unclassified"], 0)
    # día de mayor consumo (para el resumen del informe)
    peak = max(daily, key=lambda x: x["total"], default=None)
    totals["peak_day"] = peak["day"] if peak and peak["total"] else None
    totals["peak_total"] = peak["total"] if peak else 0
    return daily, totals


async def gather_overview(
    pool: asyncpg.Pool, start: datetime, end: datetime, limit: int = 20
) -> dict:
    """Datos para la vista de Reportes en pantalla: serie por día, totales,
    top de equipos y categorías, sobre un rango [start, end) ya acotado."""
    cut = await _rollup_until(pool)
    since = await _internet_since(pool)
    ttl = f"{get_settings().user_map_ttl_hours} hours"
    daily, totals = await _daily_series(pool, start, end)
    top = [dict(r) for r in await pool.fetch(TOP_PERIOD_SQL, start, end, cut, limit, since, ttl)]
    for r in top:
        r["total"] = r["bytes_up"] + r["bytes_down"]
        r["bytes_internal"] = _internal(r)
    cats = [dict(r) for r in await pool.fetch(CAT_PERIOD_SQL, start, end, cut)]
    active = await pool.fetchval(ACTIVE_HOSTS_SQL, start, end, cut)
    return {"daily": daily, "totals": totals, "top": top, "categories": cats,
            "active_hosts": int(active or 0),
            "internet_since": await db.meta_get(pool, "internet_since")}


def fmt_mb(n: int) -> str:
    """Bytes -> string legible en MB/GB (coma decimal, para gerencia local)."""
    mb = n / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB".replace(".", ",")
    return f"{mb:.1f} MB".replace(".", ",")


def _mb(n: int) -> str:
    """Bytes -> MB con coma decimal para columnas numéricas del CSV."""
    return f"{n / 1048576:.1f}".replace(".", ",")


def _apps_summary(apps: list[dict], n: int = 3) -> str:
    return "; ".join(f"{a['app']} {fmt_mb(a['total'])}" for a in apps[:n])


def _hosts_summary(hosts: list[dict], n: int = 3) -> str:
    return "; ".join(f"{h['hostname'] or h['ip']} {fmt_mb(h['total'])}" for h in hosts[:n])


def _apps_note(data: dict) -> str:
    since = data.get("apps_since")
    note = (f"El desglose por aplicación se registra desde {_fmt_ts(since)}."
            if since else "Todavía no hay desglose por aplicación registrado.")
    inet = data.get("internet_since")
    if inet:
        note += (f" Internet = tráfico con IPs fuera de las redes privadas; se distingue desde "
                 f"{_fmt_ts(datetime.fromisoformat(inet))} (antes figura como 0).")
    return note


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def build_csv(data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")  # ; abre bien en Excel es-AR
    w.writerow([f"Reporte de consumo de red - {data['label']}"])
    w.writerow(["Generado", datetime.now(_zone()).strftime("%Y-%m-%d %H:%M"), "Zona horaria", _tz_name()])
    w.writerow([_apps_note(data)])
    w.writerow([])
    t = data.get("totals") or {}
    if t:
        ndays = max(1, len(data.get("daily", []) or []))
        w.writerow(["Resumen del período"])
        w.writerow(["Consumo total (MB)", _mb(t.get("total", 0)),
                    "Equipos activos", data.get("active_hosts", "")])
        w.writerow(["Internet (MB)", _mb(t.get("bytes_internet", 0)),
                    "Promedio por día (MB)", _mb(round(t.get("total", 0) / ndays))])
        w.writerow(["Red interna (MB)", _mb(t.get("bytes_internal", 0)),
                    "Día de mayor consumo", t.get("peak_day") or "—"])
        w.writerow([])
    if data.get("daily"):
        w.writerow(["Consumo por día"])
        w.writerow(["Día", "Total (MB)", "Internet (MB)", "Red interna (MB)"])
        for d in data["daily"]:
            w.writerow([d["day"], _mb(d["total"]), _mb(d["bytes_internet"]),
                        _mb(d["bytes_internal"])])
        w.writerow([])
    w.writerow(["#", "IP", "Hostname", "Usuario AD", "Fabricante",
                "Subida (MB)", "Bajada (MB)", "Total (MB)", "Internet (MB)", "Red interna (MB)",
                "Sin clasificar (MB)", "Principales aplicaciones"])
    for i, row in enumerate(data["top"], 1):
        total = row["bytes_up"] + row["bytes_down"]
        w.writerow([i, row["ip"], row["hostname"], row["ad_user"], row["vendor"],
                    _mb(row["bytes_up"]), _mb(row["bytes_down"]), _mb(total),
                    _mb(row["bytes_internet"]), _mb(_internal(row)), _mb(row["bytes_unclassified"]),
                    _apps_summary(data["host_apps"].get(row["ip"], []), 5)])
    w.writerow([])
    w.writerow(["Aplicaciones de toda la red"])
    w.writerow(["#", "Aplicación", "Categoría", "Total (MB)", "Equipos", "Principales equipos"])
    for i, a in enumerate(data["top_apps"], 1):
        w.writerow([i, a["app"], CATEGORY_LABELS.get(a["category"], a["category"]),
                    _mb(a["total"]), len(a["hosts"]), _hosts_summary(a["hosts"], 5)])
    w.writerow([])
    w.writerow(["Categoría", "Total (MB)"])
    for cat in data["categories"]:
        label = CATEGORY_LABELS.get(cat["category"], cat["category"])
        w.writerow([label, _mb(cat["total"])])
    w.writerow([])
    w.writerow(["Detalle equipo x aplicación (equipos del top)"])
    w.writerow(["IP", "Hostname", "Aplicación", "Categoría", "Total (MB)"])
    for row in data["top"]:
        for a in data["host_apps"].get(row["ip"], []):
            w.writerow([row["ip"], row["hostname"], a["app"],
                        CATEGORY_LABELS.get(a["category"], a["category"]), _mb(a["total"])])
    # BOM para que Excel detecte UTF-8 (tildes en nombres)
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# PDF (reportlab)
# ---------------------------------------------------------------------------

def _pdf_kit():
    """Imports y estilos compartidos por los dos PDFs (reportlab es pesado)."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=7, leading=8.5)
    head_bg = colors.HexColor("#1f2937")

    def table(rows, widths, right_from=None, font=7.5):
        """Tabla con encabezado oscuro; las celdas str largas van como Paragraph."""
        body = [rows[0]] + [
            [Paragraph(escape(c), cell) if isinstance(c, str) and len(c) > 28 else c for c in r]
            for r in rows[1:]]
        t = Table(body, repeatRows=1, colWidths=[w * mm for w in widths])
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), head_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), font),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
        ]
        if right_from is not None:
            style.append(("ALIGN", (right_from, 1), (-1, -1), "RIGHT"))
        t.setStyle(TableStyle(style))
        return t

    return colors, styles, mm, Paragraph, Spacer, table


def _privacy_note(styles, Paragraph):
    return Paragraph(
        "Este reporte contiene únicamente metadatos agregados de tráfico de red "
        "(volúmenes por equipo, aplicación y categoría). No se inspecciona ni almacena el "
        "contenido de las comunicaciones.", styles["Italic"])


# paleta estable para los gráficos de categoría del PDF
CAT_COLORS = {
    "streaming": "#ef4444", "camaras": "#8b5cf6", "social": "#3b82f6",
    "productividad": "#10b981", "sistema": "#64748b", "p2p": "#f59e0b",
    "desconocido": "#94a3b8",
}


def _summary_flowable(data, colors, mm, styles, Paragraph):
    """Tarjeta de resumen del período (totales y día de mayor consumo)."""
    from reportlab.platypus import Table, TableStyle
    t = data.get("totals", {}) or {}
    ndays = max(1, len(data.get("daily", []) or []))
    total = t.get("total", 0)
    pct_inet = f"{(100 * t.get('bytes_internet', 0) / total):.0f}%" if total else "—"
    peak = "—"
    if t.get("peak_day"):
        peak = f"{t['peak_day']}  ({fmt_mb(t.get('peak_total', 0))})"
    cells = [
        ["Consumo total", fmt_mb(total), "Equipos activos", str(data.get("active_hosts", "—"))],
        ["Internet", f"{fmt_mb(t.get('bytes_internet', 0))}  ({pct_inet})",
         "Promedio por día", fmt_mb(round(total / ndays))],
        ["Red interna", fmt_mb(t.get("bytes_internal", 0)),
         "Día de mayor consumo", peak],
    ]
    tbl = Table(cells, colWidths=[32 * mm, 52 * mm, 38 * mm, 58 * mm])
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#475569")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#475569")),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    return tbl


def _daily_barchart(daily, colors, mm):
    """Gráfico de barras de consumo por día (MB o GB según escala)."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, String

    vals_mb = [d["total"] / (1024 * 1024) for d in daily]
    peak_mb = max(vals_mb, default=0)
    use_gb = peak_mb >= 1024
    vals = [v / 1024 for v in vals_mb] if use_gb else vals_mb
    unit = "GB" if use_gb else "MB"
    n = len(daily)
    step = max(1, round(n / 15))
    labels = [(d["day"][8:10] + "/" + d["day"][5:7]) if i % step == 0 else ""
              for i, d in enumerate(daily)]

    width, height = 258 * mm, 62 * mm
    dr = Drawing(width, height)
    bc = VerticalBarChart()
    bc.x, bc.y = 16 * mm, 12 * mm
    bc.width, bc.height = width - 24 * mm, height - 20 * mm
    bc.data = [vals]
    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.fontSize = 6
    bc.categoryAxis.labels.angle = 90
    bc.categoryAxis.labels.dy = -4
    bc.valueAxis.valueMin = 0
    bc.valueAxis.labels.fontSize = 6
    bc.valueAxis.labelTextFormat = (lambda v: f"{v:.0f}")
    bc.bars[0].fillColor = colors.HexColor("#2563eb")
    bc.barWidth = 0.6
    dr.add(bc)
    dr.add(String(16 * mm, height - 5 * mm, f"Consumo por día ({unit})",
                  fontSize=8, fillColor=colors.HexColor("#475569")))
    return dr


def _category_pie(cats, colors, mm):
    """Torta de consumo por categoría."""
    from reportlab.graphics.charts.legends import Legend
    from reportlab.graphics.charts.piecharts import Pie
    from reportlab.graphics.shapes import Drawing

    data = [(CATEGORY_LABELS.get(c["category"], c["category"]), c["total"],
             c["category"]) for c in cats if c["total"] > 0]
    if not data:
        return None
    dr = Drawing(120 * mm, 60 * mm)
    pie = Pie()
    pie.x, pie.y = 6 * mm, 8 * mm
    pie.width = pie.height = 44 * mm
    pie.data = [d[1] for d in data]
    pie.slices.strokeWidth = 0.5
    for i, d in enumerate(data):
        pie.slices[i].fillColor = colors.HexColor(CAT_COLORS.get(d[2], "#94a3b8"))
    dr.add(pie)
    total = sum(d[1] for d in data) or 1
    leg = Legend()
    leg.x, leg.y = 58 * mm, 46 * mm
    leg.fontSize = 7
    leg.dxTextSpace = 4
    leg.deltay = 11
    leg.colorNamePairs = [
        (colors.HexColor(CAT_COLORS.get(d[2], "#94a3b8")),
         f"{d[0]}  {100 * d[1] / total:.0f}%") for d in data]
    dr.add(leg)
    return dr


def build_pdf(data: dict) -> bytes:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate

    colors, styles, mm, Paragraph, Spacer, table = _pdf_kit()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), topMargin=14 * mm,
                            bottomMargin=12 * mm, leftMargin=12 * mm, rightMargin=12 * mm)
    story = [
        Paragraph("Reporte de consumo de red", styles["Title"]),
        Paragraph(data["label"], styles["Heading2"]),
        Paragraph(f"Generado: {datetime.now(_zone()).strftime('%Y-%m-%d %H:%M')} · Horario: {_tz_name()} — "
                  "Fuente: netmon (metadatos de tráfico, sin inspección de contenido). "
                  + _apps_note(data), styles["Normal"]),
        Spacer(1, 4 * mm),
    ]

    # 1) Resumen del período
    if data.get("totals"):
        story += [Paragraph("Resumen del período", styles["Heading3"]),
                  _summary_flowable(data, colors, mm, styles, Paragraph),
                  Spacer(1, 5 * mm)]

    # 2) Consumo por día (sólo tiene sentido con varios días)
    daily = data.get("daily") or []
    if len(daily) > 1:
        story += [_daily_barchart(daily, colors, mm), Spacer(1, 4 * mm)]

    rows = [["#", "IP", "Hostname", "Usuario AD", "Subida", "Bajada", "Total",
             "Internet", "Red interna", "Sin clasif.", "Principales aplicaciones"]]
    rows += [
        [str(i), r["ip"], r["hostname"][:24], r["ad_user"][:16],
         fmt_mb(r["bytes_up"]), fmt_mb(r["bytes_down"]),
         fmt_mb(r["bytes_up"] + r["bytes_down"]), _inet_cell(r, fmt_mb),
         fmt_mb(_internal(r)), fmt_mb(r["bytes_unclassified"]),
         _apps_summary(data["host_apps"].get(r["ip"], []), 3) or "—"]
        for i, r in enumerate(data["top"], 1)
    ]
    story += [Paragraph(f"Top {len(rows) - 1} consumidores", styles["Heading3"]),
              table(rows, [8, 24, 34, 22, 17, 17, 18, 18, 18, 18, 79], right_from=4),
              Spacer(1, 6 * mm)]

    if data["top_apps"]:
        rows = [["#", "Aplicación", "Categoría", "Total", "Equipos", "Principales equipos"]]
        rows += [[str(i), a["app"], CATEGORY_LABELS.get(a["category"], a["category"]),
                  fmt_mb(a["total"]), str(len(a["hosts"])), _hosts_summary(a["hosts"], 4)]
                 for i, a in enumerate(data["top_apps"], 1)]
        story += [Paragraph("En qué se consumió: aplicaciones de toda la red", styles["Heading3"]),
                  table(rows, [8, 40, 28, 22, 16, 159]), Spacer(1, 6 * mm)]

    known = {c["category"]: c["total"] for c in data["categories"]}
    cat_rows = [[CATEGORY_LABELS.get(c, c), fmt_mb(known[c])]
                for c in CATEGORY_ORDER if c in known]
    if cat_rows:
        from reportlab.platypus import Table, TableStyle
        cat_table = table([["Categoría", "Total"]] + cat_rows, [45, 35], right_from=1, font=8.5)
        pie = _category_pie(data["categories"], colors, mm)
        story.append(Paragraph("Consumo por categoría", styles["Heading3"]))
        if pie is not None:
            side = Table([[cat_table, pie]], colWidths=[85 * mm, 125 * mm])
            side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(side)
        else:
            story.append(cat_table)

    story += [Spacer(1, 8 * mm), _privacy_note(styles, Paragraph)]
    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Reporte por equipo (auditoría)
# ---------------------------------------------------------------------------

async def gather_host_report(pool: asyncpg.Pool, ip: str, start: datetime,
                             end: datetime, label: str, mac: str | None = None) -> dict:
    cut = await _rollup_until(pool)
    # equipo(s) y usuario(s) que tuvieron la IP EN EL PERÍODO (auditoría H04)
    ttl = f"{get_settings().user_map_ttl_hours} hours"
    ident = await pool.fetchrow(
        """SELECT COALESCE(seg.names,
                           NULLIF((SELECT hostname FROM hostnames WHERE ip = $1::inet), '') || ' (actual)',
                           '') AS hostname,
                  COALESCE(us.users, '') AS ad_user,
                  COALESCE(seg.macs, d.mac::text) AS mac,
                  COALESCE(seg.vendors, d.vendor) AS vendor, d.first_seen,
                  COALESCE(seg.n, 0) AS equipos
           FROM (SELECT 1) x
           LEFT JOIN LATERAL (
               SELECT string_agg(DISTINCT COALESCE(NULLIF(a.hostname, ''), a.mac::text), ' / ') AS names,
                      string_agg(DISTINCT a.mac::text, ' / ') AS macs,
                      string_agg(DISTINCT NULLIF(dd.vendor, ''), ' / ') AS vendors,
                      count(DISTINCT a.mac) AS n
               FROM ip_assignments a LEFT JOIN devices dd ON dd.mac = a.mac
               WHERE a.ip = $1::inet AND a.first_seen < $3 AND a.last_seen >= $2) seg ON true
           LEFT JOIN LATERAL (
               SELECT string_agg(DISTINCT l.username, ', ') AS users FROM (
                   SELECT username, seen_at FROM ip_user_log WHERE ip = $1::inet
                   UNION SELECT username, seen_at FROM ip_user WHERE ip = $1::inet) l
               WHERE l.seen_at >= $2 - ($4::text)::interval AND l.seen_at < $3) us ON true
           LEFT JOIN LATERAL (SELECT mac, vendor, first_seen FROM devices
                              WHERE ip = $1::inet ORDER BY last_seen DESC LIMIT 1) d ON true""",
        ip, start, end, ttl)
    since = await _internet_since(pool)
    hourly = [dict(r) for r in await pool.fetch(HOST_HOURLY_SQL, start, end, cut, ip, since, mac)]
    hour_apps = await pool.fetch(HOST_HOUR_APPS_SQL, start, end, cut, ip, mac)
    cats = [dict(r) for r in await pool.fetch(HOST_CAT_SQL, start, end, cut, ip, mac)]
    # si se eligió un equipo puntual, mostrar su identidad (no la de la IP entera)
    if mac:
        dev = await pool.fetchrow(
            """SELECT COALESCE(NULLIF(a.hostname,''), d.hostname, '') AS hostname,
                      COALESCE(d.vendor,'') AS vendor
               FROM (SELECT $1::macaddr AS mac) m
               LEFT JOIN LATERAL (SELECT hostname FROM ip_assignments
                                  WHERE mac=m.mac AND ip=$2::inet AND hostname<>''
                                  ORDER BY last_seen DESC LIMIT 1) a ON true
               LEFT JOIN LATERAL (SELECT hostname, vendor FROM devices
                                  WHERE mac=m.mac ORDER BY last_seen DESC LIMIT 1) d ON true""",
            mac, ip)
        if dev:
            ident = {**dict(ident), "hostname": dev["hostname"] or ident["hostname"],
                     "vendor": dev["vendor"] or ident["vendor"], "mac": mac, "equipos": 1}

    apps_by_hour: dict[datetime, list[dict]] = {}
    app_totals: dict[str, dict] = {}
    for r in hour_apps:
        t = r["bytes_up"] + r["bytes_down"]
        apps_by_hour.setdefault(r["hour"], []).append(
            {"app": r["app"], "category": r["category"], "total": t})
        a = app_totals.setdefault(r["app"], {"app": r["app"], "category": r["category"],
                                             "bytes_up": 0, "bytes_down": 0, "total": 0})
        a["bytes_up"] += r["bytes_up"]
        a["bytes_down"] += r["bytes_down"]
        a["total"] += t
    for h in hourly:
        h["total"] = h["bytes_up"] + h["bytes_down"]
        h["apps"] = apps_by_hour.get(h["hour"], [])

    # Pico en ventanas de 5 minutos. No se usa el minuto suelto: si un ciclo del
    # colector se demora, dos minutos de tráfico caen en uno y el "pico" sería
    # falso; el volumen de una ventana de 5 min es correcto igual.
    peak = None
    row = await pool.fetchrow(
        f"""SELECT to_timestamp(floor(extract(epoch FROM ts) / 300) * 300) AS ts,
                  SUM(bytes_up + bytes_down)::bigint AS total
           FROM traffic_min WHERE ip = $1::inet AND ts >= $2 AND ts < $3
             AND {_seg_pred('$1', '$4')}
           GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", ip, start, end, mac)
    if row:
        apps = await pool.fetch(
            """SELECT app, MAX(category) AS category,
                      SUM(bytes_up + bytes_down)::bigint AS total
               FROM traffic_app_host_min
               WHERE ip = $1::inet AND ts >= $2 AND ts < $2 + interval '5 minutes'
               GROUP BY 1 ORDER BY 3 DESC LIMIT 5""", ip, row["ts"])
        peak = {"ts": row["ts"], "resolution": "5 minutos", "seconds": 300,
                "total": row["total"], "apps": [dict(a) for a in apps]}
    else:   # minutos ya vencidos (48 h): tier de 5 min, apps de esa hora
        row = await pool.fetchrow(
            f"""SELECT ts, bytes_up + bytes_down AS total FROM traffic_5min
               WHERE ip = $1::inet AND ts >= $2 AND ts < $3
                 AND {_seg_pred('$1', '$4')}
               ORDER BY 2 DESC LIMIT 1""", ip, start, end, mac)
        if row:
            hour = _local(row["ts"]).replace(minute=0, second=0, microsecond=0)
            peak = {"ts": row["ts"], "resolution": "5 minutos", "seconds": 300,
                    "total": row["total"],
                    "apps": next((h["apps"] for h in hourly if _local(h["hour"]) == hour), [])[:5]}

    # dominios de internet (a dónde va): del registro de flujos, con SNI.
    # Rescata apps que nDPI dejó como QUIC/TLS genérico (ej. pv-cdn.net = Prime Video).
    domains = [dict(r) for r in await pool.fetch(
        f"""WITH u AS (
             SELECT domain, bytes FROM flows_hour
             WHERE local_ip=$1::inet AND scope='internet' AND domain<>''
               AND ts>=$2 AND ts<LEAST($3::timestamptz,$4::timestamptz)
               AND {_seg_pred('$1', '$5')}
             UNION ALL
             SELECT domain, bytes FROM flows_min
             WHERE local_ip=$1::inet AND scope='internet' AND domain<>''
               AND ts>=GREATEST($2::timestamptz,$4::timestamptz) AND ts<$3
               AND {_seg_pred('$1', '$5')})
           SELECT domain, SUM(bytes)::bigint AS bytes FROM u
           GROUP BY domain ORDER BY 2 DESC LIMIT 25""", ip, start, end, cut, mac)]

    total_up = sum(h["bytes_up"] for h in hourly)
    total_down = sum(h["bytes_down"] for h in hourly)
    total_inet = sum(h["bytes_internet"] for h in hourly)
    total_uncl = sum(h["bytes_unclassified"] for h in hourly)
    return {
        "ip": ip, "label": label, "start": start, "end": end,
        "hostname": ident["hostname"], "ad_user": ident["ad_user"],
        "mac": ident["mac"] or "", "vendor": ident["vendor"] or "",
        "first_seen": ident["first_seen"],
        "bytes_up": total_up, "bytes_down": total_down, "total": total_up + total_down,
        "bytes_internet": total_inet, "bytes_unclassified": total_uncl,
        "internet_since": await db.meta_get(pool, "internet_since"),
        "hourly": hourly,
        "peak_hours": sorted(hourly, key=lambda h: h["total"], reverse=True)[:10],
        "peak_window": peak,
        "apps": sorted(app_totals.values(), key=lambda a: a["total"], reverse=True),
        "categories": cats,
        "domains": domains,
        "apps_since": await _apps_since(pool),
    }


def _mbps(total: int, seconds: int) -> str:
    return f"{total * 8 / seconds / 1e6:.1f} Mbps".replace(".", ",")


def build_host_csv(data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Reporte de consumo por equipo - {data['ip']} - {data['label']}"])
    w.writerow(["Generado", datetime.now(_zone()).strftime("%Y-%m-%d %H:%M"), "Zona horaria", _tz_name()])
    w.writerow([_apps_note(data)])
    w.writerow([])
    w.writerow(["IP", data["ip"]])
    w.writerow(["Hostname", data["hostname"]])
    w.writerow(["Usuario AD", data["ad_user"]])
    w.writerow(["MAC", data["mac"], "Fabricante", data["vendor"]])
    w.writerow(["Subida (MB)", _mb(data["bytes_up"]), "Bajada (MB)", _mb(data["bytes_down"]),
                "Total (MB)", _mb(data["total"])])
    w.writerow(["Internet (MB)", _mb(data["bytes_internet"]),
                "Red interna (MB)", _mb(max(data["total"] - data["bytes_internet"]
                                            - data["bytes_unclassified"], 0)),
                "Sin clasificar (MB)", _mb(data["bytes_unclassified"])])
    pk = data["peak_window"]
    if pk:
        w.writerow([f"Pico ({pk['resolution']})", _fmt_window(pk), _mb(pk["total"]) + " MB",
                    _mbps(pk["total"], pk["seconds"]), _apps_summary(pk["apps"], 5)])
    w.writerow([])
    w.writerow(["Horas de mayor consumo"])
    w.writerow(["Fecha y hora", "Bajada (MB)", "Subida (MB)", "Total (MB)", "Internet (MB)",
                "Promedio", "Aplicaciones en esa hora"])
    for h in data["peak_hours"]:
        w.writerow([_fmt_ts(h["hour"]), _mb(h["bytes_down"]), _mb(h["bytes_up"]),
                    _mb(h["total"]), _inet_cell(h, _mb), _mbps(h["total"], 3600),
                    _apps_summary(h["apps"], 5)])
    w.writerow([])
    w.writerow(["Aplicaciones del período"])
    w.writerow(["Aplicación", "Categoría", "Bajada (MB)", "Subida (MB)", "Total (MB)", "% del equipo"])
    for a in data["apps"]:
        pct = 100 * a["total"] / data["total"] if data["total"] else 0
        w.writerow([a["app"], CATEGORY_LABELS.get(a["category"], a["category"]),
                    _mb(a["bytes_down"]), _mb(a["bytes_up"]), _mb(a["total"]),
                    f"{pct:.1f}".replace(".", ",")])
    w.writerow([])
    w.writerow(["A dónde fue el tráfico de internet (por sitio/dominio)"])
    w.writerow(["Dominio", "Total (MB)"])
    for d in data.get("domains", []):
        w.writerow([friendly_site(d["domain"]), _mb(d["bytes"])])
    w.writerow([])
    w.writerow(["Categoría", "Total (MB)"])
    for c in data["categories"]:
        w.writerow([CATEGORY_LABELS.get(c["category"], c["category"]), _mb(c["total"])])
    w.writerow([])
    w.writerow(["Detalle hora por hora"])
    w.writerow(["Fecha y hora", "Bajada (MB)", "Subida (MB)", "Total (MB)", "Internet (MB)",
                "Aplicación", "Categoría", "MB de la aplicación"])
    for h in data["hourly"]:
        if not h["total"]:
            continue
        base = [_fmt_ts(h["hour"]), _mb(h["bytes_down"]), _mb(h["bytes_up"]), _mb(h["total"]),
                _inet_cell(h, _mb)]
        if not h["apps"]:
            w.writerow(base + ["", "", ""])
        for a in h["apps"]:
            w.writerow(base + [a["app"], CATEGORY_LABELS.get(a["category"], a["category"]),
                               _mb(a["total"])])
    return buf.getvalue().encode("utf-8-sig")


def _hourly_chart(data: dict, width: float, height: float):
    """Barras de MB por hora de todo el período (horas sin tráfico = 0)."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.lib import colors

    by_hour = {_local(h["hour"]): h["total"] for h in data["hourly"]}
    slots, t = [], _local(data["start"]).replace(minute=0, second=0, microsecond=0)
    end = _local(data["end"])
    while t < end:
        slots.append(t)
        t += timedelta(hours=1)
    values = [by_hour.get(s, 0) / 1048576 for s in slots]
    multi_day = len(slots) > 24
    labels = [(s.strftime("%d/%m") if s.hour == 0 else "") if multi_day
              else (s.strftime("%H") if s.hour % 2 == 0 else "") for s in slots]

    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x, chart.y = 40, 22
    chart.width, chart.height = width - 55, height - 40
    chart.data = [values or [0]]
    chart.categoryAxis.categoryNames = labels or [""]
    chart.categoryAxis.labels.fontSize = 6
    chart.categoryAxis.tickShift = 0
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontSize = 6
    chart.valueAxis.labelTextFormat = lambda v: f"{v:,.0f}".replace(",", ".")
    chart.bars[0].fillColor = colors.HexColor("#0ea5e9")
    chart.bars[0].strokeColor = None
    chart.barSpacing = 0.5 if multi_day else 1
    d.add(chart)
    d.add(String(0, height - 10, "MB por hora", fontSize=7))
    return d


def build_host_pdf(data: dict) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    colors, styles, mm, Paragraph, Spacer, table = _pdf_kit()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=14 * mm, bottomMargin=12 * mm,
                            leftMargin=12 * mm, rightMargin=12 * mm)
    name = data["hostname"] or data["ip"]
    story = [
        Paragraph("Reporte de consumo por equipo", styles["Title"]),
        Paragraph(escape(f"{name} — {data['label']}"), styles["Heading2"]),
        Paragraph(f"Generado: {datetime.now(_zone()).strftime('%Y-%m-%d %H:%M')} · Horario: {_tz_name()} — "
                  "Fuente: netmon (metadatos de tráfico, sin inspección de contenido). "
                  + _apps_note(data), styles["Normal"]),
        Spacer(1, 4 * mm),
    ]

    ident = [["Dato", "Valor"],
             ["IP", data["ip"]],
             ["Hostname", data["hostname"] or "—"],
             ["Usuario AD", data["ad_user"] or "—"],
             ["MAC / Fabricante", f"{data['mac'] or '—'} {('· ' + data['vendor']) if data['vendor'] else ''}"],
             ["Visto por primera vez", _fmt_ts(data["first_seen"]) if data["first_seen"] else "—"],
             ["Consumo total", f"{fmt_mb(data['total'])}  (bajada {fmt_mb(data['bytes_down'])}, "
                               f"subida {fmt_mb(data['bytes_up'])})"],
             ["Internet / Red interna",
              f"{fmt_mb(data['bytes_internet'])} de internet · "
              f"{fmt_mb(max(data['total'] - data['bytes_internet'] - data['bytes_unclassified'], 0))} "
              f"dentro de la red"
              + (f" · {fmt_mb(data['bytes_unclassified'])} sin clasificar (anterior a la medición)"
                 if data["bytes_unclassified"] else "")],
             ["Horas con actividad", str(sum(1 for h in data["hourly"] if h["total"]))]]
    if data["peak_hours"] and data["peak_hours"][0]["total"]:
        ph = data["peak_hours"][0]
        ident.append(["Hora pico", f"{_fmt_ts(ph['hour'])} — {fmt_mb(ph['total'])} "
                                   f"(prom. {_mbps(ph['total'], 3600)}) — "
                                   f"{_apps_summary(ph['apps'], 3) or 'sin desglose'}"])
    pk = data["peak_window"]
    if pk:
        ident.append([f"Pico ({pk['resolution']})",
                      f"{_fmt_window(pk)} — {fmt_mb(pk['total'])} "
                      f"({_mbps(pk['total'], pk['seconds'])}) — "
                      f"{_apps_summary(pk['apps'], 3) or 'sin desglose'}"])
    story += [table(ident, [42, 144], font=8), Spacer(1, 5 * mm)]

    if not data["total"]:
        story += [Paragraph("Sin tráfico registrado para este equipo en el período.",
                            styles["Heading3"])]
    else:
        story += [_hourly_chart(data, 186 * mm, 60 * mm), Spacer(1, 4 * mm)]

        rows = [["Fecha y hora", "Bajada", "Subida", "Total", "Internet", "Promedio", "En qué se usó"]]
        rows += [[_fmt_ts(h["hour"]), fmt_mb(h["bytes_down"]), fmt_mb(h["bytes_up"]),
                  fmt_mb(h["total"]), _inet_cell(h, fmt_mb), _mbps(h["total"], 3600),
                  _apps_summary(h["apps"], 4) or "sin desglose"]
                 for h in data["peak_hours"] if h["total"]]
        story += [Paragraph("Horas de mayor consumo", styles["Heading3"]),
                  table(rows, [26, 18, 18, 18, 18, 18, 70], right_from=1), Spacer(1, 5 * mm)]

        if data["apps"]:
            rows = [["Aplicación", "Categoría", "Bajada", "Subida", "Total", "%"]]
            rows += [[a["app"], CATEGORY_LABELS.get(a["category"], a["category"]),
                      fmt_mb(a["bytes_down"]), fmt_mb(a["bytes_up"]), fmt_mb(a["total"]),
                      f"{100 * a['total'] / data['total']:.1f}%".replace(".", ",")]
                     for a in data["apps"][:30]]
            story += [Paragraph("En qué aplicaciones se consumió", styles["Heading3"]),
                      table(rows, [50, 30, 26, 26, 26, 18], right_from=2), Spacer(1, 5 * mm)]

        if data.get("domains"):
            rows = [["Sitio / dominio de internet", "Total"]]
            rows += [[friendly_site(d["domain"]), fmt_mb(d["bytes"])] for d in data["domains"][:20]]
            story += [Paragraph("A dónde fue el tráfico de internet (por sitio)", styles["Heading3"]),
                      Paragraph("Rescata lo que nDPI deja como QUIC/TLS genérico "
                                "(ej. pv-cdn.net = Prime Video, googlevideo.com = YouTube).",
                                styles["Italic"]),
                      table(rows, [70, 30], right_from=1, font=8.5), Spacer(1, 5 * mm)]

        if data["categories"]:
            rows = [["Categoría", "Total"]] + [
                [CATEGORY_LABELS.get(c["category"], c["category"]), fmt_mb(c["total"])]
                for c in data["categories"]]
            story += [Paragraph("Consumo por categoría", styles["Heading3"]),
                      table(rows, [60, 40], right_from=1, font=8.5), Spacer(1, 5 * mm)]

        rows = [["Fecha y hora", "Bajada", "Subida", "Total", "Internet", "Aplicaciones de esa hora"]]
        rows += [[_fmt_ts(h["hour"]), fmt_mb(h["bytes_down"]), fmt_mb(h["bytes_up"]),
                  fmt_mb(h["total"]), _inet_cell(h, fmt_mb),
                  _apps_summary(h["apps"], 4) or "sin desglose"]
                 for h in data["hourly"] if h["total"]]
        story += [Paragraph("Detalle hora por hora", styles["Heading3"]),
                  table(rows, [26, 19, 19, 19, 19, 84], right_from=1)]

    story += [Spacer(1, 8 * mm), _privacy_note(styles, Paragraph)]
    doc.build(story)
    return buf.getvalue()
