"""Invariantes de integridad de datos de netmon (auditoría 2026-09-29).

Todos los tests son de SOLO LECTURA: consultan la base de producción, la API y
los contadores del kernel de la placa SPAN. No escriben nada.

Cómo correrlos (en netmon-srv, como el usuario netmon para leer netmon.env):
    sudo -u netmon bash -c 'set -a; . /etc/netmon/netmon.env; set +a;
        cd /opt/netmon && NETMON_TEST_API=https://127.0.0.1:8443 \
        /ruta/al/venv/bin/python -m pytest -q tests/test_invariantes.py'

Variables:
    NETMON_TEST_API   base de la API (default https://127.0.0.1:8443, sin verificar TLS)
    NETMON_TEST_IFACE interfaz SPAN (default enp1s0)

Tolerancias (justificación en cada test):
    LINK_BPS  = 1 Gbit/s: el puerto destino del mirror (1/1/36) y los accesos de
                los equipos son de 1 Gbit/s; ningún host ni la suma de la
                interfaz puede superar eso por dirección.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
import pytest

API = os.environ.get("NETMON_TEST_API", "https://127.0.0.1:8443")
IFACE = os.environ.get("NETMON_TEST_IFACE", "enp1s0")
DSN = os.environ.get("NETMON_DB_DSN", "")
TOKEN = os.environ.get("NETMON_KIOSK_TOKEN", "")

LINK_BPS = 1_000_000_000                      # 1 Gbit/s
MAX_BYTES_PER_MIN = LINK_BPS * 60 // 8        # 7.5 GB por minuto y dirección

pytestmark = pytest.mark.skipif(not DSN, reason="falta NETMON_DB_DSN (correr con netmon.env)")


def q(sql: str, *args):
    async def run():
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()
    return asyncio.run(run())


def api_get(path: str):
    sep = "&" if "?" in path else "?"
    r = httpx.get(f"{API}{path}{sep}token={TOKEN}", verify=False, timeout=60)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# 1. Tasas y volúmenes no negativos
# ---------------------------------------------------------------------------
TABLAS_BYTES = {
    "traffic_min": "bytes_up, bytes_down, bytes_internet",
    "traffic_5min": "bytes_up, bytes_down, bytes_internet",
    "traffic_hour": "bytes_up, bytes_down, bytes_internet",
    "traffic_cat_min": "bytes_up, bytes_down",
    "traffic_app_host_min": "bytes_up, bytes_down",
    "app_min": "bytes_up, bytes_down",
    "flows_min": "bytes",
    "domain_min": "bytes",
}


@pytest.mark.parametrize("tabla", sorted(TABLAS_BYTES))
def test_volumenes_no_negativos(tabla):
    cols = TABLAS_BYTES[tabla].split(", ")
    cond = " OR ".join(f"{c} < 0" for c in cols)
    n = q(f"SELECT count(*) AS n FROM {tabla} WHERE {cond}")[0]["n"]
    assert n == 0, f"{tabla}: {n} filas con bytes negativos"


# ---------------------------------------------------------------------------
# 2. Ningún host supera la capacidad del enlace (1 Gbit/s por dirección)
# ---------------------------------------------------------------------------
def test_host_no_supera_enlace_por_minuto():
    """traffic_min: bytes de UN host en UN minuto <= 1 Gbit/s * 60 s.
    Con 5 % de margen porque un ciclo del colector puede durar un poco más de 60 s
    y su delta caer entero en un minuto."""
    lim = int(MAX_BYTES_PER_MIN * 1.05)
    rows = q("""SELECT ts, host(ip) ip, bytes_up, bytes_down FROM traffic_min
                WHERE bytes_up > $1 OR bytes_down > $1 ORDER BY ts DESC LIMIT 5""", lim)
    assert not rows, f"hosts por encima del enlace: {[dict(r) for r in rows]}"


def test_flujo_no_supera_enlace_por_minuto():
    lim = int(MAX_BYTES_PER_MIN * 1.05)
    rows = q("""SELECT ts, host(local_ip) l, host(remote_ip) r, srv_port, bytes FROM flows_min
                WHERE bytes > $1 ORDER BY ts DESC LIMIT 5""", lim)
    assert not rows, f"flujos imposibles: {[dict(r) for r in rows]}"


# ---------------------------------------------------------------------------
# 3. Suma por host ≈ total de la interfaz
# ---------------------------------------------------------------------------
def test_suma_bajada_de_hosts_no_supera_la_interfaz():
    """Cada byte del cable lo RECIBE a lo sumo un equipo local: la suma de bajadas
    de todos los hosts en un minuto no puede superar lo que entra por la placa
    SPAN (tope físico 1 Gbit/s). Margen 10 % por el desfasaje de los ciclos."""
    lim = int(MAX_BYTES_PER_MIN * 1.10)
    rows = q("""SELECT ts, SUM(bytes_down) b FROM traffic_min
                WHERE ts > now() - interval '24 hours'
                GROUP BY ts HAVING SUM(bytes_down) > $1 ORDER BY 2 DESC LIMIT 5""", lim)
    assert not rows, f"minutos con suma de bajadas > interfaz: {[dict(r) for r in rows]}"


def _kernel_rx_bps(seconds: float) -> float:
    p = Path(f"/sys/class/net/{IFACE}/statistics/rx_bytes")
    b0, t0 = int(p.read_text()), time.time()
    time.sleep(seconds)
    return (int(p.read_text()) - b0) * 8 / (time.time() - t0)


def _ws_messages(seconds: float, kind: str) -> list[dict]:
    import websockets

    async def run():
        url = API.replace("https", "wss").replace("http", "ws") + f"/ws?token={TOKEN}"
        ctx = ssl._create_unverified_context() if url.startswith("wss") else None
        out, t0 = [], time.time()
        async with websockets.connect(url, ssl=ctx) as ws:
            while time.time() - t0 < seconds:
                m = json.loads(await asyncio.wait_for(ws.recv(), seconds))
                if m.get("type") == kind:
                    out.append(m)
        return out
    return asyncio.run(run())


def test_grafico_realtime_coincide_con_el_kernel():
    """La UI dibuja rt.bps * 8 / 1e6 como Mbps. Debe coincidir con rx de la placa
    SPAN medido por el kernel (±15 %: ventanas de muestreo distintas)."""
    msgs = _ws_messages(20, "rt")
    kern = _kernel_rx_bps(5)
    ui = sum(m["sample"]["bps"] for m in msgs if m.get("sample")) / max(1, len(msgs)) * 8
    assert msgs, "no llegaron muestras rt"
    assert abs(ui - kern) / kern < 0.15, f"UI {ui/1e6:.0f} Mbps vs kernel {kern/1e6:.0f} Mbit/s"


def test_kpi_ancho_de_banda_coincide_con_el_kernel():
    """El KPI 'Ancho de banda ahora' muestra totals.down_bps (bits/s) en Mbps.
    La bajada sumada de los hosts no puede superar lo que entra por la placa SPAN
    (+25 % por suavizado y desfasaje de ventanas). Que la UI no vuelva a
    multiplicar por 8 lo cubre tests/test_frontend.py."""
    msgs = _ws_messages(35, "live")
    kern = _kernel_rx_bps(5)
    assert len(msgs) >= 2, "no llegaron mensajes live"
    shown = [m["totals"]["down_bps"] for m in msgs[1:]]   # bits/s
    worst = max(shown)
    assert worst <= kern * 1.25, (
        f"KPI muestra hasta {worst/1e6:.0f} Mbps con la interfaz a {kern/1e6:.0f} Mbit/s")


# ---------------------------------------------------------------------------
# 4. Porcentajes y partes
# ---------------------------------------------------------------------------
def test_perdida_ping_entre_0_y_100():
    n = q("SELECT count(*) n FROM ping_min WHERE loss_pct < 0 OR loss_pct > 100")[0]["n"]
    assert n == 0


def test_internet_no_supera_el_total_del_host():
    n = q("""SELECT count(*) n FROM traffic_min
             WHERE bytes_internet > bytes_up + bytes_down""")[0]["n"]
    assert n == 0, f"{n} filas con internet > total"


def test_sitios_no_superan_internet_por_minuto():
    rows = q("""WITH d AS (SELECT ts, SUM(bytes) b FROM domain_min GROUP BY ts),
                     i AS (SELECT ts, SUM(bytes_internet) b FROM traffic_min GROUP BY ts)
                SELECT d.ts, d.b, i.b ib FROM d JOIN i USING (ts)
                WHERE d.b > i.b * 1.10 ORDER BY d.ts DESC LIMIT 5""")
    assert not rows, f"minutos con sitios > internet: {[dict(r) for r in rows]}"


def test_parte_del_mayor_consumidor_de_un_sitio_hasta_100():
    # datos por equipo: desde H08 requieren sesión (el token de kiosco da 403)
    d = httpx.get(f"{API}/api/site-hosts?name=YouTube&range=24h", verify=False, timeout=60,
                  cookies=_admin_cookies()).json()
    if d["hosts"]:
        assert 0 <= d["hosts"][0]["bytes"] <= d["bytes_total"]


# ---------------------------------------------------------------------------
# 5. Timestamps: no futuros, rollups consistentes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tabla", ["traffic_min", "flows_min", "domain_min", "ping_min",
                                   "traffic_hour", "alerts"])
def test_timestamps_no_futuros(tabla):
    r = q(f"SELECT max(ts) m FROM {tabla}")[0]["m"]
    assert r is None or r <= datetime.now(timezone.utc) + timedelta(minutes=1), \
        f"{tabla}: ts futuro {r}"


def test_rollup_horario_suma_igual_que_minutos():
    """Para horas cerradas que siguen en el tier de minutos, SUM(hora) == SUM(minutos):
    el rollup suma volúmenes, no los promedia."""
    rows = q("""WITH h AS (SELECT ts, SUM(bytes_up+bytes_down) b FROM traffic_hour
                           WHERE ts >= (SELECT min(date_trunc('hour', ts)) + interval '1 hour' FROM traffic_min)
                             AND ts < (SELECT (v #>> '{}')::timestamptz FROM meta_kv WHERE k='rollup_until')
                           GROUP BY ts),
                     m AS (SELECT date_trunc('hour', ts) ts, SUM(bytes_up+bytes_down) b FROM traffic_min GROUP BY 1)
                SELECT h.ts, h.b hb, m.b mb FROM h JOIN m USING (ts) WHERE h.b <> m.b LIMIT 5""")
    assert not rows, f"horas con rollup distinto de sus minutos: {[dict(r) for r in rows]}"


# ---------------------------------------------------------------------------
# 6. Fuente caída -> "sin datos", no un número
# ---------------------------------------------------------------------------
def test_estado_de_enlaces_no_esta_viejo():
    """target_state alimenta 'Estado de red: En línea'. Si el pinger murió, el
    estado queda congelado; la API no expone su antigüedad al frontend."""
    old = q("""SELECT target, updated_at FROM target_state
               WHERE updated_at < now() - interval '2 minutes'""")
    assert not old, f"targets con estado viejo: {[dict(r) for r in old]}"


def test_mensaje_live_expone_antiguedad_del_estado_de_enlaces():
    """Para poder mostrar 'sin datos' la UI necesita saber cuándo se midió cada
    target. Hoy live_loop no manda updated_at."""
    msgs = _ws_messages(30, "live")    # un 'live' cada 10-18 s (listado de ntopng + espera)
    assert msgs and all("updated_at" in t for t in msgs[0]["targets"]), \
        "los targets del mensaje live no traen updated_at"


def test_api_sin_ntopng_responde_503_no_500():
    """Con ntopng caído los endpoints en vivo deben decir 'sin datos' (503), no
    explotar con 500. Se levanta la app en un subproceso con ntopng inalcanzable."""
    code = r"""
import os, sys
os.environ["NETMON_NTOPNG_URL"] = "http://127.0.0.1:9"
from fastapi.testclient import TestClient
from netmon.api import app
with TestClient(app, raise_server_exceptions=False) as c:
    r = c.get("/api/hosts", params={"token": os.environ["NETMON_KIOSK_TOKEN"]})
    print(r.status_code)
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.environ.get("NETMON_TEST_SRC", "/opt/netmon"), timeout=120)
    status = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[-300:]
    assert status == "503", f"/api/hosts sin ntopng devolvió {status}"


# ---------------------------------------------------------------------------
# 7. Atribución
# ---------------------------------------------------------------------------
def test_usuario_de_dispositivo_esta_respaldado_por_un_login():
    """Desde H07 el usuario de un dispositivo es el del último login DESDE ese
    dispositivo. Tiene que existir un login (ip_user / ip_user_log) de ese usuario
    en alguna IP que esa MAC usó; si no, lo heredó por IP (bug viejo)."""
    rows = q("""SELECT d.mac::text, host(d.ip) ip, d.hostname, d.ad_user FROM devices d
                WHERE d.ad_user IS NOT NULL AND d.ad_user <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM (SELECT ip, username FROM ip_user
                                     UNION SELECT ip, username FROM ip_user_log) l
                      WHERE lower(l.username) = lower(d.ad_user)
                        AND (l.ip = d.ip OR l.ip IN (SELECT a.ip FROM ip_assignments a
                                                     WHERE a.mac = d.mac)))
                LIMIT 10""")
    assert not rows, f"dispositivos con usuario sin login que lo respalde: {[dict(r) for r in rows]}"


def test_celular_con_mac_privada_no_tiene_usuario_de_dominio():
    """Los celulares no hacen Kerberos: una MAC privada (bit 'local') con usuario
    de dominio sólo puede ser herencia de otra máquina que usó la IP."""
    rows = q("""SELECT mac::text, host(ip) ip, hostname, ad_user FROM devices
                WHERE ad_user IS NOT NULL AND ad_user <> ''
                  AND (get_byte(macaddr8_send(mac::macaddr8), 0) & 2) = 2 LIMIT 10""")
    assert not rows, f"MAC privadas con usuario de dominio: {[dict(r) for r in rows]}"


def test_una_ip_no_la_usan_dos_equipos_al_mismo_tiempo():
    """Que DHCP reasigne una IP a otro equipo es normal (y desde H04 se atribuye
    por período). Lo que no debería pasar es que dos MAC tengan la misma IP A LA
    VEZ: eso es un conflicto de IP en la red (o un error de atribución)."""
    rows = q("""SELECT host(a.ip) ip, a.mac::text m1, b.mac::text m2,
                       GREATEST(a.first_seen, b.first_seen) desde,
                       LEAST(a.last_seen, b.last_seen) hasta
                FROM ip_assignments a JOIN ip_assignments b
                  ON a.ip = b.ip AND a.mac < b.mac
                -- superposición real de más de 5 minutos, en el estado actual
                -- (los tramos históricos previos a H31 quedan como se observaron)
                WHERE LEAST(a.last_seen, b.last_seen) - GREATEST(a.first_seen, b.first_seen)
                      > interval '5 minutes'
                  AND a.last_seen > now() - interval '10 minutes'
                  AND b.last_seen > now() - interval '10 minutes'
                LIMIT 10""")
    assert not rows, f"IPs usadas por dos equipos a la vez: {[dict(r) for r in rows]}"


# ---------------------------------------------------------------------------
# H18: el "día" de cuotas, alertas y reportes empieza a medianoche argentina
# ---------------------------------------------------------------------------
def test_inicio_del_dia_es_medianoche_argentina():
    sys.path.insert(0, os.environ.get("NETMON_TEST_SRC", "/opt/netmon"))
    from netmon import db as nmdb
    r = q(f"SELECT {nmdb.day_start_sql()} AT TIME ZONE 'America/Argentina/Buenos_Aires' AS local")[0]["local"]
    assert (r.hour, r.minute) == (0, 0), f"el día arranca a las {r:%H:%M} hora argentina"


# ---------------------------------------------------------------------------
# H24: la ficha de un equipo no recorre todo el listado de ntopng
# ---------------------------------------------------------------------------
def test_detalle_de_host_responde_en_menos_de_3_s():
    """Antes: 5-14 s (listado completo de ntopng para averiguar la VLAN)."""
    ip = q("""SELECT host(ip) ip FROM ip_assignments WHERE last_seen > now() - interval '3 minutes'
              ORDER BY last_seen DESC LIMIT 1""")[0]["ip"]
    t = time.time()
    r = httpx.get(f"{API}/api/hosts/{ip}", verify=False, timeout=60,
                  cookies=_admin_cookies())
    assert r.status_code == 200
    assert time.time() - t < 3, f"{time.time() - t:.1f} s"


def _admin_cookies():
    r = httpx.post(f"{API}/api/login", verify=False, timeout=30,
                   json={"username": "admin", "password": os.environ["NETMON_ADMIN_PASSWORD"]})
    r.raise_for_status()
    return r.cookies


# ---------------------------------------------------------------------------
# H22: Sitios informa qué parte del tráfico de internet cubre (es una muestra)
# ---------------------------------------------------------------------------
def test_sitios_informa_cobertura():
    d = httpx.get(f"{API}/api/sites-top?range=24h", verify=False, timeout=60,
                  cookies=_admin_cookies()).json()
    assert "coverage_pct" in d, "la API no informa la cobertura de los sitios"
    assert 0 <= d["coverage_pct"] <= 100


# ---------------------------------------------------------------------------
# H33: "internet" en el registro de conexiones = destino público, y el registro
# no puede sumar más internet que el contador de los propios equipos
# ---------------------------------------------------------------------------
def test_conexiones_de_internet_tienen_destino_publico():
    rows = q("""SELECT host(remote_ip) r, count(*) n FROM flows_min
                WHERE scope = 'internet' AND ts > now() - interval '10 minutes'
                  AND remote_ip << ANY('{10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,224.0.0.0/4}'::inet[])
                GROUP BY 1 LIMIT 5""")
    assert not rows, f"destinos privados clasificados como internet: {[dict(r) for r in rows]}"


def test_registro_de_internet_no_supera_el_contador_de_los_equipos():
    """Últimos 20 min completos. +10 % por el desfasaje entre ciclos."""
    r = q("""WITH w AS (SELECT date_trunc('minute', now()) - interval '21 minutes' d,
                               date_trunc('minute', now()) - interval '1 minute' h)
             SELECT (SELECT SUM(bytes) FROM flows_min, w WHERE scope = 'internet'
                       AND ts >= w.d AND ts < w.h) f,
                    (SELECT SUM(bytes_internet) FROM traffic_min, w WHERE ts >= w.d AND ts < w.h) t""")[0]
    assert float(r["f"]) <= float(r["t"]) * 1.10, f"registro {r['f']} > contador {r['t']}"
