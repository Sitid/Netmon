"""Tests contra una base TEMPORAL creada desde schema.sql (no toca la base de
producción). Requieren un PostgreSQL local y un usuario que pueda crear bases:

    sudo -u postgres /ruta/venv/bin/python -m pytest -q tests/test_db.py

Variable opcional NETMON_TEST_PGHOST (default /var/run/postgresql).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PGHOST = os.environ.get("NETMON_TEST_PGHOST", "/var/run/postgresql")

T0 = datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)   # 17:00 hora argentina
TEST_DB: dict[str, str] = {}   # DSN de la base temporal (para la API en subproceso)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    name = f"netmon_test_{uuid.uuid4().hex[:8]}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def setup():
        admin = await asyncpg.connect(host=PGHOST, database="postgres")
        await admin.execute(f'CREATE DATABASE "{name}"')
        await admin.close()
        conn = await asyncpg.connect(host=PGHOST, database=name)
        await conn.execute((ROOT / "schema.sql").read_text())
        await conn.close()

        async def _init(c):
            import json
            await c.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                   schema="pg_catalog")
        return await asyncpg.create_pool(host=PGHOST, database=name, init=_init)

    try:
        p = loop.run_until_complete(setup())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"sin PostgreSQL local para tests: {exc}")
    TEST_DB["dsn"] = f"postgresql:///{name}?host={PGHOST}"
    yield p

    async def teardown():
        await p.close()
        admin = await asyncpg.connect(host=PGHOST, database="postgres")
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()
    loop.run_until_complete(teardown())
    loop.close()


@pytest.fixture(autouse=True)
def clean(pool):
    run(pool.execute("""TRUNCATE ip_assignments, ip_user_log, ip_user, devices,
                        traffic_min, traffic_hour, hostnames, meta_kv, dns_map"""))


# ---------------------------------------------------------------------------
# H04: historial IP -> equipo
# ---------------------------------------------------------------------------
from netmon import attribution as attr  # noqa: E402

IPHONE, SAVERIS = "0a:5e:67:00:00:05", "e8:96:06:00:00:06"
IP = "10.10.30.113"


def _seed_ip_change(pool):
    """iPhone con la IP de 17:00 a 19:00, después el sensor desde las 21:00."""
    for m in range(0, 121, 1):
        run(attr.record_assignments(pool, [(IP, IPHONE, 30, "iPhone")], T0 + timedelta(minutes=m)))
    for m in range(240, 300, 1):
        run(attr.record_assignments(pool, [(IP, SAVERIS, 30, "Saveris2-SN54893848")],
                                    T0 + timedelta(minutes=m)))


def test_cada_equipo_que_uso_la_ip_queda_como_un_segmento(pool):
    _seed_ip_change(pool)
    segs = run(attr.assignments(pool, IP, T0 - timedelta(hours=1), T0 + timedelta(hours=6)))
    assert [(s["mac"], s["hostname"]) for s in segs] == [
        (IPHONE, "iPhone"), (SAVERIS, "Saveris2-SN54893848")]
    assert segs[0]["first_seen"] == T0 and segs[0]["last_seen"] == T0 + timedelta(minutes=120)


def test_el_trafico_de_cada_segmento_es_del_equipo_que_tenia_la_ip(pool):
    _seed_ip_change(pool)
    rows = [(T0 + timedelta(minutes=m), IP, 0, 50_000_000, 50_000_000) for m in range(0, 120)]
    rows += [(T0 + timedelta(minutes=m), IP, 1_000, 500, 0) for m in range(240, 300)]
    run(pool.executemany("""INSERT INTO traffic_min (ts, ip, bytes_up, bytes_down, bytes_internet)
                            VALUES ($1, $2, $3, $4, $5)""", rows))
    segs = run(attr.assignments(pool, IP, T0 - timedelta(hours=1), T0 + timedelta(hours=6)))
    by = {s["mac"]: s for s in segs}
    assert by[IPHONE]["bytes"] == 120 * 50_000_000
    assert by[SAVERIS]["bytes"] == 60 * 1_500


def test_una_vuelta_del_mismo_equipo_despues_de_un_rato_abre_otro_segmento(pool):
    run(attr.record_assignments(pool, [(IP, IPHONE, 30, "iPhone")], T0))
    run(attr.record_assignments(pool, [(IP, IPHONE, 30, "iPhone")], T0 + timedelta(hours=2)))
    segs = run(attr.assignments(pool, IP, T0 - timedelta(hours=1), T0 + timedelta(hours=3)))
    assert len(segs) == 2


# ---------------------------------------------------------------------------
# H07: el usuario de un login se asigna sólo al equipo que tenía la IP en ese momento
# ---------------------------------------------------------------------------
def test_login_no_se_hereda_a_otra_mac_que_tuvo_la_ip(pool):
    run(pool.execute("""INSERT INTO devices (mac, ip, hostname) VALUES
                        ($1, $3, 'iPhone'), ($2, $3, 'PC-01')""", IPHONE, "50:91:e3:00:00:01", IP))
    run(attr.record_assignments(pool, [(IP, "50:91:e3:00:00:01", 30, "PC-01")], T0))
    run(attr.record_logon(pool, IP, "usuario1", T0 + timedelta(seconds=30)))
    users = dict(run(pool.fetch("SELECT mac::text, ad_user FROM devices")))
    assert users["50:91:e3:00:00:01"] == "usuario1"
    assert users[IPHONE] is None


def test_login_sin_equipo_conocido_no_asigna_usuario_a_ningun_dispositivo(pool):
    run(pool.execute("INSERT INTO devices (mac, ip, hostname) VALUES ($1, $2, 'x')", IPHONE, IP))
    run(attr.record_logon(pool, IP, "usuario1", T0))
    assert run(pool.fetchval("SELECT ad_user FROM devices")) is None
    # pero el login queda registrado en el historial
    assert run(pool.fetchval("SELECT count(*) FROM ip_user_log")) == 1


# ---------------------------------------------------------------------------
# H04: consumo por usuario según quién tenía la IP en cada momento
# ---------------------------------------------------------------------------
def test_consumo_por_usuario_respeta_el_cambio_de_usuario_y_el_ttl(pool):
    run(attr.record_logon(pool, IP, "alice", T0))                       # 17:00
    run(attr.record_logon(pool, IP, "bob", T0 + timedelta(hours=3)))    # 20:00
    rows = [(T0 + timedelta(minutes=m), IP, 0, 1_000, 1_000) for m in range(0, 14 * 60)]
    run(pool.executemany("""INSERT INTO traffic_min (ts, ip, bytes_up, bytes_down, bytes_internet)
                            VALUES ($1, $2, $3, $4, $5)""", rows))
    got = {r["username"]: r for r in run(attr.usage_by_user(
        pool, T0 - timedelta(hours=1), T0 + timedelta(hours=15), None, "10 hours"))}
    assert got["alice"]["total"] == 3 * 60 * 1_000          # 17:00-20:00
    assert got["bob"]["total"] == 10 * 60 * 1_000           # 20:00-06:00 (TTL 10 h)
    # después del TTL (06:00-07:00) el tráfico no se atribuye a nadie
    assert sum(r["total"] for r in got.values()) == 13 * 60 * 1_000


def test_la_sesion_termina_si_la_ip_pasa_a_otro_equipo(pool):
    run(attr.record_assignments(pool, [(IP, "50:91:e3:00:00:01", 30, "PC-01")], T0))
    for m in range(1, 61):
        run(attr.record_assignments(pool, [(IP, "50:91:e3:00:00:01", 30, "PC-01")],
                                    T0 + timedelta(minutes=m)))
    run(attr.record_logon(pool, IP, "usuario1", T0))
    for m in range(90, 200):   # el celular toma la IP a los 90 min
        run(attr.record_assignments(pool, [(IP, IPHONE, 30, "iPhone")], T0 + timedelta(minutes=m)))
    rows = [(T0 + timedelta(minutes=m), IP, 0, 1_000, 1_000) for m in range(0, 200)]
    run(pool.executemany("""INSERT INTO traffic_min (ts, ip, bytes_up, bytes_down, bytes_internet)
                            VALUES ($1, $2, $3, $4, $5)""", rows))
    got = {r["username"]: r for r in run(attr.usage_by_user(
        pool, T0 - timedelta(hours=1), T0 + timedelta(hours=5), None, "10 hours"))}
    # 60 min del segmento de la PC + 2 min de margen; el celular no suma a 'usuario1'
    assert got["usuario1"]["total"] <= 63 * 1_000


# ---------------------------------------------------------------------------
# H04: los reportes atribuyen el período con quién tenía la IP en ese período
# ---------------------------------------------------------------------------
from datetime import date  # noqa: E402

from netmon import reports  # noqa: E402


def _traffic(pool, minutes):
    rows = [(T0 + timedelta(minutes=m), IP, 100, 1_000, 1_000) for m in minutes]
    run(pool.executemany("""INSERT INTO traffic_min (ts, ip, bytes_up, bytes_down, bytes_internet)
                            VALUES ($1, $2, $3, $4, $5)""", rows))


def test_reporte_del_dia_nombra_a_todos_los_equipos_que_tuvieron_la_ip(pool):
    _seed_ip_change(pool)
    _traffic(pool, range(0, 300))
    data = run(reports.gather_report_data(pool, "day", date(2026, 9, 28)))
    row = next(r for r in data["top"] if r["ip"] == IP)
    assert "iPhone" in row["hostname"] and "Saveris2-SN54893848" in row["hostname"]
    assert row["equipos"] == 2


def test_reporte_de_otro_dia_no_usa_el_usuario_de_hoy(pool):
    run(attr.record_logon(pool, IP, "alice", T0))            # 28/09 17:00 AR
    _traffic(pool, range(0, 60))
    _traffic(pool, range(3 * 1440, 3 * 1440 + 60))            # 01/10, sin logins
    d28 = run(reports.gather_report_data(pool, "day", date(2026, 9, 28)))
    d01 = run(reports.gather_report_data(pool, "day", date(2026, 10, 1)))
    assert next(r for r in d28["top"] if r["ip"] == IP)["ad_user"] == "alice"
    assert next(r for r in d01["top"] if r["ip"] == IP)["ad_user"] == ""


def test_reporte_sin_historial_marca_el_nombre_como_actual(pool):
    run(pool.execute("INSERT INTO hostnames (ip, hostname, source) VALUES ($1, 'Saveris2', 'ntopng')", IP))
    _traffic(pool, range(0, 60))
    data = run(reports.gather_report_data(pool, "day", date(2026, 9, 28)))
    assert next(r for r in data["top"] if r["ip"] == IP)["hostname"] == "Saveris2 (actual)"


# ---------------------------------------------------------------------------
# API contra la base temporal (subproceso: settings aislados)
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402


def _api(pool, code: str, **env) -> str:
    full = {**os.environ, "NETMON_DB_DSN": TEST_DB["dsn"],
            "NETMON_NTOPNG_URL": "http://127.0.0.1:9", "NETMON_KIOSK_TOKEN": "tok-kiosco-123",
            "NETMON_ADMIN_PASSWORD": "clave-admin-123", "NETMON_SECRET_KEY": "s" * 32, **env}
    pre = ("import sys, time; sys.path.insert(0, %r)\n"
           "from fastapi.testclient import TestClient\nfrom netmon.api import app, state\n"
           "def ready(c):\n"
           "    for _ in range(50):\n"
           "        if state.get('pool') is not None: return\n"
           "        time.sleep(0.1)\n") % str(ROOT)
    r = subprocess.run([sys.executable, "-c", pre + code], capture_output=True, text=True,
                       env=full, timeout=120)
    assert r.returncode == 0, r.stderr[-1200:]
    return r.stdout.strip().splitlines()[-1]


# H19: con ntopng caído los endpoints en vivo dicen "sin datos" (503), no 500
def test_api_hosts_sin_ntopng_responde_503(pool):
    out = _api(pool, """
