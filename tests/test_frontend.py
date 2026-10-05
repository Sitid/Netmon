"""Tests del frontend en un Chromium headless (Playwright). Se saltean si Playwright
no está instalado. Sirven los archivos de frontend/ del repo con un servidor local.

    pip install playwright && playwright install chromium-headless-shell
    python -m pytest -q tests/test_frontend.py
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

FRONT = Path(__file__).resolve().parent.parent / "frontend"


class _Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?")[0]
        if path in ("/", "/kiosk"):
            return str(FRONT / "index.html")
        if path.startswith("/static/"):
            return str(FRONT / path[len("/static/"):])
        return str(FRONT / "no-existe")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def page(base_url):
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        # el resto de la API responde 503 (fuente caída); /api/me responde como
        # kiosco. En Playwright gana la ÚLTIMA ruta registrada: la específica va después.
        pg.route("**/api/**", lambda r: r.fulfill(status=503, json={"detail": "test"}))
        pg.route("**/api/me*", lambda r: r.fulfill(json={"role": "kiosk", "user": ""}))
        yield pg
        b.close()


# ---------------------------------------------------------------------------
# H01/H02: fmtMbps recibe bits/s (live_loop manda bits/s; ntopng thpt.bps de
# flujos también es bits/s). 1.000.000 bits/s = 1 Mbps, no 8.
# ---------------------------------------------------------------------------
def test_fmtMbps_recibe_bits_por_segundo(page, base_url):
    page.goto(base_url + "/")
    page.wait_for_function("window.NM !== undefined")
    assert page.evaluate("NM.fmtMbps(1e6)") == "1.00"
    assert page.evaluate("NM.fmtMbps(450e6)") == "450"


# ---------------------------------------------------------------------------
# H05/H06/H11: fuente caída -> la UI muestra "sin datos", no el último número
# ---------------------------------------------------------------------------
def _iso(sec_ago):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=sec_ago)).isoformat()


def _live(targets_age=5, down=300e6):
    return json.dumps({"type": "live", "ts": _iso(0), "alerts_open": 0,
                       "totals": {"up_bps": 100e6, "down_bps": down}, "hosts": [],
                       "targets": [{"target": "gateway", "address": "10.0.0.1", "up": True,
                                    "since": _iso(3600), "last_rtt_ms": 1.0, "last_loss_pct": 0,
                                    "updated_at": _iso(targets_age)}]})


def _open_resumen(page, base_url, messages):
    def ws_handler(ws):
        for m in messages:
            ws.send(m)
    page.route_web_socket("**/ws*", ws_handler)
    page.goto(base_url + "/?token=x#/resumen")
    page.wait_for_selector("#k-bw-d")
    page.wait_for_timeout(1500)


def test_ntopng_caido_el_indicador_no_dice_en_vivo(page, base_url):
    _open_resumen(page, base_url, [_live(), json.dumps({"type": "live_error", "message": "x"}),
                                   json.dumps({"type": "rt", "sample": None})])
    txt = page.inner_text("#conn-text").lower()
    assert "en vivo" not in txt and "sin datos" in txt, txt


def test_ntopng_caido_el_kpi_no_queda_con_el_ultimo_valor(page, base_url):
    _open_resumen(page, base_url, [_live(), json.dumps({"type": "live_error", "message": "x"})])
    assert page.inner_text("#k-bw-d").strip() in ("—", "sin datos"), page.inner_text("#k-bw-d")


def test_estado_de_red_viejo_se_muestra_sin_datos(page, base_url):
    """target_state de hace 10 min (pinger muerto) no puede decir 'En línea'."""
    _open_resumen(page, base_url, [_live(targets_age=600)])
    assert "sin datos" in page.inner_text("#k-net").lower(), page.inner_text("#k-net")


def test_estado_de_red_fresco_dice_en_linea(page, base_url):
    _open_resumen(page, base_url, [_live(targets_age=5)])
    assert page.inner_text("#k-net").strip() == "En línea"
