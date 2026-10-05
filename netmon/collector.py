"""Colector principal: ntopng -> PostgreSQL.

Cada NETMON_COLLECT_INTERVAL segundos (default 60):
  1. Pide a ntopng la lista de hosts activos y el detalle (contadores + nDPI)
     de cada host LOCAL.
  2. Calcula deltas contra el ciclo anterior (los contadores de ntopng son
     acumulados y se resetean si el host purga por inactividad).
  3. Persiste tráfico por minuto: total por host, por categoría de negocio
     (con overrides editables de category_map) y por aplicación a nivel red.
  4. Mantiene el inventario de dispositivos (MAC/IP/OUI/first-last seen).
  5. Evalúa las reglas de alerta configurables (alert_rules): dispositivo
     nuevo, cuota diaria por host, categoría prohibida, y peers en la
     blocklist de reputación (FireHOL level1) revisando los flujos activos.
  6. Rollups estilo RRD: minuto -> 5 min (14 días) -> hora (90 días),
     + limpieza por retención.

Correr con: python -m netmon.collector
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone

import asyncpg

from . import attribution, db, dnsmap
from .alerting import alert_once_per_day, load_rules, raise_alert
from .blocklist import Blocklist
from .categories import categorize
from .geo import country as geo_country
from .config import get_settings
from .ntopng_client import NtopngClient, as_num, own_host_entries, router_macs
from .oui import vendor_for_mac

log = logging.getLogger("netmon.collector")

# Tolerancia sobre la capacidad del enlace: el tiempo entre lecturas se mide en
# el colector, no en ntopng, y puede diferir unos segundos del real.
LINK_TOLERANCE = 1.25


def counter_delta(prev: int, cur: int, elapsed_s: float) -> int:
    """Bytes transferidos entre dos lecturas de un contador acumulado de ntopng.

    * Si el contador retrocedió (ntopng recreó el host/flujo o se leyó otra copia)
      no se sabe cuánto se transfirió: se DESCARTA la muestra (0). Tomar el
      acumulado nuevo como delta registró 23 GB en un minuto (auditoría H03).
    * Si el delta supera lo que el enlace puede transportar en el tiempo real
      transcurrido, es un dato imposible: también se descarta, no se recorta.
    """
    if cur < prev:
        log.debug("contador retrocedió (%d -> %d): muestra descartada", prev, cur)
        return 0
    d = cur - prev
    limit = get_settings().link_mbps * 1_000_000 / 8 * max(elapsed_s, 1.0) * LINK_TOLERANCE
    if d > limit:
        log.warning("delta imposible (%d bytes en %.0f s, tope %d): descartado", d, elapsed_s, limit)
        return 0
    return d


class HostCache:
    """Últimos contadores acumulados vistos por host, para calcular deltas.

    La clave es (ip, vlan): ntopng mantiene contadores separados para la misma
    IP en VLAN distintas, y mezclarlos bajo la IP sola produce deltas falsos.
    """

    def __init__(self) -> None:
        self.counters: dict[tuple[str, int], tuple[int, int]] = {}          # (ip, vlan) -> (sent, rcvd)
        self.ndpi: dict[tuple[str, int], dict[str, tuple[int, int]]] = {}   # (ip, vlan) -> proto -> (s, r)
        self.split: dict[tuple[str, int], tuple[int, int]] = {}             # (ip, vlan) -> (local, non_local)
        self.last_seen: dict[tuple[str, int], datetime] = {}
        self.read_at: dict[tuple[str, int], datetime] = {}   # última lectura de contadores

    @staticmethod
    def delta(prev: int, cur: int, elapsed_s: float) -> int:
        return counter_delta(prev, cur, elapsed_s)

    def evict_stale(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=30)
        for key in [key for key, ts in self.last_seen.items() if ts < cutoff]:
            self.counters.pop(key, None)
            self.ndpi.pop(key, None)
            self.split.pop(key, None)
            self.last_seen.pop(key, None)
            self.read_at.pop(key, None)


class FlowCache:
    """Bytes acumulados por instancia de flujo, para calcular deltas por minuto.

    La identidad es (cli_ip, cli_port, srv_ip, srv_port, l4): el puerto de
    origen efímero hace única cada conexión. Mismo criterio de delta que
    HostCache (si el contador baja, ntopng recreó el flujo).
    """

    def __init__(self) -> None:
        self.prev_fetch: float | None = None   # hora (epoch) de la lectura anterior de flujos
        self.bytes: dict[tuple, int] = {}
        self.last_seen: dict[tuple, datetime] = {}

    @staticmethod
    def delta(prev: int, cur: int, elapsed_s: float) -> int:
        return counter_delta(prev, cur, elapsed_s)

    def evict_stale(self, now: datetime) -> None:
        cutoff = now - timedelta(minutes=10)
        for fid in [fid for fid, ts in self.last_seen.items() if ts < cutoff]:
            self.bytes.pop(fid, None)
            self.last_seen.pop(fid, None)


_TWO_LEVEL = {"com", "net", "org", "gov", "gob", "edu", "co", "mil"}


def reg_domain(name: str) -> str:
    """Reduce un SNI/hostname al dominio registrable (últimas 2-3 etiquetas).

    'seektables.spotifycdn.com' -> 'spotifycdn.com'; 'x.y.gob.ar' -> 'y.gob.ar'.
    Devuelve '' si es vacío o una IP.
    """
    name = (name or "").strip(".").lower()
    # descartar vacíos, IPs, nombres internos (ntopng usa 'host@vlan', NetBIOS, .local)
    if not name or "@" in name or " " in name or ":" in name:
        return ""
    if name.replace(".", "").isdigit():
        return ""
    parts = name.split(".")
    if len(parts) < 2:
        return ""                         # etiqueta suelta: NetBIOS/hostname, no dominio
    tld = parts[-1]
    if tld in {"local", "lan", "internal", "home", "arpa", "corp"} or not tld.isalpha() or len(tld) < 2:
        return ""
    if len(parts) >= 3 and parts[-2] in _TWO_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def flow_scope(remote_ip: str) -> str:
    """'internet' sólo si el destino es una IP pública (global).

    La otra sede por VPN (192.168.60.x, 192.168.150.x...) no está en
    NETMON_LOCAL_NETWORKS pero es tráfico interno: contarla como internet
    inflaba Sitios/Conexiones (hasta 29 % del "internet"; auditoría H33) y no
    coincidía con el contador de internet del host, que sale de ntopng (todo
    RFC1918 es local para ntopng).
    """
    try:
        ip = ipaddress.ip_address(remote_ip)
        # multicast (p. ej. 239.255.255.250 SSDP) figura como "global" en ipaddress
        return "internet" if ip.is_global and not ip.is_multicast else "interno"
    except ValueError:
        return "interno"


def first_sight_bytes(first_seen: int, cur: int, prev_fetch: float | None, now: float) -> int:
    """Bytes a contar la primera vez que se ve un flujo.

    Si ntopng dice que el flujo empezó después de la lectura anterior, nunca pudo
    haberse contado: todos sus bytes son de este intervalo (así entran las
    conexiones cortas, que antes se perdían porque la primera vista contaba 0).
    Si empezó antes, parte de sus bytes es anterior y no se sabe cuánta: 0.
    """
    if prev_fetch is None or not first_seen or first_seen < prev_fetch:
        return 0
    return counter_delta(0, cur, max(now - first_seen, 1.0))


async def collect_flows(
    pool: asyncpg.Pool, nt: NtopngClient, fc: FlowCache, minute: datetime, now: datetime
) -> int:
    """Registra las conexiones (flujos) del ciclo en flows_min. Devuelve nº de filas."""
    s = get_settings()
    if not s.flowlog_enabled:
        return 0
    try:
        fetch_start = time.time()
        flows = await nt.top_flows(s.flowlog_top)
    except Exception as exc:
        log.warning("no pude leer flujos para el registro: %s", str(exc)[:200])
        return 0
    rows: dict[tuple, list] = {}   # (local, remote, port, l7, scope, direction) -> [bytes, domain]
    dom_rows: dict[str, int] = {}  # dominio -> bytes (internet, a nivel red)
    # Un mismo flujo aparece en varias VLAN cuando se rutea entre VLAN (el SPAN ve
    # cada tramo). Cada copia tiene su propio contador, así que la identidad lleva
    # la VLAN (sin ella las copias se pisaban y se guardaba el acumulado entero).
    # Para no contar dos veces: si hay copia en una VLAN troncal (≠ 0/1) se usa
    # sólo esa (ve las dos direcciones); si no, se suman la 0 y la 1 (el espejo del
    # puerto de acceso separa fw->host sin tag y host->fw con VLAN 1).
    groups: dict[tuple, list] = {}
    for raw in flows:
        f = nt.flow_row(raw)
        cli, srv = f["cli_ip"], f["srv_ip"]
        if not cli or not srv:
            continue
        cli_l, srv_l = s.is_local_ip(cli), s.is_local_ip(srv)
        if not (cli_l or srv_l):
            continue                      # ambas externas: no nos incumbe
        if (f["l7"] or "").upper() == "RTSP" or f["srv_port"] == 554:
            continue                      # cámaras: excluidas por pedido
        fid5 = (cli, f["cli_port"], srv, f["srv_port"], f["l4"])
        groups.setdefault(fid5, []).append((int(as_num(raw.get("vlan") or 0)), f))
    selected = []
    for fid5, copies in groups.items():
        trunk = [c for c in copies if c[0] not in (0, 1)]
        for vlan, f in ([min(trunk, key=lambda c: c[0])] if trunk else copies):
            selected.append((fid5 + (vlan,), f))
    for fid, f in selected:
        cli, srv = f["cli_ip"], f["srv_ip"]
        cli_l, srv_l = s.is_local_ip(cli), s.is_local_ip(srv)
        cur = int(f["bytes"])
        first = fid not in fc.bytes
        if first:
            # conexión nueva desde la lectura anterior: todos sus bytes son de este intervalo
            d = first_sight_bytes(f["first_seen"], cur, fc.prev_fetch, fetch_start)
        else:
            elapsed = (now - fc.last_seen[fid]).total_seconds()
            d = fc.delta(fc.bytes[fid], cur, elapsed)
        fc.bytes[fid] = cur
        fc.last_seen[fid] = now
        if d <= 0:
            continue
        if cli_l:                         # el que inició (cliente) es local
            local, remote, direction = cli, srv, "saliente"
        else:
            local, remote, direction = srv, cli, "entrante"
        scope = "interno" if (cli_l and srv_l) else flow_scope(remote)
        domain = reg_domain(f["srv_name"]) if scope == "internet" else ""
        key = (local, remote, f["srv_port"], f["l7"] or "otro", scope, direction)
        acc = rows.setdefault(key, [0, "", ""])      # [bytes, dominio, origen del nombre]
        acc[0] += d
        if domain:
            acc[1], acc[2] = domain, "sni"
            dom_rows[domain] = dom_rows.get(domain, 0) + d
    fc.evict_stale(now)
    fc.prev_fetch = fetch_start
    # conexiones de internet sin SNI (QUIC/ECH): nombre por el mapa DNS pasivo
    try:
        await label_by_dns(pool, rows, dom_rows, now)
    except Exception:
        log.exception("no pude nombrar conexiones por DNS (se registran sin nombre)")
    if rows:
        await pool.executemany(
            """INSERT INTO flows_min (ts, local_ip, remote_ip, srv_port, l7, scope, direction, bytes,
                                     domain, domain_src)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
               ON CONFLICT (ts, local_ip, remote_ip, srv_port, l7) DO UPDATE SET
                 bytes = flows_min.bytes + EXCLUDED.bytes,
                 scope = EXCLUDED.scope, direction = EXCLUDED.direction,
                 domain = COALESCE(NULLIF(EXCLUDED.domain, ''), flows_min.domain),
                 domain_src = CASE WHEN EXCLUDED.domain <> '' THEN EXCLUDED.domain_src
                                   ELSE flows_min.domain_src END""",
            [(minute, k[0], k[1], k[2], k[3], k[4], k[5], v[0], v[1], v[2]) for k, v in rows.items()],
        )
    if dom_rows:
        await pool.executemany(
            """INSERT INTO domain_min (ts, domain, bytes) VALUES ($1, $2, $3)
               ON CONFLICT (ts, domain) DO UPDATE SET bytes = domain_min.bytes + EXCLUDED.bytes""",
            [(minute, dom, b) for dom, b in dom_rows.items()],
        )
    return len(rows)


async def label_by_dns(pool: asyncpg.Pool, rows: dict, dom_rows: dict, now: datetime) -> None:
    """Nombra por el mapa DNS pasivo las conexiones de internet sin SNI.

    rows: {(local, remoto, puerto, l7, ámbito, sentido): [bytes, dominio, origen]}.
    El SNI (lo que el equipo dijo al conectarse) tiene prioridad sobre el DNS.
    """
    pending = [k for k, v in rows.items() if k[4] == "internet" and not v[1]]
    if not pending:
        return
    names = await dnsmap.lookup(pool, list({(k[0], k[1]) for k in pending}), now)
    for k in pending:
        dom = reg_domain(names.get((k[0], k[1]), ""))
        if dom:
            v = rows[k]
            v[1], v[2] = dom, "dns"
            dom_rows[dom] = dom_rows.get(dom, 0) + v[0]


async def load_category_overrides(pool: asyncpg.Pool) -> dict[str, str]:
    rows = await pool.fetch("SELECT app, category FROM category_map")
    return {r["app"]: r["category"] for r in rows}


async def collect_cycle(
    pool: asyncpg.Pool, nt: NtopngClient, cache: HostCache, bl: Blocklist,
    fcache: FlowCache | None = None
) -> None:
    s = get_settings()
    started = asyncio.get_event_loop().time()
    now = datetime.now(timezone.utc)
    minute = now.replace(second=0, microsecond=0)
    overrides = await load_category_overrides(pool)
    rules = await load_rules(pool)

    hosts = await nt.active_hosts()
    local_rows = []
    entries = []                            # (ip, mac, vlan) de todo el snapshot
    listed: dict[tuple, tuple[int, int]] = {}   # (ip, vlan) -> contadores del listado
    for row in hosts:
        ip, mac, name, _, _ = nt.host_row_basics(row)
        if not ip:
            continue
        entries.append((ip, mac, row.get("vlan", 0)))
        listed[(ip, row.get("vlan", 0))] = nt.row_counters(row)
        if s.is_local_ip(ip):
            local_rows.append((ip, mac, name))
    # inventario sin MACs de router (la VRRP "cambiaba de IP" cada ciclo) ni repetidos
    routers = router_macs(entries)
    local_rows = list(dict.fromkeys(
        (ip, mac, name) for ip, mac, name in local_rows if (mac or "").lower() not in routers))
    # sólo las entradas del host real: descarta la copia inter-VLAN con MAC de router
    weights = {k: sent + rcvd for k, (sent, rcvd) in listed.items()}
    own_rows = [(ip, vlan) for ip, vlan in own_host_entries(entries, weights) if s.is_local_ip(ip)]
    # detalle sólo si el contador se movió desde el último detalle: sin cambio no
    # hay delta que registrar (ahorra cientos de consultas a ntopng por ciclo)
    detail_rows = []
    for ip, vlan in own_rows:
        key = (ip, vlan)
        if key in cache.counters and cache.counters[key] == listed.get(key):
            cache.last_seen[key] = now
        else:
            detail_rows.append((ip, vlan))

    # --- detalle por host con concurrencia limitada -------------------------
    sem = asyncio.Semaphore(8)

    async def fetch_detail(ip: str, vlan: int) -> tuple[str, int, dict | None]:
        async with sem:
            try:
                return ip, vlan, await nt.host_data(f"{ip}@{vlan}" if vlan else ip)
            except Exception as exc:  # host pudo purgar entre llamadas
                log.debug("host_data(%s@%s) falló: %s", ip, vlan, exc)
                return ip, vlan, None

    details = await asyncio.gather(*(fetch_detail(ip, vlan) for ip, vlan in detail_rows))

    traffic_rows: list[tuple] = []          # (ts, ip, up, down, internet)
    missing_counters = 0                    # detalles sin bytes.sent/rcvd (H16)
    cat_rows: dict[tuple, list[int]] = {}   # (ts, ip, cat) -> [up, down]
    app_rows: dict[str, list] = {}          # app -> [cat, up, down] (toda la red)
    host_app_rows: dict[tuple, list] = {}   # (ts, ip, app) -> [cat, up, down]

    # traffic_min sigue siendo por IP: las filas de la misma IP en VLAN
    # distintas se suman en el upsert aditivo (misma (ts, ip)).
    for ip, vlan, detail in details:
        if not detail:
            continue
        key = (ip, vlan)
        cache.last_seen[key] = now
        elapsed = (now - cache.read_at[key]).total_seconds() if key in cache.read_at \
            else s.collect_interval
        counters = nt.host_counters(detail)
        if counters is None:
            missing_counters += 1
            continue                     # sin contadores no hay delta: no se inventa 0
        cache.read_at[key] = now
        sent, rcvd = counters
        prev_s, prev_r = cache.counters.get(key, (0, 0))
        first_time = key not in cache.counters
        d_up = 0 if first_time else cache.delta(prev_s, sent, elapsed)
        d_down = 0 if first_time else cache.delta(prev_r, rcvd, elapsed)
        cache.counters[key] = (sent, rcvd)

        # parte de internet (non_local): nunca más que el total del delta
        d_inet = 0
        split = nt.host_split(detail)
        if split is not None:
            prev_split = cache.split.get(key)
            if not first_time and prev_split is not None:
                d_inet = min(cache.delta(prev_split[1], split[1], elapsed),
                             d_up + d_down)
            cache.split[key] = split

        if d_up or d_down:
            traffic_rows.append((minute, ip, d_up, d_down, d_inet))

        # --- desglose nDPI -> categorías (por host) y apps (a nivel red) -----
        ndpi_now = nt.host_ndpi(detail)
        ndpi_prev = cache.ndpi.get(key, {})
        for proto, (ps, pr) in ndpi_now.items():
            os_, or_ = ndpi_prev.get(proto, (0, 0))
            cd_up = 0 if first_time else cache.delta(os_, ps, elapsed)
            cd_down = 0 if first_time else cache.delta(or_, pr, elapsed)
            if not (cd_up or cd_down):
                continue
            cat = categorize(proto, overrides)
            acc = cat_rows.setdefault((minute, ip, cat), [0, 0])
            acc[0] += cd_up
            acc[1] += cd_down
            app = app_rows.setdefault(proto, [cat, 0, 0])
            app[1] += cd_up
            app[2] += cd_down
            host_app = host_app_rows.setdefault((minute, ip, proto), [cat, 0, 0])
            host_app[1] += cd_up
            host_app[2] += cd_down
        cache.ndpi[key] = ndpi_now

    cache.evict_stale(now)
    got = sum(1 for _, _, d in details if d)
    if missing_counters:
        log.error("%d de %d detalles de ntopng sin contadores de bytes", missing_counters, got)
    if got >= 20 and missing_counters > got / 2:
        await alert_once_per_day(
            pool, "collector", "ntopng-sin-contadores", "critical",
            f"ntopng devolvió {missing_counters} de {got} hosts sin contadores de bytes: "
            f"¿cambió el formato de su API? El consumo no se está registrando.",
            {"missing": missing_counters, "total": got})

    # --- persistencia (upsert aditivo: varios ciclos pueden caer en el mismo minuto)
    async with pool.acquire() as conn:
        if traffic_rows:
            await conn.executemany(
                """INSERT INTO traffic_min (ts, ip, bytes_up, bytes_down, bytes_internet)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (ts, ip) DO UPDATE SET
                     bytes_up   = traffic_min.bytes_up   + EXCLUDED.bytes_up,
                     bytes_down = traffic_min.bytes_down + EXCLUDED.bytes_down,
                     bytes_internet = traffic_min.bytes_internet + EXCLUDED.bytes_internet""",
                traffic_rows,
            )
        if cat_rows:
            await conn.executemany(
                """INSERT INTO traffic_cat_min (ts, ip, category, bytes_up, bytes_down)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (ts, ip, category) DO UPDATE SET
                     bytes_up   = traffic_cat_min.bytes_up   + EXCLUDED.bytes_up,
                     bytes_down = traffic_cat_min.bytes_down + EXCLUDED.bytes_down""",
                [(ts, ip, cat, v[0], v[1]) for (ts, ip, cat), v in cat_rows.items()],
            )
        if app_rows:
            await conn.executemany(
                """INSERT INTO app_min (ts, app, category, bytes_up, bytes_down)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (ts, app) DO UPDATE SET
                     bytes_up   = app_min.bytes_up   + EXCLUDED.bytes_up,
                     bytes_down = app_min.bytes_down + EXCLUDED.bytes_down""",
                [(minute, app, v[0], v[1], v[2]) for app, v in app_rows.items()],
            )
        if host_app_rows:
            await conn.executemany(
                """INSERT INTO traffic_app_host_min (ts, ip, app, category, bytes_up, bytes_down)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (ts, ip, app) DO UPDATE SET
                     bytes_up   = traffic_app_host_min.bytes_up   + EXCLUDED.bytes_up,
                     bytes_down = traffic_app_host_min.bytes_down + EXCLUDED.bytes_down""",
                [(ts, ip, app, v[0], v[1], v[2])
                 for (ts, ip, app), v in host_app_rows.items()],
            )

    flow_rows = 0
    if fcache is not None:
        try:
            flow_rows = await collect_flows(pool, nt, fcache, minute, now)
        except Exception:
            log.exception("registro de flujos falló (el resto del ciclo ya persistió)")

    await update_inventory(pool, local_rows, rules)
    # historial IP -> equipo (auditoría H04): una fila por (ip, mac) propia vista
    names = {(ip, mac): name for ip, mac, name in local_rows}
    assign_rows = assignment_rows(entries, set(own_rows), routers, names)
    try:
        await attribution.record_assignments(pool, assign_rows, now)
    except Exception:
        log.exception("no pude registrar el historial IP -> equipo")
    await evaluate_rules(pool, nt, bl, rules)
    log.info(
        "ciclo ok en %.1fs: %d hosts locales, %d detalles pedidos de %d, "
        "%d filas tráfico, %d categorías, %d apps, %d flujos",
        asyncio.get_event_loop().time() - started, len(local_rows), len(detail_rows),
        len(own_rows), len(traffic_rows), len(cat_rows), len(app_rows), flow_rows,
    )


async def evaluate_rules(
    pool: asyncpg.Pool, nt: NtopngClient, bl: Blocklist, rules: dict
) -> None:
    """Reglas de tráfico configurables. Dedupe: una alerta por clave por día."""
    s = get_settings()

    # --- Cuota diaria de INTERNET por host --------------------------------------
    # Sólo tráfico con internet: backups, cámaras->grabador o escritorio remoto
    # internos no cuentan. 'exclude' = IPs que nunca alertan (ej. un proxy).
    rule = rules.get("quota_daily", {})
    if rule.get("enabled"):
        params = rule.get("params", {})
        gb = float(params.get("gb", 15))
        exclude = [str(x).strip() for x in params.get("exclude", []) if str(x).strip()]
        offenders = await pool.fetch(
            f"""SELECT host(ip) AS ip, SUM(bytes_internet) AS total
               FROM traffic_min WHERE ts >= {db.day_start_sql()}
                 AND NOT (host(ip) = ANY($2::text[]))
               GROUP BY ip HAVING SUM(bytes_internet) > $1""",
            int(gb * 1024 ** 3), exclude,
        )
        for row in offenders:
            total_gb = row["total"] / 1024 ** 3
            name = await pool.fetchval(
                "SELECT hostname FROM hostnames WHERE ip = $1::inet", row["ip"])
            who = f"{row['ip']} ({name})" if name else row["ip"]
            await alert_once_per_day(
                pool, "quota_daily", f"{row['ip']}:internet",
                rule.get("severity", "warning"),
                f"El equipo {who} lleva {total_gb:.1f} GB de internet hoy "
                f"(umbral: {gb:g} GB)",
                {"ip": row["ip"], "gb": round(float(total_gb), 1), "scope": "internet"},
            )

    # --- Espacio en disco del servidor ---------------------------------------------
    # En este equipo también viven Wazuh (Docker), ntopng y Suricata: cualquiera
    # puede llenar la partición. Aviso en 'pct' y crítico en 'crit_pct'.
    rule = rules.get("disk_usage", {})
    if rule.get("enabled"):
        params = rule.get("params", {})
        warn_pct = float(params.get("pct", 80))
        crit_pct = float(params.get("crit_pct", 90))
        for path in params.get("paths", ["/"]):
            try:
                du = shutil.disk_usage(path)
            except OSError:
                log.warning("disk_usage: no pude leer %s", path)
                continue
            pct = 100.0 * du.used / (du.used + du.free)   # como df: sin el 5% reservado a root
            if pct < warn_pct:
                continue
            severity = "critical" if pct >= crit_pct else rule.get("severity", "warning")
            free_gb, total_gb = du.free / 1024 ** 3, du.total / 1024 ** 3
            await alert_once_per_day(
                pool, "disk_usage", f"{path}:{severity}", severity,
                f"Disco {path} de netmon-srv al {pct:.0f}%: quedan {free_gb:.0f} GB libres "
                f"de {total_gb:.0f} GB (umbral {warn_pct:g}% / crítico {crit_pct:g}%)",
                {"path": path, "pct": round(pct, 1), "free_gb": round(free_gb, 1)},
            )

    # --- Categoría prohibida (ej. P2P) -----------------------------------------
    rule = rules.get("banned_category", {})
    if rule.get("enabled"):
        params = rule.get("params", {})
        banned = params.get("categories", ["p2p"])
        min_bytes = int(float(params.get("min_mb", 10)) * 1024 ** 2)
        offenders = await pool.fetch(
            f"""SELECT ip::text AS ip, category, SUM(bytes_up + bytes_down) AS total
               FROM traffic_cat_min
               WHERE ts >= {db.day_start_sql()} AND category = ANY($1)
               GROUP BY ip, category
               HAVING SUM(bytes_up + bytes_down) > $2""",
            banned, min_bytes,
        )
        for row in offenders:
            mb = row["total"] / 1024 ** 2
            await alert_once_per_day(
                pool, "banned_category", f"{row['ip']}:{row['category']}",
                rule.get("severity", "warning"),
                f"Uso de categoría prohibida '{row['category']}' en {row['ip']}: "
                f"{mb:.0f} MB hoy",
                {"ip": row["ip"], "category": row["category"], "mb": round(mb)},
            )

    # --- Peers en blocklist de reputación ----------------------------------------
    rule = rules.get("blocklist", {})
    if rule.get("enabled") and bl.available:
        try:
            flows = await nt.active_flows()
        except Exception:
            log.debug("no pude leer flujos para chequear blocklist")
            flows = []
        for raw in flows:
            f = nt.flow_row(raw)
            local_ip, remote_ip = "", ""
            if s.is_local_ip(f["cli_ip"]) and not s.is_local_ip(f["srv_ip"]):
                local_ip, remote_ip = f["cli_ip"], f["srv_ip"]
            elif s.is_local_ip(f["srv_ip"]) and not s.is_local_ip(f["cli_ip"]):
                local_ip, remote_ip = f["srv_ip"], f["cli_ip"]
            if remote_ip and bl.contains(remote_ip):
                await alert_once_per_day(
                    pool, "blocklist", f"{local_ip}>{remote_ip}",
                    rule.get("severity", "critical"),
                    f"Host interno {local_ip} conectado a IP de mala reputación "
                    f"{remote_ip} (puerto {f['srv_port']}, app {f['l7'] or '?'})",
                    {"ip": local_ip, "remote": remote_ip,
                     "port": f["srv_port"], "l7": f["l7"]},
                )

    # --- Escaneo / movimiento lateral: un equipo que habla con muchos internos --
    rule = rules.get("internal_fanout", {})
    if rule.get("enabled"):
        maxdst = int(rule.get("params", {}).get("max_destinos", 50))
        offenders = await pool.fetch(
            """SELECT host(local_ip) AS ip, COUNT(DISTINCT remote_ip) AS n
               FROM flows_min WHERE ts >= now() - interval '1 hour' AND scope = 'interno'
               GROUP BY local_ip HAVING COUNT(DISTINCT remote_ip) > $1
               ORDER BY 2 DESC""", maxdst)
        for row in offenders:
            name = await pool.fetchval("SELECT hostname FROM hostnames WHERE ip=$1::inet", row["ip"])
            who = f"{row['ip']} ({name})" if name else row["ip"]
            await alert_once_per_day(
                pool, "internal_fanout", row["ip"], rule.get("severity", "warning"),
                f"El equipo {who} habló con {row['n']} equipos internos distintos en 1 h "
                f"(umbral {maxdst}): posible escaneo o movimiento lateral",
                {"ip": row["ip"], "destinos": int(row["n"])})

    # --- Subida anómala: exfiltración o backup mal configurado -------------------
    rule = rules.get("upload_spike", {})
    if rule.get("enabled"):
        params = rule.get("params", {})
        floor_mb = float(params.get("min_mb", 500))
        factor = float(params.get("factor", 5))
        offenders = await pool.fetch(
            """WITH last AS (
                   SELECT ip, SUM(bytes_up) AS up FROM traffic_hour
                   WHERE ts = date_trunc('hour', now()) - interval '1 hour' GROUP BY ip),
               base AS (
                   SELECT ip, AVG(hup) AS avg_up FROM (
                       SELECT ip, date_trunc('hour', ts) h, SUM(bytes_up) hup FROM traffic_hour
                       WHERE ts >= now() - interval '7 days'
                         AND ts < date_trunc('hour', now()) - interval '1 hour'
                       GROUP BY ip, 2) x GROUP BY ip)
               SELECT host(l.ip) AS ip, l.up, COALESCE(b.avg_up, 0) AS avg_up
               FROM last l LEFT JOIN base b USING (ip)
               WHERE l.up > $1 AND l.up > $2 * GREATEST(COALESCE(b.avg_up,0), 1)""",
            int(floor_mb * 1024**2), factor)
        for row in offenders:
            name = await pool.fetchval("SELECT hostname FROM hostnames WHERE ip=$1::inet", row["ip"])
            who = f"{row['ip']} ({name})" if name else row["ip"]
            up_gb = float(row["up"]) / 1024**3; avg_mb = float(row["avg_up"]) / 1024**2
            await alert_once_per_day(
                pool, "upload_spike", row["ip"], rule.get("severity", "warning"),
                f"El equipo {who} subió {up_gb:.1f} GB en la última hora "
                f"(promedio habitual {avg_mb:.0f} MB/h): revisar posible exfiltración o backup",
                {"ip": row["ip"], "up_gb": round(up_gb, 2)})

    # --- Tráfico a países inusuales ---------------------------------------------
    rule = rules.get("unusual_country", {})
    if rule.get("enabled"):
        params = rule.get("params", {})
        allow = {c.strip().upper() for c in params.get("paises_ok", ["AR", "US"]) if c.strip()}
        min_mb = float(params.get("min_mb", 200))
        remotes = await pool.fetch(
            """SELECT host(remote_ip) AS ip, SUM(bytes) AS b FROM flows_min
               WHERE ts >= now() - interval '1 hour' AND scope = 'internet'
               GROUP BY remote_ip""")
        by_country: dict[str, dict] = {}   # país -> {"bytes":, "ip":, "ip_bytes":}
        for r in remotes:
            c = (geo_country(r["ip"], s.geoip_mmdb) or "").upper()
            if not c or c in allow:
                continue
            acc = by_country.setdefault(c, {"bytes": 0, "ip": "", "ip_bytes": 0})
            acc["bytes"] += float(r["b"])
            if float(r["b"]) > acc["ip_bytes"]:
                acc["ip"], acc["ip_bytes"] = r["ip"], float(r["b"])
        for c, acc in by_country.items():
            if acc["bytes"] < min_mb * 1024**2:
                continue
            await alert_once_per_day(
                pool, "unusual_country", c, rule.get("severity", "warning"),
                f"Tráfico a país inusual {c}: {acc['bytes']/1024**2:.0f} MB en 1 h "
                f"(ej. {acc['ip']}); permitidos: {', '.join(sorted(allow)) or 'ninguno'}",
                {"country": c, "mb": int(acc['bytes']/1024**2)})


async def update_inventory(
    pool: asyncpg.Pool, local_rows: list[tuple], rules: dict
) -> None:
    """Upsert de dispositivos por MAC + alerta de dispositivo nuevo."""
    s = get_settings()
    nd_rule = rules.get("new_device", {"enabled": True, "severity": "info"})
    for ip, mac, name in local_rows:
        mac = (mac or "").lower()
        if not mac or mac == "00:00:00:00:00:00":
            continue  # sin L2 real (host remoto o dato incompleto)
        # el nombre "ip" repetido como hostname no aporta
        name = "" if name == ip else name

        async with pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT mac FROM devices WHERE mac = $1", mac)
            hostname = await conn.fetchval(
                "SELECT hostname FROM hostnames WHERE ip = $1", ip
            )
            display_name = hostname or name or None

            if existing:
                await conn.execute(
                    """UPDATE devices SET ip = $2, last_seen = now(),
                              hostname = COALESCE($3, hostname),
                              vendor = CASE WHEN vendor IS NULL OR vendor = ''
                                            THEN $4 ELSE vendor END
                       WHERE mac = $1""",
                    mac, ip, display_name, vendor_for_mac(mac, s.oui_csv),
                )
            else:
                vendor = vendor_for_mac(mac, s.oui_csv)
                await conn.execute(
                    """INSERT INTO devices (mac, ip, hostname, vendor)
                       VALUES ($1, $2, $3, $4) ON CONFLICT (mac) DO NOTHING""",
                    mac, ip, display_name, vendor,
                )
                if nd_rule.get("enabled", True):
                    msg = f"Dispositivo nuevo en la red: {mac} ({vendor or 'fabricante desconocido'}) IP {ip}" \
                          + (f" hostname {display_name}" if display_name else "")
                    await raise_alert(pool, "new_device",
                                      nd_rule.get("severity", "info"), msg,
                                      {"mac": mac, "ip": ip, "vendor": vendor})

        # sincronizar nombre disectado por ntopng al cache de hostnames (prio 30)
        if name:
            await pool.execute(
                """INSERT INTO hostnames (ip, hostname, source, prio)
                   VALUES ($1, $2, 'ntopng', 30)
                   ON CONFLICT (ip) DO UPDATE
                     SET hostname = EXCLUDED.hostname, resolved_at = now()
                   WHERE hostnames.prio >= 30""",
                ip, name,
            )


async def rollup_and_retention(pool: asyncpg.Pool) -> None:
    """Rollups estilo RRD (min -> 5 min -> hora) + borrado por retención.

    Idempotente: usa las marcas meta_kv['rollup5_until'] y ['rollup_until']
    para no re-agregar. Los reportes leen 'rollup_until' para combinar
    hour+min sin contar dos veces.
    """
    s = get_settings()
    now = datetime.now(timezone.utc)

    # --- tier 5 minutos -------------------------------------------------------
    current_5m = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    raw5 = await db.meta_get(pool, "rollup5_until")
    rollup5_until = (
        datetime.fromisoformat(raw5) if raw5 else current_5m - timedelta(minutes=10)
    )
    if rollup5_until < current_5m:
        await pool.execute(
            """INSERT INTO traffic_5min (ts, ip, bytes_up, bytes_down, bytes_internet)
               SELECT to_timestamp(floor(extract(epoch FROM ts) / 300) * 300),
                      ip, SUM(bytes_up), SUM(bytes_down), SUM(bytes_internet)
               FROM traffic_min WHERE ts >= $1 AND ts < $2
               GROUP BY 1, 2
               ON CONFLICT (ts, ip) DO UPDATE SET
                 bytes_up = EXCLUDED.bytes_up, bytes_down = EXCLUDED.bytes_down,
                 bytes_internet = EXCLUDED.bytes_internet""",
            rollup5_until, current_5m,
        )
        await db.meta_set(pool, "rollup5_until", current_5m.isoformat())

    # --- tier hora --------------------------------------------------------------
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    raw = await db.meta_get(pool, "rollup_until")
    rollup_until = (
        datetime.fromisoformat(raw) if raw
        else current_hour - timedelta(hours=2)
    )
    if rollup_until >= current_hour:
        return  # nada nuevo que consolidar en el tier horario

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO traffic_hour (ts, ip, bytes_up, bytes_down, bytes_internet)
               SELECT date_trunc('hour', ts), ip, SUM(bytes_up), SUM(bytes_down),
                      SUM(bytes_internet)
               FROM traffic_min WHERE ts >= $1 AND ts < $2
               GROUP BY 1, 2
               ON CONFLICT (ts, ip) DO UPDATE SET
                 bytes_up = EXCLUDED.bytes_up, bytes_down = EXCLUDED.bytes_down,
                 bytes_internet = EXCLUDED.bytes_internet""",
            rollup_until, current_hour,
        )
        await conn.execute(
            """INSERT INTO traffic_cat_hour (ts, ip, category, bytes_up, bytes_down)
               SELECT date_trunc('hour', ts), ip, category, SUM(bytes_up), SUM(bytes_down)
               FROM traffic_cat_min WHERE ts >= $1 AND ts < $2
               GROUP BY 1, 2, 3
               ON CONFLICT (ts, ip, category) DO UPDATE SET
                 bytes_up = EXCLUDED.bytes_up, bytes_down = EXCLUDED.bytes_down""",
            rollup_until, current_hour,
        )
        await conn.execute(
            """INSERT INTO app_hour (ts, app, category, bytes_up, bytes_down)
               SELECT date_trunc('hour', ts), app, MAX(category),
                      SUM(bytes_up), SUM(bytes_down)
               FROM app_min WHERE ts >= $1 AND ts < $2
               GROUP BY 1, 2
               ON CONFLICT (ts, app) DO UPDATE SET
                 bytes_up = EXCLUDED.bytes_up, bytes_down = EXCLUDED.bytes_down,
                 category = EXCLUDED.category""",
            rollup_until, current_hour,
        )
        await conn.execute(
            """INSERT INTO traffic_app_host_hour (ts, ip, app, category, bytes_up, bytes_down)
               SELECT date_trunc('hour', ts), ip, app, MAX(category),
                      SUM(bytes_up), SUM(bytes_down)
               FROM traffic_app_host_min WHERE ts >= $1 AND ts < $2
               GROUP BY 1, 2, 3
               ON CONFLICT (ts, ip, app) DO UPDATE SET
                 bytes_up = EXCLUDED.bytes_up, bytes_down = EXCLUDED.bytes_down,
                 category = EXCLUDED.category""",
            rollup_until, current_hour,
        )
        await conn.execute(
            """INSERT INTO flows_hour (ts, local_ip, remote_ip, srv_port, l7,
                                       scope, direction, bytes, domain, domain_src)
               SELECT date_trunc('hour', ts), local_ip, remote_ip, srv_port, l7,
                      MAX(scope), MAX(direction), SUM(bytes), MAX(domain),
                      -- si algún minuto lo nombró el SNI, gana 'sni' ('sni' > 'dns')
                      MAX(domain_src)
               FROM flows_min WHERE ts >= $1 AND ts < $2
               GROUP BY 1, 2, 3, 4, 5
               ON CONFLICT (ts, local_ip, remote_ip, srv_port, l7) DO UPDATE SET
                 bytes = EXCLUDED.bytes, scope = EXCLUDED.scope,
                 direction = EXCLUDED.direction, domain = EXCLUDED.domain,
                 domain_src = EXCLUDED.domain_src""",
            rollup_until, current_hour,
        )
        await conn.execute(
            """INSERT INTO domain_hour (ts, domain, bytes)
               SELECT date_trunc('hour', ts), domain, SUM(bytes)
               FROM domain_min WHERE ts >= $1 AND ts < $2 GROUP BY 1, 2
               ON CONFLICT (ts, domain) DO UPDATE SET bytes = EXCLUDED.bytes""",
            rollup_until, current_hour,
        )
        # Retención por tier
        for table, keep in (
            ("traffic_min", f"{s.retention_min_hours} hours"),
            ("traffic_cat_min", f"{s.retention_min_hours} hours"),
            ("app_min", f"{s.retention_min_hours} hours"),
            ("traffic_5min", f"{s.retention_5min_days} days"),
            ("traffic_hour", f"{s.retention_hour_days} days"),
            ("traffic_cat_hour", f"{s.retention_hour_days} days"),
            ("app_hour", f"{s.retention_hour_days} days"),
            ("traffic_app_host_min", f"{s.retention_min_hours} hours"),
            ("traffic_app_host_hour", f"{s.retention_hour_days} days"),
            ("flows_min", f"{s.retention_min_hours} hours"),
            ("flows_hour", f"{s.retention_hour_days} days"),
            ("domain_min", f"{s.retention_min_hours} hours"),
            ("domain_hour", f"{s.retention_hour_days} days"),
            ("ping_min", f"{s.ping_retention_days} days"),
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE ts < now() - ($1::text)::interval", keep
            )
        await conn.execute(
            "DELETE FROM alerts WHERE ts < now() - interval '180 days'"
        )
        await conn.execute(
            "DELETE FROM ip_assignments WHERE last_seen < now() - ($1::text)::interval",
            f"{s.retention_hour_days} days")
        await conn.execute(
            "DELETE FROM ip_user_log WHERE seen_at < now() - ($1::text)::interval",
            f"{s.retention_hour_days} days")
        await conn.execute("DELETE FROM access_log WHERE ts < now() - interval '180 days'")

    await db.meta_set(pool, "rollup_until", current_hour.isoformat())
    log.info("rollup consolidado hasta %s", current_hour.isoformat())


