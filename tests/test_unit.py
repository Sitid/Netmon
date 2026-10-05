"""Tests unitarios (sin base ni ntopng): corren en cualquier venv con las dependencias.

    python -m pytest -q tests/test_unit.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NETMON_DB_DSN", "postgresql://x:y@127.0.0.1:1/none")

from netmon.ntopng_client import NtopngClient  # noqa: E402


# ---------------------------------------------------------------------------
# H01: el listado paginado de ntopng repite hosts entre páginas
# ---------------------------------------------------------------------------
class _FakePages(NtopngClient):
    """Cliente con _get falso: devuelve páginas predefinidas."""

    def __init__(self, pages):
        super().__init__()
        self.pages = pages

    async def _get(self, path, params=None):
        page = params["currentPage"]
        return {"data": self.pages[page - 1] if page <= len(self.pages) else []}


def _host(ip, vlan=10, sent=0, rcvd=0):
    return {"ip": ip, "vlan": vlan, "mac": "aa:bb:cc:00:00:01",
            "bytes.sent": sent, "bytes.recvd": rcvd}


def test_active_hosts_no_repite_un_host_que_aparece_en_dos_paginas():
    NtopngClient.PER_PAGE = 2
    try:
        nt = _FakePages([[_host("10.0.0.1"), _host("10.0.0.2", sent=5)],
                         [_host("10.0.0.2", sent=7), _host("10.0.0.3")],
                         [_host("10.0.0.4")]])
        rows = asyncio.run(nt.active_hosts())
    finally:
        NtopngClient.PER_PAGE = 250
    keys = [(r["ip"], r["vlan"]) for r in rows]
    assert len(keys) == len(set(keys)) == 4


def test_misma_ip_en_dos_vlan_no_es_duplicado():
    NtopngClient.PER_PAGE = 5
    try:
        nt = _FakePages([[_host("10.0.0.1", 10), _host("10.0.0.1", 1)]])
        rows = asyncio.run(nt.active_hosts())
    finally:
        NtopngClient.PER_PAGE = 250
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# H03: un contador que retrocede no es tráfico nuevo; un delta imposible para
# el enlace se descarta (no se recorta a un tope)
# ---------------------------------------------------------------------------
from netmon.collector import FlowCache, HostCache  # noqa: E402

LINK_MIN = int(1e9 / 8 * 60)          # 1 Gbit/s durante 60 s, en bytes


@pytest.mark.parametrize("cache", [HostCache(), FlowCache()])
def test_contador_que_retrocede_descarta_la_muestra(cache):
    # 192.168.15.203 el 28/09: ~21 MB/min y de golpe el acumulado entero (23 GB)
    assert cache.delta(24_000_000_000, 21_000_000, 60) == 0


@pytest.mark.parametrize("cache", [HostCache(), FlowCache()])
def test_delta_normal_se_conserva(cache):
    assert cache.delta(1_000_000, 22_000_000, 60) == 21_000_000


@pytest.mark.parametrize("cache", [HostCache(), FlowCache()])
def test_delta_imposible_para_el_enlace_se_descarta(cache):
    assert cache.delta(0, int(LINK_MIN * 1.5), 60) == 0


@pytest.mark.parametrize("cache", [HostCache(), FlowCache()])
def test_el_limite_escala_con_el_tiempo_real_entre_lecturas(cache):
    # un host que no se leyó en 3 minutos puede acumular 3 minutos de tráfico
    assert cache.delta(0, int(LINK_MIN * 2), 180) == int(LINK_MIN * 2)


# ---------------------------------------------------------------------------
# H21: un ciclo por minuto, alineado al reloj (antes dos ciclos podían caer en
# el mismo minuto y el siguiente quedaba vacío -> picos y valles falsos)
# ---------------------------------------------------------------------------
from datetime import datetime, timezone  # noqa: E402

from netmon.collector import next_cycle_delay  # noqa: E402


def _t(h, m, s, us=0):
    return datetime(2026, 9, 29, h, m, s, us, tzinfo=timezone.utc)


def test_ciclo_arranca_en_el_segundo_2_del_minuto_siguiente():
    assert next_cycle_delay(_t(10, 0, 30, 500000), 60) == pytest.approx(31.5)
    assert next_cycle_delay(_t(10, 0, 59), 60) == pytest.approx(3.0)


def test_si_el_ciclo_termina_antes_del_segundo_2_espera_ese_mismo_minuto():
    assert next_cycle_delay(_t(10, 1, 0, 500000), 60) == pytest.approx(1.5)


def test_nunca_dos_ciclos_en_el_mismo_minuto():
    # ciclo que arrancó 10:01:02 y terminó 10:01:25 -> el próximo es 10:02:02
    start = _t(10, 1, 2)
    end = _t(10, 1, 25)
    nxt = end.timestamp() + next_cycle_delay(end, 60)
    assert int(nxt // 60) == int(start.timestamp() // 60) + 1


# ---------------------------------------------------------------------------
# H18: "hoy", cuotas y reportes en hora argentina (el servidor está en US/Eastern)
# ---------------------------------------------------------------------------
from datetime import date  # noqa: E402

from netmon import db as nmdb  # noqa: E402
from netmon.config import get_settings  # noqa: E402
from netmon.reports import period_bounds  # noqa: E402


def test_el_dia_de_los_reportes_es_el_dia_argentino():
    start, end, _ = period_bounds("day", date(2026, 9, 28))
    assert start.isoformat() == "2026-09-28T00:00:00-03:00"
    assert end.isoformat() == "2026-09-29T00:00:00-03:00"


def test_la_semana_de_los_reportes_es_lunes_a_lunes_argentino():
    start, end, _ = period_bounds("week", date(2026, 9, 30))   # miércoles
    assert start.isoformat() == "2026-09-28T00:00:00-03:00"
    assert end.isoformat() == "2026-10-05T00:00:00-03:00"


def test_inicio_del_dia_en_sql_usa_la_zona_configurada():
    sql = nmdb.day_start_sql()
    assert "America/Argentina/Buenos_Aires" in sql and "date_trunc('day'" in sql


def test_zona_invalida_se_rechaza():
    with pytest.raises(ValueError):
        nmdb.day_start_sql("America/'; DROP TABLE x; --")


def test_hoy_es_el_de_argentina():
    assert get_settings().tz == "America/Argentina/Buenos_Aires"


# ---------------------------------------------------------------------------
# H20: la API arranca aunque PostgreSQL no esté, y responde 503 "sin datos"
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _api(code: str, **env) -> str:
    """Corre `code` con la app importada en un subproceso (settings aislados)."""
    full = {**os.environ, "NETMON_DB_DSN": "postgresql://x:y@127.0.0.1:1/none",
            "NETMON_NTOPNG_URL": "http://127.0.0.1:9", "NETMON_KIOSK_TOKEN": "tok-kiosco-123",
            "NETMON_ADMIN_PASSWORD": "clave-admin-123", "NETMON_SECRET_KEY": "s" * 32, **env}
    pre = ("import sys; sys.path.insert(0, %r)\n"
           "from fastapi.testclient import TestClient\nfrom netmon.api import app\n") % str(REPO)
    r = subprocess.run([sys.executable, "-c", pre + code], capture_output=True, text=True,
                       env=full, timeout=90)
    assert r.returncode == 0, r.stderr[-800:]
    return r.stdout.strip()


def test_api_arranca_sin_base_y_responde_503():
    out = _api("""