with TestClient(app, raise_server_exceptions=False) as c:
    ready(c)
    # sesión de admin: desde H08 el token de kiosco no abre /api/hosts
    c.post('/api/login', json={'username': 'admin', 'password': 'clave-admin-123'})
    print(c.get('/api/hosts').status_code)
""")
    assert out == "503", out


# H08: el token del kiosco sólo abre lo que muestra el kiosco (Resumen/Estado)
def test_token_kiosco_no_accede_a_datos_por_persona(pool):
    out = _api(pool, """
with TestClient(app, raise_server_exceptions=False) as c:
    ready(c)
    t = '?token=tok-kiosco-123'
    codes = [c.get(p + t).status_code for p in (
        '/api/overview', '/api/summary', '/api/ping/summary',
        '/api/user/usuario1', '/api/search',
        '/api/hosts/10.0.0.1/usage', '/api/ip/8.8.8.8', '/api/site-hosts')]
    print(codes)
""")
    assert out == "[200, 200, 200, 403, 403, 403, 403, 403]", out


def test_kiosco_cambia_el_token_de_la_url_por_una_cookie(pool):
    out = _api(pool, """
with TestClient(app, raise_server_exceptions=False) as c:
    ready(c)
    r = c.get('/kiosk?token=tok-kiosco-123', follow_redirects=False)
    ok_cookie = 'nm_kiosk' in r.headers.get('set-cookie', '')
    r2 = c.get('/api/summary')          # sin token: alcanza la cookie
    print(r.status_code, r.headers.get('location'), ok_cookie, r2.status_code)