def assignment_rows(entries: list[tuple], own: set, routers: set,
                    names: dict) -> list[tuple[str, str, int, str]]:
    """(ip, mac, vlan, nombre) a registrar en el historial IP -> equipo.

    Sólo entradas propias (no MAC de router ni vacías). Si una IP aparece en la
    VLAN 0 y en otra VLAN, la VLAN 0 es la copia sin etiqueta del espejo de un
    puerto de acceso (sólo fw->host) y ntopng puede conservarle una MAC vieja
    (p. ej. un iPhone que rotó su MAC privada): se usa la otra (auditoría H31).
    """
    tagged = {ip for ip, _, vlan in entries if (vlan or 0) != 0 and (ip, vlan) in own}
    out = []
    for ip, mac, vlan in entries:
        m = (mac or "").lower()
        if (ip, vlan) not in own or not m or m in routers or m == "00:00:00:00:00:00":
            continue
        if (vlan or 0) == 0 and ip in tagged:
            continue
        name = names.get((ip, mac), "")
        out.append((ip, m, vlan or 0, "" if name == ip else name))
    return list(dict.fromkeys(out))


CYCLE_OFFSET_S = 2.0   # el ciclo arranca en el segundo 2 de cada minuto


def next_cycle_delay(now: datetime, interval: int) -> float:
    """Segundos hasta el próximo inicio de ciclo alineado al reloj.

    Un ciclo por intervalo, siempre en el segundo CYCLE_OFFSET_S: así cada ciclo
    escribe en un minuto distinto (antes, con sleep(60 - duración) el ciclo se
    corría y dos podían caer en el mismo minuto, dejando el siguiente vacío).
    """
    ts = now.timestamp()
    start = (ts // interval) * interval + CYCLE_OFFSET_S
    if start <= ts:
        start += interval
    return start - ts


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = get_settings()
    pool = await db.create_pool()
    nt = NtopngClient()
    cache = HostCache()
    fcache = FlowCache()
    bl = Blocklist(s.blocklist_path)
    ntopng_down_since: datetime | None = None

    log.info("colector iniciado (intervalo %ds, ntopng %s)", s.collect_interval, s.ntopng_url)
    try:
        while True:
            try:
                await collect_cycle(pool, nt, cache, bl, fcache)
                await rollup_and_retention(pool)
                if ntopng_down_since:
                    await raise_alert(pool, "collector", "info",
                                      "ntopng volvió a responder")
                    ntopng_down_since = None
            except Exception as exc:
                log.exception("ciclo de colección falló")
                if ntopng_down_since is None:
                    ntopng_down_since = datetime.now(timezone.utc)
                    await raise_alert(
                        pool, "collector", "warning",
                        f"El colector no pudo consultar ntopng: {exc}",
                    )
            await asyncio.sleep(next_cycle_delay(datetime.now(timezone.utc), s.collect_interval))
    finally:
        await nt.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