with TestClient(app, raise_server_exceptions=False) as c:
    print(c.get('/').status_code, c.get('/api/summary?token=tok-kiosco-123').status_code)
""")
    assert out.splitlines()[-1] == "200 503", out


# ---------------------------------------------------------------------------
# H16: un campo que falta en la respuesta de ntopng no es "0 bytes"
# ---------------------------------------------------------------------------
def test_host_sin_contadores_devuelve_none_no_ceros():
    assert NtopngClient.host_counters({"ip": "10.0.0.1"}) is None


def test_host_con_contadores_los_devuelve():
    assert NtopngClient.host_counters({"bytes.sent": 10, "bytes.rcvd": 20}) == (10, 20)


def test_contador_cero_real_no_es_faltante():
    assert NtopngClient.host_counters({"bytes.sent": 0, "bytes.rcvd": 0}) == (0, 0)


# ---------------------------------------------------------------------------
# H23: un error del propio ping no es "enlace caído"
# ---------------------------------------------------------------------------
from netmon import pinger  # noqa: E402


def test_error_del_ping_no_se_registra_como_perdida(monkeypatch):
    recorded, alerts = [], []

    async def fake_handle(pool, mon, rtt, loss):
        recorded.append((rtt, loss))

    async def fake_alert(pool, kind, sev, msg, meta=None):
        alerts.append(kind)

    monkeypatch.setattr(pinger, "handle_round", fake_handle)
    monkeypatch.setattr(pinger, "raise_alert", fake_alert)
    mon = pinger.TargetMonitor("gateway", "10.0.0.1", privileged=False)

    async def boom():
        raise PermissionError("socket")
    mon.probe = boom
    for _ in range(3):
        asyncio.run(pinger.probe_and_record(None, mon))
    assert recorded == []                       # sin dato, no 100 % de pérdida
    assert alerts == ["pinger"]                 # una sola alerta de "no puedo medir"


# ---------------------------------------------------------------------------
# H30: un host visto SÓLO con MAC de router en varias VLAN no se suma N veces
# ---------------------------------------------------------------------------
from netmon.ntopng_client import own_host_entries  # noqa: E402

VRRP10, VRRP1, WG = "00:00:5e:00:01:2d", "00:00:5e:00:01:2b", "00:90:7f:00:00:03"


def test_host_solo_con_mac_de_router_se_cuenta_una_vez():
    entries = [("10.10.11.77", VRRP10, 10), ("10.10.11.77", VRRP1, 1), ("10.10.11.77", WG, 0),
               # otros hosts que hacen que WG sea MAC de router (>= 3 IPs)
               ("10.10.10.5", WG, 0), ("10.10.10.6", WG, 0)]
    weights = {("10.10.11.77", 10): 24_000, ("10.10.11.77", 1): 900, ("10.10.11.77", 0): 500}
    own = [e for e in own_host_entries(entries, weights) if e[0] == "10.10.11.77"]
    assert own == [("10.10.11.77", 10)]


def test_host_con_mac_propia_sigue_igual():
    entries = [("10.0.0.9", "aa:bb:cc:dd:ee:01", 10), ("10.0.0.9", VRRP1, 1)]
    assert own_host_entries(entries, {}) == [("10.0.0.9", 10)]


# ---------------------------------------------------------------------------
# H25: GeoIP recarga la base cuando cambia el archivo (actualización mensual)
# ---------------------------------------------------------------------------
from netmon import geo  # noqa: E402


def test_geoip_reintenta_si_la_base_aparece_despues(tmp_path, monkeypatch):
    path = tmp_path / "db.mmdb"
    opened = []

    class FakeReader:
        def __init__(self, p): opened.append(p)
        def get(self, ip): return {"country": {"iso_code": "AR"}}
        def close(self): pass

    import maxminddb
    monkeypatch.setattr(maxminddb, "open_database", lambda p: FakeReader(p))
    geo._reset()
    assert geo.country("8.8.8.8", str(path)) == ""        # todavía no existe
    path.write_bytes(b"x")
    assert geo.country("8.8.8.8", str(path)) == "AR"      # apareció: se carga
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# H26: un login con hora ilegible no se registra con la hora de procesamiento
# ---------------------------------------------------------------------------
from netmon.adsync import parse_event_time  # noqa: E402


def test_hora_de_evento_ilegible_es_none():
    assert parse_event_time("no-es-fecha") is None
    assert parse_event_time(None) is None


def test_hora_de_evento_sin_zona_es_utc():
    assert parse_event_time("2026-09-29T13:00:00").isoformat() == "2026-09-29T13:00:00+00:00"


# ---------------------------------------------------------------------------
# H27: sin desglose de throughput, subida y bajada son desconocidas (no "todo bajada")
# ---------------------------------------------------------------------------
def test_throughput_sin_desglose_no_se_asigna_a_bajada():
    ip, mac, name, up, down = NtopngClient.host_row_basics({"ip": "10.0.0.1", "thpt": 12345.0})
    assert up is None and down is None


# ---------------------------------------------------------------------------
# H28: la regla de reputación no marca IPs privadas (otras sedes, RFC1918)
# ---------------------------------------------------------------------------
from netmon.blocklist import Blocklist  # noqa: E402


def test_blocklist_ignora_ips_privadas(tmp_path):
    f = tmp_path / "l.netset"
    f.write_text("192.168.0.0/16\n10.0.0.0/8\n1.2.3.0/24\n")
    bl = Blocklist(str(f))
    assert not bl.contains("192.168.60.22")
    assert not bl.contains("10.1.1.1")
    assert bl.contains("1.2.3.4")


# ---------------------------------------------------------------------------
# H01 (causa 3): la tasa en vivo usa el tiempo REAL entre lecturas de cada host,
# no el intervalo nominal de la vuelta (el listado tarda 5-13 s en recorrerse)
# ---------------------------------------------------------------------------
def test_tasa_en_vivo_usa_el_intervalo_real_de_cada_host():
    from netmon.api import live_rates
    # vuelta anterior: el host se leyó en t=100; esta vuelta en t=118 (18 s reales),
    # aunque la vuelta "nominal" empezó 10 s después de la anterior
    prev = {"10.0.0.1": (0, 0, 100.0)}
    cur = {"10.0.0.1": (18_000_000, 36_000_000, 118.0)}
    up, down = live_rates(prev, cur, cap_bps=1e9)["10.0.0.1"]
    assert up == pytest.approx(18_000_000 * 8 / 18)      # 8 Mbit/s, no 14,4
    assert down == pytest.approx(36_000_000 * 8 / 18)


def test_tasa_en_vivo_host_nuevo_o_contador_que_retrocede_es_cero():
    from netmon.api import live_rates
    rates = live_rates({"a": (500, 500, 1.0)}, {"a": (100, 900, 11.0), "b": (5, 5, 11.0)}, cap_bps=1e9)
    assert rates["a"][0] == 0 and rates["b"] == (0.0, 0.0)


def test_paginas_del_listado_marcan_la_hora_de_lectura():
    NtopngClient.PER_PAGE = 1
    try:
        nt = _FakePages([[_host("10.0.0.1")], [_host("10.0.0.2")]])
        rows = asyncio.run(nt.active_hosts())
    finally:
        NtopngClient.PER_PAGE = 250
    assert all(isinstance(r.get("_nm_t"), float) for r in rows)
    assert rows[1]["_nm_t"] >= rows[0]["_nm_t"]


# ---------------------------------------------------------------------------
# H31: la copia sin VLAN del espejo de un puerto de acceso no abre un segundo
# "equipo" en el historial IP -> equipo
# ---------------------------------------------------------------------------
def test_historial_ignora_la_copia_vlan0_si_la_ip_esta_en_otra_vlan():
    from netmon.collector import assignment_rows
    entries = [("192.168.100.41", "00:17:61:00:00:04", 0),     # fw->host, MAC vieja
               ("192.168.100.41", "6a:02:f4:00:00:02", 1),     # host->fw, MAC actual
               ("10.10.10.9", "aa:bb:cc:00:00:09", 0)]       # sólo VLAN 0: se conserva
    own = {("192.168.100.41", 0), ("192.168.100.41", 1), ("10.10.10.9", 0)}
    rows = assignment_rows(entries, own, routers=set(), names={})
    assert [(ip, mac, vlan) for ip, mac, vlan, _ in rows] == [
        ("192.168.100.41", "6a:02:f4:00:00:02", 1), ("10.10.10.9", "aa:bb:cc:00:00:09", 0)]


# ---------------------------------------------------------------------------
# Mapa DNS pasivo (sitios sin SNI): parser de eve.json de Suricata 7 (dns v2)
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402


def _dns_ev(rrname="www.netflix.com", rcode="NOERROR", typ="answer", answers=None,
            src="10.10.30.5", dst="192.168.100.20"):
    if answers is None:
        answers = [{"rrname": rrname, "rrtype": "CNAME", "ttl": 30, "rdata": "www.dradis.netflix.com"},
                   {"rrname": "www.dradis.netflix.com", "rrtype": "A", "ttl": 60, "rdata": "52.1.2.3"},
                   {"rrname": "www.dradis.netflix.com", "rrtype": "AAAA", "ttl": 60, "rdata": "2600::1"}]
    return _json.dumps({"timestamp": "2026-09-29T12:37:33.569584-0400", "event_type": "dns",
                        "src_ip": src, "dest_ip": dst, "dest_port": 53,
                        "dns": {"version": 2, "type": typ, "rrname": rrname, "rrtype": "A",
                                "rcode": rcode, "answers": answers}})


def test_respuesta_dns_da_cliente_ip_respuesta_y_nombre_preguntado():
    from netmon.dnsmap import parse_dns_line
    got = parse_dns_line(_dns_ev())
    assert [(c, a, d) for c, a, d, _ in got] == [
        ("10.10.30.5", "52.1.2.3", "www.netflix.com"), ("10.10.30.5", "2600::1", "www.netflix.com")]
    assert got[0][3].isoformat() == "2026-09-29T12:37:33.569584-04:00"


def test_dns_sin_respuesta_util_no_genera_nada():
    from netmon.dnsmap import parse_dns_line
    assert parse_dns_line(_dns_ev(rcode="NXDOMAIN", answers=[])) == []
    assert parse_dns_line(_dns_ev(typ="query")) == []
    assert parse_dns_line('{"event_type": "flow", "src_ip": "1.1.1.1"}') == []
    assert parse_dns_line("no es json") == []
    assert parse_dns_line(_dns_ev(rrname="23.1.201.10.in-addr.arpa")) == []


def test_lector_de_eve_sigue_la_rotacion(tmp_path):
    from netmon.dnsmap import EveTail
    f = tmp_path / "eve.json"
    f.write_text("vieja-1\n")
    t = EveTail(str(f), from_start=False)          # arranca al final: no relee lo viejo
    assert t.read_lines() == []
    with f.open("a") as fh:
        fh.write("nueva-1\nnueva-2 parcial")
    assert t.read_lines() == ["nueva-1"]           # la línea incompleta espera
    with f.open("a") as fh:
        fh.write(" fin\n")
    assert t.read_lines() == ["nueva-2 parcial fin"]
    f.rename(tmp_path / "eve.json.1")              # logrotate: renombra y Suricata crea otro
    (tmp_path / "eve.json").write_text("rotada-1\n")
    assert t.read_lines() == ["rotada-1"]


# ---------------------------------------------------------------------------
# H33: "internet" es un destino con IP pública; la otra sede por VPN
# (192.168.60.x, 192.168.150.x) es tráfico interno aunque no esté en
# NETMON_LOCAL_NETWORKS (mismo criterio que ntopng para el contador del host)
# ---------------------------------------------------------------------------
def test_ambito_otra_sede_por_vpn_es_interno():
    from netmon.collector import flow_scope
    assert flow_scope("192.168.60.10") == "interno"
    assert flow_scope("10.13.1.5") == "interno"
    assert flow_scope("172.16.4.4") == "interno"


def test_ambito_ip_publica_es_internet():
    from netmon.collector import flow_scope
    assert flow_scope("52.1.2.3") == "internet"
    assert flow_scope("2600::1") == "internet"


def test_ambito_multicast_o_reservada_es_interno():
    from netmon.collector import flow_scope
    assert flow_scope("239.255.255.250") == "interno"
    assert flow_scope("255.255.255.255") == "interno"



# ---------------------------------------------------------------------------
# Registro de conexiones: una conexión que EMPEZÓ después de la lectura anterior
# se cuenta entera (antes, la primera vez que se veía un flujo contaba 0 y las
# conexiones cortas -que empiezan y terminan entre dos ciclos- se perdían)
# ---------------------------------------------------------------------------
def test_flujo_nuevo_desde_la_lectura_anterior_cuenta_todo():
    from netmon.collector import first_sight_bytes
    assert first_sight_bytes(first_seen=1000, cur=5_000_000, prev_fetch=990, now=1030) == 5_000_000


def test_flujo_viejo_visto_por_primera_vez_no_cuenta():
    from netmon.collector import first_sight_bytes
    # empezó antes de la lectura anterior: parte de sus bytes pudo ya estar en otra fila
    assert first_sight_bytes(first_seen=900, cur=5_000_000, prev_fetch=990, now=1030) == 0


def test_sin_lectura_anterior_no_cuenta():
    from netmon.collector import first_sight_bytes
    assert first_sight_bytes(first_seen=1000, cur=5_000_000, prev_fetch=None, now=1030) == 0


def test_flujo_nuevo_imposible_para_el_enlace_se_descarta():
    from netmon.collector import first_sight_bytes
    assert first_sight_bytes(first_seen=1000, cur=50_000_000_000, prev_fetch=990, now=1030) == 0


def test_flow_row_trae_first_seen():
    assert NtopngClient.flow_row({"first_seen": 1790624508})["first_seen"] == 1790624508


# ---------------------------------------------------------------------------
# Reportes descargables (PDF/CSV) — generación sin base de datos
# ---------------------------------------------------------------------------
from datetime import timedelta as _td  # noqa: E402


def _fake_report_data(ndays=30):
    from netmon import reports
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).date()
    daily = []
    for i in range(ndays):
        up, down = (i + 1) * 10 * 1048576, (i + 1) * 40 * 1048576
        inet = int((up + down) * 0.6)
        daily.append({"day": (base + _td(days=i)).isoformat(), "bytes_up": up,
                      "bytes_down": down, "total": up + down, "bytes_internet": inet,
                      "bytes_internal": (up + down) - inet, "bytes_unclassified": 0})
    tot = sum(d["total"] for d in daily)
    inet = sum(d["bytes_internet"] for d in daily)
    peak = max(daily, key=lambda x: x["total"])
    totals = {"bytes_up": sum(d["bytes_up"] for d in daily),
              "bytes_down": sum(d["bytes_down"] for d in daily), "total": tot,
              "bytes_internet": inet, "bytes_internal": tot - inet,
              "bytes_unclassified": 0, "peak_day": peak["day"], "peak_total": peak["total"]}
    top = [{"ip": f"10.0.0.{i}", "hostname": f"PC-{i}", "ad_user": f"u{i}", "vendor": "x",
            "equipos": 1, "bytes_up": 100 * 1048576, "bytes_down": 300 * 1048576,
            "bytes_internet": 280 * 1048576, "bytes_unclassified": 0,
            "total": 400 * 1048576} for i in range(1, 6)]
    return reports, {"label": "Mes de 2026-09", "start": None, "end": None, "top": top,
                     "categories": [{"category": "streaming", "total": 500 * 1048576},
                                    {"category": "sistema", "total": 120 * 1048576}],
                     "host_apps": {}, "top_apps": [], "daily": daily, "totals": totals,
                     "active_hosts": 42, "apps_since": None, "internet_since": None}


def test_pdf_del_reporte_se_genera_con_graficos():
    reports, data = _fake_report_data(30)
    pdf = reports.build_pdf(data)
    assert pdf[:4] == b"%PDF" and len(pdf) > 3000


def test_csv_del_reporte_incluye_resumen_y_consumo_por_dia():
    reports, data = _fake_report_data(10)
    csv_bytes = reports.build_csv(data)
    text = csv_bytes.decode("utf-8-sig")
    assert "Resumen del período" in text
    assert "Consumo por día" in text
    assert "2026-09-01" in text   # el desglose diario aparece