""")
    assert out == "303 /kiosk True 200", out


# H09: login con límite de intentos
def test_login_bloquea_despues_de_5_intentos_fallidos(pool):
    out = _api(pool, """
with TestClient(app, raise_server_exceptions=False) as c:
    ready(c)
    codes = [c.post('/api/login', json={'username': 'admin', 'password': 'mala'}).status_code
             for _ in range(5)]
    codes.append(c.post('/api/login', json={'username': 'admin', 'password': 'clave-admin-123'}).status_code)
    print(codes)
""")
    assert out == "[401, 401, 401, 401, 401, 429]", out


# H10: registro de quién consultó datos de qué persona / equipo
def test_consulta_de_datos_personales_queda_registrada(pool):
    out = _api(pool, """
with TestClient(app, raise_server_exceptions=False) as c:
    ready(c)
    c.post('/api/login', json={'username': 'admin', 'password': 'clave-admin-123'})
    c.get('/api/user/usuario1?range=24h')
    log = c.get('/api/access-log').json()
    print(any(e['user'] == 'admin' and e['path'] == '/api/user/usuario1' for e in log))
""")
    assert out == "True", out


# ---------------------------------------------------------------------------
# Mapa DNS pasivo: nombre de sitio para conexiones sin SNI (QUIC/ECH)
# ---------------------------------------------------------------------------
def test_dns_etiqueta_prefiere_la_respuesta_que_recibio_el_mismo_cliente(pool):
    from netmon import dnsmap
    run(dnsmap.record(pool, [("10.10.30.5", "52.1.2.3", "www.netflix.com", T0),
                             ("192.168.100.20", "52.1.2.3", "otra.cdn.com", T0),
                             ("192.168.100.20", "8.8.4.4", "dns.google", T0)]))
    got = run(dnsmap.lookup(pool, [("10.10.30.5", "52.1.2.3"), ("10.10.30.9", "8.8.4.4"),
                                   ("10.10.30.9", "1.2.3.4")], T0 + timedelta(minutes=5)))
    assert got == {("10.10.30.5", "52.1.2.3"): "www.netflix.com",   # su propia consulta
                   ("10.10.30.9", "8.8.4.4"): "dns.google"}         # de otro cliente (el DC)


def test_dns_viejo_no_se_usa(pool):
    from netmon import dnsmap
    run(dnsmap.record(pool, [("10.10.30.5", "52.1.2.3", "www.netflix.com", T0)]))
    assert run(dnsmap.lookup(pool, [("10.10.30.5", "52.1.2.3")], T0 + timedelta(hours=13))) == {}


def test_colector_nombra_por_dns_solo_conexiones_de_internet_sin_sni(pool):
    from netmon import dnsmap
    from netmon.collector import label_by_dns
    run(dnsmap.record(pool, [("10.10.30.5", "52.1.2.3", "www.netflix.com", T0),
                             ("10.10.30.5", "10.10.10.20", "dc01.empresa.local", T0)]))
    quic = ("10.10.30.5", "52.1.2.3", 443, "QUIC", "internet", "saliente")
    tls = ("10.10.30.5", "52.9.9.9", 443, "TLS.YouTube", "internet", "saliente")
    smb = ("10.10.30.5", "10.10.10.20", 445, "SMB", "interno", "saliente")
    rows = {quic: [1000, "", ""], tls: [500, "youtube.com", "sni"], smb: [700, "", ""]}
    dom_rows = {"youtube.com": 500}
    run(label_by_dns(pool, rows, dom_rows, T0 + timedelta(minutes=1)))
    assert rows[quic] == [1000, "netflix.com", "dns"]      # dominio registrable, origen dns
    assert rows[tls] == [500, "youtube.com", "sni"]        # el SNI manda
    assert rows[smb] == [700, "", ""]                      # interno: no se nombra
    assert dom_rows == {"youtube.com": 500, "netflix.com": 1000}


# ---------------------------------------------------------------------------
# Reportes: serie de consumo por día (vista de Reportes en pantalla)
# ---------------------------------------------------------------------------
from netmon import db as _db  # noqa: E402
from netmon import reports as _rep  # noqa: E402


def test_overview_consumo_por_dia(pool):
    # rollup al futuro => la serie sale toda de traffic_hour; internet ya distinguido
    run(_db.meta_set(pool, "rollup_until", (T0 + timedelta(days=5)).isoformat()))
    run(_db.meta_set(pool, "internet_since", (T0 - timedelta(days=1)).isoformat()))
    rows = [
        (T0, IP, 100, 900, 500),                            # 28/09 17:00 ART
        (T0 + timedelta(hours=1), IP, 100, 900, 0),         # 28/09 18:00 ART
        (T0 + timedelta(days=1), "10.0.0.9", 10, 90, 0),    # 29/09 17:00 ART
    ]
    run(pool.executemany(
        "INSERT INTO traffic_hour (ts, ip, bytes_up, bytes_down, bytes_internet) "
        "VALUES ($1,$2,$3,$4,$5)", rows))
    start, end, _ = _rep.period_bounds_custom(date(2026, 9, 28), date(2026, 9, 30))
    data = run(_rep.gather_overview(pool, start, end, limit=20))

    por_dia = {d["day"]: d for d in data["daily"]}
    assert set(por_dia) == {"2026-09-28", "2026-09-29", "2026-09-30"}  # días rellenados
    assert por_dia["2026-09-28"]["total"] == 2000
    assert por_dia["2026-09-28"]["bytes_internet"] == 500
    assert por_dia["2026-09-28"]["bytes_internal"] == 1500
    assert por_dia["2026-09-29"]["total"] == 100
    assert por_dia["2026-09-30"]["total"] == 0          # día sin tráfico => 0, no hueco
    assert data["totals"]["total"] == 2100
    assert data["active_hosts"] == 2                     # dos IPs con tráfico
    assert data["top"][0]["ip"] == IP                   # la IP que más consumió


# ---------------------------------------------------------------------------
# Reporte por equipo (elegir el equipo cuando la IP fue de varios) + purga
# ---------------------------------------------------------------------------
from netmon import purge as _purge  # noqa: E402

START = T0 - timedelta(hours=1)
END = T0 + timedelta(hours=6)


def _seed_two_devices_traffic(pool):
    """iPhone (17-19h) y Saveris (21-22h) comparten IP; tráfico en cada franja."""
    run(_db.meta_set(pool, "rollup_until", (T0 - timedelta(days=1)).isoformat()))
    _seed_ip_change(pool)
    rows = [(T0 + timedelta(minutes=10), IP, 10 * 1048576, 90 * 1048576, 0),   # iPhone
            (T0 + timedelta(minutes=60), IP, 10 * 1048576, 90 * 1048576, 0),   # iPhone
            (T0 + timedelta(minutes=250), IP, 5 * 1048576, 45 * 1048576, 0)]   # Saveris
    run(pool.executemany(
        "INSERT INTO traffic_min (ts, ip, bytes_up, bytes_down, bytes_internet) "
        "VALUES ($1,$2,$3,$4,$5)", rows))


def test_reporte_por_equipo_filtra_por_mac(pool):
    _seed_two_devices_traffic(pool)
    todos = run(_rep.gather_host_report(pool, IP, START, END, "t"))
    iph = run(_rep.gather_host_report(pool, IP, START, END, "t", IPHONE))
    sav = run(_rep.gather_host_report(pool, IP, START, END, "t", SAVERIS))
    assert todos["total"] == (100 + 100 + 50) * 1048576      # todos los equipos
    assert iph["total"] == (100 + 100) * 1048576             # solo iPhone
    assert sav["total"] == 50 * 1048576                      # solo Saveris


def test_purga_borra_solo_el_equipo_elegido_y_respalda(pool, tmp_path):
    from netmon.config import get_settings
    get_settings().reports_dir = str(tmp_path)
    run(pool.execute("TRUNCATE purge_log"))
    _seed_two_devices_traffic(pool)

    pre = run(_purge.preview(pool, IP, START, END, None, T0 - timedelta(days=1)))
    assert pre["por_tabla"].get("traffic_min") == 3          # las 3 filas de la IP
    assert pre["resumen"]["total_bytes"] == (100 + 100 + 50) * 1048576  # resumen friendly

    res = run(_purge.execute(pool, IP, START, END, SAVERIS, "tester", "prueba"))
    assert res["borradas"].get("traffic_min") == 1           # solo la de Saveris
    assert run(pool.fetchval("SELECT count(*) FROM traffic_min WHERE ip=$1", IP)) == 2
    assert run(pool.fetchval("SELECT count(*) FROM purge_log")) == 1   # auditado
    import os
    assert os.path.exists(res["backup"])                     # backup en disco


def test_purga_por_sitio_borra_solo_el_sitio_elegido(pool, tmp_path):
    from netmon.config import get_settings
    get_settings().reports_dir = str(tmp_path)
    run(pool.execute("TRUNCATE flows_min, flows_hour, purge_log"))
    rows = [(T0, IP, "1.1.1.1", 443, "TLS", "internet", 100, "a.com", "sni"),
            (T0 + timedelta(minutes=1), IP, "1.1.1.2", 443, "TLS", "internet", 50, "a.com", "sni"),
            (T0, IP, "2.2.2.2", 443, "QUIC", "internet", 30, "b.com", "sni")]
    run(pool.executemany(
        "INSERT INTO flows_min (ts,local_ip,remote_ip,srv_port,l7,scope,bytes,domain,domain_src) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)", rows))
    cut = T0 - timedelta(days=1)   # corte en el pasado: todo sale de flows_min
    lst = run(_purge.sites(pool, IP, START, END, None, cut))
    names = {g["site"] for g in lst}
    assert "a.com" in names and "b.com" in names

    res = run(_purge.delete_sites(pool, IP, START, END, None, ["a.com"], "tester", "x"))
    assert res["borradas"].get("flows_min") == 2          # las 2 filas de a.com
    assert run(pool.fetchval("SELECT count(*) FROM flows_min WHERE local_ip=$1", IP)) == 1  # queda b.com
    assert run(pool.fetchval("SELECT count(*) FROM purge_log")) == 1
