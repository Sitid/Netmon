"""API web de netmon: REST + WebSocket + frontend estático.

Roles:
  * admin : login con NETMON_ADMIN_PASSWORD (cookie firmada). Puede confirmar
            dispositivos, reconocer alertas y descargar reportes.
  * kiosk : token de solo lectura por query (?token=...) o Bearer. Pensado
            para la pantalla fija de Sistemas.

Un task de fondo consulta ntopng cada NETMON_LIVE_INTERVAL segundos y hace
broadcast por WebSocket de: top hosts en vivo (Mbps), totales de interfaz,
estado de enlaces y alertas sin reconocer.

Correr en producción con la unit netmon-api (uvicorn, 1 worker: el estado de
WebSockets vive en el proceso).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import shutil
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import httpx
from fastapi import (Depends, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from . import attribution, db, purge, reports
from .categories import CATEGORY_ORDER, categorize
from .config import get_settings
from .geo import country as geo_country
from .sites import SITE_NAMES, friendly_site, site_domains
from .ntopng_client import NtopngClient, as_num, own_host_entries, pick

log = logging.getLogger("netmon.api")

SESSION_COOKIE = "nm_session"
SESSION_MAX_AGE = 12 * 3600

settings = get_settings()
signer = URLSafeTimedSerializer(settings.secret_key, salt="netmon-session")
kiosk_signer = URLSafeTimedSerializer(settings.secret_key, salt="netmon-kiosk")

app = FastAPI(title="netmon", docs_url=None, redoc_url=None)


@app.middleware("http")
async def revalidate_frontend(request: Request, call_next):
    """Frontend siempre revalidado (ETag -> 304): sin esto el navegador seguía
    usando JS viejo después de un deploy."""
    path = request.url.path
    # sin base (arranque con PostgreSQL caído): la API dice "sin datos", no explota
    if path.startswith("/api/") and state.get("pool") is None:
        return JSONResponse({"detail": "Base de datos no disponible"}, status_code=503)
    response = await call_next(request)
    if path in ("/", "/kiosk") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith(PERSONAL_PREFIXES) and response.status_code < 400:
        await _log_access(request, response.status_code)
    return response


# Endpoints que devuelven datos de un equipo, una persona o un sitio: cada consulta
# queda registrada con quién la hizo (auditoría H10). El token nunca se guarda.
PERSONAL_PREFIXES = (
    "/api/hosts/", "/api/ip/", "/api/ipcard/", "/api/user/", "/api/users-usage",
    "/api/search", "/api/site-hosts", "/api/app-hosts", "/api/category-hosts",
    "/api/flows", "/api/top", "/api/devices", "/api/report",
)


async def _log_access(request: Request, status: int) -> None:
    """Se espera la escritura (~1 ms): una tarea suelta podía perderse en un
    reinicio y la consulta quedaba sin registrar."""
    pool = state.get("pool")
    if pool is None:
        return
    query = "&".join(f"{k}={v}" for k, v in request.query_params.multi_items() if k != "token")
    st = request.state
    row = (getattr(st, "user", "") or "", getattr(st, "role", "") or "",
           request.client.host if request.client else "", request.method,
           request.url.path, query[:500], status)

    try:
        await pool.execute(
            """INSERT INTO access_log (username, role, client_ip, method, path, query, status)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""", *row)
    except Exception:
        log.exception("no pude registrar el acceso")

# Estado del proceso (poblado en lifespan/startup)
state: dict = {
    "pool": None,
    "nt": None,
    "ws_clients": set(),
    "hostname_cache": {},
    # ring buffer del gráfico realtime de interfaz: ~30 min a 2 s por muestra
    "rt_buf": deque(maxlen=900),
}

# Tiers de consulta estilo RRD: rango -> (tabla, bucket_segundos, intervalo SQL)
TIMELINE_TIERS = {
    "1h":  ("traffic_min",  60,   "1 hour"),
    "24h": ("traffic_min",  600,  "24 hours"),
    "7d":  ("traffic_5min", 1800, "7 days"),
    "30d": ("traffic_hour", 3600, "30 days"),
}
APP_TIERS = {
    "1h":  ("app_min",  60,    "1 hour"),
    "24h": ("app_min",  600,   "24 hours"),
    "7d":  ("app_hour", 3600,  "7 days"),
    "30d": ("app_hour", 86400, "30 days"),
}
CAT_TIERS = {
    "1h":  ("traffic_cat_min",  60,    "1 hour"),
    "24h": ("traffic_cat_min",  600,   "24 hours"),
    "7d":  ("traffic_cat_hour", 3600,  "7 days"),
    "30d": ("traffic_cat_hour", 86400, "30 days"),
}


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    """scrypt (stdlib) con sal aleatoria: 'scrypt$n$r$p$sal$hash' en base64."""
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        n, r, p, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                              n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


async def _session_from_cookie(raw: str | None) -> dict | None:
    """{'role', 'user'} de una cookie de sesión válida, o None.

    Los usuarios de la tabla se revalidan en cada pedido: deshabilitar, borrar
    o cambiar el rol tiene efecto inmediato. La sesión de la clave del .env
    (src='env') es el acceso de emergencia del admin y no depende de la tabla.
    """
    if not raw:
        return None
    try:
        data = signer.loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    if data.get("src") == "env":
        return {"role": "admin", "user": "admin"}
    username = data.get("user")
    if not username:
        return None
    row = await state["pool"].fetchrow(
        "SELECT role FROM users WHERE username = $1 AND enabled", username)
    return {"role": row["role"], "user": username} if row else None


def _token_ok(token: str | None) -> bool:
    return token is not None and token != "" and hmac.compare_digest(token, settings.kiosk_token)


# --- Kiosco (auditoría H08) ----------------------------------------------------
# El token de la pantalla fija sólo abre lo que esa pantalla muestra (Resumen y
# Estado de red). Fichas por equipo/usuario, búsqueda, sitios y conexiones piden
# sesión. /kiosk?token=... cambia el token por una cookie y redirige, así el
# token no viaja en cada pedido (ni queda en cada línea del log de nginx).
KIOSK_COOKIE = "nm_kiosk"
KIOSK_COOKIE_MAX_AGE = 30 * 86400
KIOSK_PATHS = frozenset({
    "/api/me", "/api/summary", "/api/timeline", "/api/categories", "/api/overview",
    "/api/alerts", "/api/ping", "/api/ping/summary", "/api/interface/realtime",
})


def _kiosk_fingerprint() -> str:
    # atada al token actual: si se rota el token, las cookies viejas dejan de valer
    return hashlib.sha256(settings.kiosk_token.encode()).hexdigest()[:16]


def _kiosk_cookie_ok(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        data = kiosk_signer.loads(raw, max_age=KIOSK_COOKIE_MAX_AGE)
    except BadSignature:
        return False
    return hmac.compare_digest(str(data.get("k", "")), _kiosk_fingerprint())


def _is_kiosk(request: Request) -> bool:
    token = request.query_params.get("token")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = token or auth[7:]
    return _token_ok(token) or _kiosk_cookie_ok(request.cookies.get(KIOSK_COOKIE))


async def require_view(request: Request) -> str:
    """Sesión (admin o viewer) o kiosco (sólo KIOSK_PATHS). Devuelve el rol."""
    sess = await _session_from_cookie(request.cookies.get(SESSION_COOKIE))
    if sess:
        request.state.user = sess["user"]
        request.state.role = sess["role"]
        return sess["role"]
    if _is_kiosk(request):
        request.state.role = "kiosk"
        if request.url.path not in KIOSK_PATHS:
            raise HTTPException(403, "El acceso de kiosco sólo incluye el Resumen y el "
                                     "Estado de red: para ver datos por equipo o persona, iniciá sesión")
        return "kiosk"
    raise HTTPException(401, "No autenticado")


# --- Límite de intentos de login (auditoría H09) --------------------------------
LOGIN_MAX_FAILS = 5          # intentos fallidos...
LOGIN_WINDOW_S = 600         # ...en 10 minutos
LOGIN_LOCK_S = 900           # bloquean 15 minutos (por IP y por usuario)
_login_fails: dict[str, deque] = {}


def _login_locked(keys: list[str], now: float) -> float:
    """Segundos de bloqueo restantes (0 = puede intentar)."""
    remaining = 0.0
    for k in keys:
        dq = _login_fails.get(k)
        if not dq:
            continue
        while dq and now - dq[0] > LOGIN_WINDOW_S + LOGIN_LOCK_S:
            dq.popleft()
        recent = [t for t in dq if now - t <= LOGIN_WINDOW_S]
        if len(recent) >= LOGIN_MAX_FAILS:
            remaining = max(remaining, LOGIN_LOCK_S - (now - dq[-1]))
    return max(remaining, 0.0)


def _login_failed(keys: list[str], now: float) -> None:
    for k in keys:
        _login_fails.setdefault(k, deque(maxlen=50)).append(now)


async def require_user(request: Request) -> str:
    """Sesión de usuario (admin o viewer); el token kiosco no alcanza."""
    sess = await _session_from_cookie(request.cookies.get(SESSION_COOKIE))
    if sess:
        request.state.user = sess["user"]
        request.state.role = sess["role"]
        return sess["role"]
    raise HTTPException(401, "Requiere iniciar sesión")


async def require_admin(request: Request) -> str:
    sess = await _session_from_cookie(request.cookies.get(SESSION_COOKIE))
    if sess and sess["role"] == "admin":
        request.state.user = sess["user"]
        request.state.role = "admin"
        return "admin"
    raise HTTPException(403, "Requiere perfil administrador")


class LoginBody(BaseModel):
    username: str = ""
    password: str


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    username = body.username.strip()
    client = request.client.host if request.client else "?"
    keys = [f"ip:{client}", f"user:{(username or 'admin').lower()}"]
    now = time.monotonic()
    wait = _login_locked(keys, now)
    if wait:
        log.warning("login bloqueado para %s / %s (%.0f s)", client, username or "admin", wait)
        raise HTTPException(429, f"Demasiados intentos fallidos: esperá {int(wait // 60) + 1} min",
                            headers={"Retry-After": str(int(wait))})
    payload = None
    if username:
        row = await state["pool"].fetchrow(
            "SELECT role, password_hash FROM users WHERE username = $1 AND enabled",
            username)
        if row and _verify_password(body.password, row["password_hash"]):
            payload = {"user": username}
            await state["pool"].execute(
                "UPDATE users SET last_login = now() WHERE username = $1", username)
    if payload is None and username in ("", "admin") and \
            hmac.compare_digest(body.password, settings.admin_password):
        payload = {"user": "admin", "src": "env"}   # acceso de emergencia (.env)
    if payload is None:
        _login_failed(keys, now)
        await asyncio.sleep(0.5)  # frena la fuerza bruta
        raise HTTPException(401, "Usuario o clave incorrectos")
    if payload.get("src") == "env":
        log.warning("ingreso con la clave de emergencia del .env desde %s", client)
    # detrás de nginx (X-Forwarded-Proto) el esquema es https: la cookie sólo viaja cifrada
    response.set_cookie(
        SESSION_COOKIE, signer.dumps(payload),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request, role: str = Depends(require_view)):
    return {"role": role, "user": getattr(request.state, "user", "")}


# ---------------------------------------------------------------------------
# Usuarios (solo admin)
# ---------------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
USER_ROLES = ("admin", "viewer")
MIN_PASSWORD_LEN = 8


class UserCreateBody(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserUpdateBody(BaseModel):
    role: str | None = None
    enabled: bool | None = None
    password: str | None = None


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"La clave debe tener al menos {MIN_PASSWORD_LEN} caracteres")


@app.get("/api/users")
async def users_list(role: str = Depends(require_admin)):
    rows = await state["pool"].fetch(
        """SELECT username, role, enabled, created_at, last_login
           FROM users ORDER BY username""")
    return [dict(r) for r in rows]


@app.post("/api/users")
async def user_create(body: UserCreateBody, role: str = Depends(require_admin)):
    username = body.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Usuario inválido: 3-32 caracteres, letras, números, . _ -")
    if body.role not in USER_ROLES:
        raise HTTPException(400, f"role debe ser uno de {list(USER_ROLES)}")
    _check_password(body.password)
    try:
        await state["pool"].execute(
            "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)",
            username, _hash_password(body.password), body.role)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Ese usuario ya existe")
    return {"ok": True}


@app.post("/api/users/{username}")
async def user_update(username: str, body: UserUpdateBody, request: Request,
                      role: str = Depends(require_admin)):
    pool: asyncpg.Pool = state["pool"]
    if not await pool.fetchval("SELECT 1 FROM users WHERE username = $1", username):
        raise HTTPException(404, "No existe")
    is_self = username == request.state.user
    if body.role is not None:
        if body.role not in USER_ROLES:
            raise HTTPException(400, f"role debe ser uno de {list(USER_ROLES)}")
        if is_self and body.role != "admin":
            raise HTTPException(400, "No podés quitarte el perfil administrador a vos mismo")
        await pool.execute("UPDATE users SET role = $2 WHERE username = $1", username, body.role)
    if body.enabled is not None:
        if is_self and not body.enabled:
            raise HTTPException(400, "No podés deshabilitar tu propio usuario")
        await pool.execute("UPDATE users SET enabled = $2 WHERE username = $1",
                           username, body.enabled)
    if body.password is not None:
        _check_password(body.password)
        await pool.execute("UPDATE users SET password_hash = $2 WHERE username = $1",
                           username, _hash_password(body.password))
    return {"ok": True}


@app.delete("/api/users/{username}")
async def user_delete(username: str, request: Request, role: str = Depends(require_admin)):
    if username == request.state.user:
        raise HTTPException(400, "No podés borrar tu propio usuario")
    result = await state["pool"].execute("DELETE FROM users WHERE username = $1", username)
    if result.endswith(" 0"):
        raise HTTPException(404, "No existe")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints de datos
# ---------------------------------------------------------------------------

@app.get("/api/summary")
async def summary(role: str = Depends(require_view)):
    pool: asyncpg.Pool = state["pool"]
    row = await pool.fetchrow(
        """SELECT COALESCE(SUM(bytes_up),0) AS up, COALESCE(SUM(bytes_down),0) AS down
           FROM traffic_min WHERE ts > now() - interval '5 minutes'"""
    )
    devices = await pool.fetchrow(
        """SELECT COUNT(*) FILTER (WHERE last_seen > now() - interval '24 hours') AS active,
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE NOT is_known) AS unconfirmed
           FROM devices"""
    )
    targets = await pool.fetch("SELECT * FROM target_state ORDER BY target")
    alerts_open = await pool.fetchval("SELECT COUNT(*) FROM alerts WHERE NOT acked")
    return {
        "traffic_5m": dict(row),
        "devices": dict(devices),
        "targets": [dict(t) for t in targets],
        "alerts_open": alerts_open,
    }


@app.get("/api/top")
async def top(range: str = "1h", limit: int = 50, role: str = Depends(require_view)):
    if range not in db.RANGE_INTERVALS:
        raise HTTPException(400, f"range debe ser uno de {list(db.RANGE_INTERVALS)}")
    rows = await db.top_talkers(state["pool"], range, min(limit, 200))
    # cuántos equipos distintos usaron cada IP en el período (DHCP, auditoría H04)
    eq = await _equipos_por_ip(state["pool"], [r["ip"] for r in rows],
                               datetime.now(timezone.utc) - _RANGE_DELTA[range])
    for r in rows:
        r["equipos"] = eq.get(r["ip"], 0)
    return rows


@app.get("/api/timeline")
async def timeline(range: str = "1h", ip: str = "", role: str = Depends(require_view)):
    """Serie temporal de tráfico, total o de un host (tier según el rango)."""
    if range not in TIMELINE_TIERS:
        raise HTTPException(400, f"range debe ser uno de {list(TIMELINE_TIERS)}")
    table, bucket, interval = TIMELINE_TIERS[range]
    pool: asyncpg.Pool = state["pool"]
    ip_filter = "AND ip = $2::inet" if ip else ""
    args = [interval, ip] if ip else [interval]
    rows = await pool.fetch(
        f"""SELECT to_timestamp(floor(extract(epoch FROM ts) / {bucket}) * {bucket}) AS t,
                   SUM(bytes_up) AS up, SUM(bytes_down) AS down,
                   SUM(bytes_internet) AS internet
            FROM {table}
            WHERE ts > now() - ($1::text)::interval {ip_filter}
            GROUP BY 1 ORDER BY 1""",
        *args,
    )
    return {"bucket_s": bucket, "points": [dict(r) for r in rows]}


@app.get("/api/categories")
async def categories(range: str = "1h", role: str = Depends(require_view)):
    """Totales por categoría + serie apilada para el gráfico."""
    if range not in CAT_TIERS:
        raise HTTPException(400, f"range debe ser uno de {list(CAT_TIERS)}")
    table, bucket, interval = CAT_TIERS[range]
    pool: asyncpg.Pool = state["pool"]
    series = await pool.fetch(
        f"""SELECT to_timestamp(floor(extract(epoch FROM ts) / {bucket}) * {bucket}) AS t,
                   category, SUM(bytes_up + bytes_down) AS total
            FROM {table} WHERE ts > now() - ($1::text)::interval
            GROUP BY 1, 2 ORDER BY 1""",
        interval,
    )
    totals = await pool.fetch(
        f"""SELECT category, SUM(bytes_up + bytes_down) AS total
            FROM {table} WHERE ts > now() - ($1::text)::interval
            GROUP BY 1 ORDER BY 2 DESC""",
        interval,
    )
    return {
        "bucket_s": bucket,
        "series": [dict(r) for r in series],
        "totals": [dict(r) for r in totals],
    }


@app.get("/api/apps")
async def apps(range: str = "1h", role: str = Depends(require_view)):
    """Top de aplicaciones nDPI (toda la red) + serie temporal de las top 8."""
    if range not in APP_TIERS:
        raise HTTPException(400, f"range debe ser uno de {list(APP_TIERS)}")
    table, bucket, interval = APP_TIERS[range]
    pool: asyncpg.Pool = state["pool"]
    totals = await pool.fetch(
        f"""SELECT app, MAX(category) AS category,
                   SUM(bytes_up) AS bytes_up, SUM(bytes_down) AS bytes_down
            FROM {table} WHERE ts > now() - ($1::text)::interval
            GROUP BY app ORDER BY SUM(bytes_up + bytes_down) DESC LIMIT 20""",
        interval,
    )
    top_apps = [r["app"] for r in totals[:8]]
    series = []
    if top_apps:
        series = await pool.fetch(
            f"""SELECT to_timestamp(floor(extract(epoch FROM ts) / {bucket}) * {bucket}) AS t,
                       app, SUM(bytes_up + bytes_down) AS total
                FROM {table}
                WHERE ts > now() - ($1::text)::interval AND app = ANY($2)
                GROUP BY 1, 2 ORDER BY 1""",
            interval, top_apps,
        )
    return {
        "bucket_s": bucket,
        "totals": [dict(r) for r in totals],
        "series": [dict(r) for r in series],
    }


@app.get("/api/ping")
async def ping_history(range: str = "24h", role: str = Depends(require_view)):
    pool: asyncpg.Pool = state["pool"]
    interval = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days"}.get(range, "24 hours")
    rows = await pool.fetch(
        """SELECT ts, target, rtt_avg_ms, loss_pct FROM ping_min
           WHERE ts > now() - ($1::text)::interval ORDER BY ts""",
        interval,
    )
    return [dict(r) for r in rows]


@app.get("/api/ping/summary")
async def ping_summary(range: str = "24h", role: str = Depends(require_view)):
    """Uptime %, latencia promedio y caídas por target, para la vista Estado."""
    pool: asyncpg.Pool = state["pool"]
    interval = {"24h": "24 hours", "7d": "7 days", "30d": "30 days"}.get(range, "24 hours")
    stats = await pool.fetch(
        """SELECT target,
                  COUNT(*)         AS minutes,
                  MIN(ts)          AS first_ts,
                  AVG(rtt_avg_ms)  AS rtt_avg,
                  MAX(rtt_max_ms)  AS rtt_max,
                  AVG(loss_pct)    AS loss_avg,
                  100.0 * AVG(CASE WHEN loss_pct >= 100 THEN 0 ELSE 1 END) AS uptime_pct
           FROM ping_min WHERE ts > now() - ($1::text)::interval
           GROUP BY target ORDER BY target""",
        interval,
    )
    outages = await pool.fetch(
        """SELECT meta->>'target' AS target, COUNT(*) AS downs
           FROM alerts WHERE kind = 'link_down'
             AND ts > now() - ($1::text)::interval
           GROUP BY 1""",
        interval,
    )
    downs = {r["target"]: r["downs"] for r in outages}
    current = {r["target"]: dict(r) for r in await pool.fetch("SELECT * FROM target_state")}
    # minutos esperados en el período: el uptime se calcula sólo sobre los medidos,
    # así que la UI tiene que mostrar la cobertura (auditoría H11)
    expected = {"24h": 1440, "7d": 10080, "30d": 43200}.get(range, 1440)
    return [
        {**dict(r), "downs": downs.get(r["target"], 0),
         "expected_minutes": expected,
         "state": current.get(r["target"], {})}
        for r in stats
    ]


# ---------------------------------------------------------------------------
# Hosts y flujos (paridad ntopng: datos en vivo directo de su API)
# ---------------------------------------------------------------------------

_NTOP_OS = {1: "Linux", 2: "Windows", 3: "macOS", 4: "iOS", 5: "Android"}


def _host_extra(row: dict) -> dict:
    """Campos adicionales de una fila de active hosts, con candidatos por versión."""
    os_name = pick(row, "os_detail", default="") or pick(row, "os", default="")
    if not isinstance(os_name, str):
        # ntopng devuelve el enum OSType (ntop_typedefs.h) cuando no tiene detalle
        os_name = _NTOP_OS.get(int(as_num(os_name)), "")
    flows = int(
        as_num(pick(row, "active_flows.as_client", default=0))
        + as_num(pick(row, "active_flows.as_server", default=0))
    ) or int(as_num(pick(row, "num_flows", "flows", default=0)))
    return {
        "os": os_name,
        "flows": flows,
        "bytes_sent": int(as_num(pick(row, "bytes.sent", "bytes_sent", default=0))),
        "bytes_rcvd": int(as_num(pick(row, "bytes.recvd", "bytes.rcvd", "bytes_rcvd", default=0))),
    }


@app.get("/api/hosts")
async def hosts_list(role: str = Depends(require_view)):
    """Todos los hosts activos que ve ntopng (locales y remotos), enriquecidos."""
    nt: NtopngClient = state["nt"]
    pool: asyncpg.Pool = state["pool"]
    if not state["hostname_cache"]:
        await refresh_hostname_cache()
    vendors = {
        r["mac"]: r["vendor"] for r in
        await pool.fetch("SELECT mac::text AS mac, vendor FROM devices WHERE vendor <> ''")
    }
    try:
        rows = await nt.active_hosts()
    except (httpx.HTTPError, RuntimeError) as exc:
        # fuente caída: "sin datos" explícito, no un 500 (auditoría H19)
        raise HTTPException(503, f"ntopng no responde: {exc.__class__.__name__}")
    out = []
    for row in rows:
        ip, mac, name, up_bps, down_bps = nt.host_row_basics(row)
        if not ip:
            continue
        cached = state["hostname_cache"].get(ip, ("", ""))
        out.append({
            "ip": ip,
            "mac": mac,
            "hostname": cached[0] or (name if name != ip else ""),
            "user": cached[1],
            "vendor": vendors.get((mac or "").lower(), ""),
            "local": settings.is_local_ip(ip),
            "up_bps": up_bps,
            "down_bps": down_bps,
            **_host_extra(row),
        })
    out.sort(key=lambda h: h["bytes_sent"] + h["bytes_rcvd"], reverse=True)
    return out


async def _category_overrides() -> dict[str, str]:
    rows = await state["pool"].fetch("SELECT app, category FROM category_map")
    return {r["app"]: r["category"] for r in rows}


def _valid_ip(ip: str) -> str:
    """Normaliza la IP del path (tolera el sufijo /32 de links viejos)."""
    try:
        return str(ipaddress.ip_address(ip.split("/")[0]))
    except ValueError:
        raise HTTPException(400, "IP inválida")


_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def _valid_mac(mac: str) -> str:
    """Valida y normaliza una MAC (formato aa:bb:cc:dd:ee:ff)."""
    mac = mac.strip().lower()
    if not _MAC_RE.match(mac):
        raise HTTPException(400, "MAC inválida")
    return mac


@app.get("/api/hosts/{ip}")
async def host_detail(ip: str, role: str = Depends(require_view)):
    """Detalle de un host: identidad (DB) + estado en vivo de ntopng si está activo.

    El consumo histórico por categoría/app se pide aparte con
    /api/hosts/{ip}/usage y la serie con /api/timeline?ip=...
    Si la IP figura en varias VLAN se suman sus entradas con MAC propia (la
    copia inter-VLAN con MAC del router se descarta, igual que en el colector).
    """
    ip = _valid_ip(ip)
    nt: NtopngClient = state["nt"]
    pool: asyncpg.Pool = state["pool"]
    source_ok = True
    # VLAN(s) propias del host: el colector las registra cada minuto en
    # ip_assignments; recorrer el listado completo de ntopng tardaba 5-14 s
    # (auditoría H24). Si el host no se vio en los últimos minutos, no está activo.
    vlans = sorted(r["vlan"] for r in await pool.fetch(
        """SELECT DISTINCT vlan FROM ip_assignments
           WHERE ip = $1::inet AND last_seen > now() - interval '3 minutes'""", ip))

    details: list[dict] = []
    for vlan in vlans:
        try:
            details.append(await nt.host_data(f"{ip}@{vlan}" if vlan else ip))
        except httpx.HTTPStatusError:
            continue  # el host pudo purgar entre llamadas
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("host_data(%s) falló: %s", ip, exc)
            source_ok = False   # no es "equipo inactivo": ntopng no respondió
            break

    bytes_sent = bytes_rcvd = 0
    ndpi: dict[str, list[int]] = {}
    for detail in details:
        counters = nt.host_counters(detail)
        if counters is None:
            continue
        s_, r_ = counters
        bytes_sent += s_
        bytes_rcvd += r_
        for proto, (ps, pr) in nt.host_ndpi(detail).items():
            acc = ndpi.setdefault(proto, [0, 0])
            acc[0] += ps
            acc[1] += pr

    overrides = await _category_overrides()
    apps_list = [
        {"app": proto, "category": categorize(proto, overrides),
         "bytes_up": s_, "bytes_down": r_}
        for proto, (s_, r_) in ndpi.items()
    ]
    apps_list.sort(key=lambda a: a["bytes_up"] + a["bytes_down"], reverse=True)

    # contactos / puertos / países desde los flujos activos del host
    peers: dict[str, dict] = {}
    ports: dict[int, dict] = {}
    countries: dict[str, int] = {}
    try:
        flows = await nt.active_flows(host=ip)
    except Exception:
        flows = []
    for raw in flows:
        f = nt.flow_row(raw)
        if f["cli_ip"] == ip:
            peer_ip, peer_name = f["srv_ip"], f["srv_name"]
        elif f["srv_ip"] == ip:
            peer_ip, peer_name = f["cli_ip"], f["cli_name"]
        else:
            continue
        if not peer_ip:
            continue
        p = peers.setdefault(peer_ip, {
            "ip": peer_ip, "name": peer_name if peer_name != peer_ip else "",
            "local": settings.is_local_ip(peer_ip), "bytes": 0, "flows": 0,
            "country": "" if settings.is_local_ip(peer_ip)
                       else geo_country(peer_ip, settings.geoip_mmdb),
        })
        p["bytes"] += f["bytes"]
        p["flows"] += 1
        if p["country"]:
            countries[p["country"]] = countries.get(p["country"], 0) + f["bytes"]
        port = f["srv_port"]
        if 0 < port < 49152:  # ignorar efímeros como "puerto del servicio"
            entry = ports.setdefault(port, {"port": port, "l7": f["l7"], "flows": 0, "bytes": 0})
            entry["flows"] += 1
            entry["bytes"] += f["bytes"]

    dev = await pool.fetchrow(
        """SELECT d.mac::text AS mac, d.vendor, d.hostname, d.ad_user,
                  d.first_seen, d.last_seen
           FROM devices d WHERE d.ip = $1::inet""", ip)
    cached = state["hostname_cache"].get(ip, ("", ""))
    return {
        "ip": ip,
        "local": settings.is_local_ip(ip),
        "live": bool(details),
        "source_ok": source_ok,
        "vlans": vlans,
        "hostname": (dev and dev["hostname"]) or cached[0] or "",
        "user": (dev and dev["ad_user"]) or cached[1] or "",
        "mac": (dev and dev["mac"]) or "",
        "vendor": (dev and dev["vendor"]) or "",
        "os": _host_extra(details[0]).get("os", "") if details else "",
        "first_seen": dev and dev["first_seen"],
        "last_seen": dev and dev["last_seen"],
        "bytes_sent": bytes_sent,
        "bytes_rcvd": bytes_rcvd,
        "apps": apps_list[:20],
        "peers": sorted(peers.values(), key=lambda p: p["bytes"], reverse=True)[:20],
        "ports": sorted(ports.values(), key=lambda p: p["bytes"], reverse=True)[:10],
        "countries": sorted(
            ({"country": c, "bytes": b} for c, b in countries.items()),
            key=lambda x: x["bytes"], reverse=True)[:10],
    }


HOST_USAGE_RANGES = {"1h": "1 hour", "24h": "24 hours", "7d": "7 days", "30d": "30 days"}

# Combina la tabla horaria (hasta 'cut') con la de minutos (desde 'cut'), igual
# que los reportes: cubre la hora en curso sin contar bytes dos veces.
# {cols}, {where} y las tablas salen de constantes del módulo, nunca del cliente;
# $1 es siempre el valor filtrado (IP o app), $2 el intervalo y $3 el corte.
_USAGE_SQL = """
WITH b AS (
    SELECT now() - ($2::text)::interval AS start,
           COALESCE($3::timestamptz, now() - ($2::text)::interval) AS cut
), unified AS (
    SELECT {cols}, bytes_up, bytes_down FROM {hour}, b
    WHERE {where} AND ts >= b.start AND ts < b.cut
    UNION ALL
    SELECT {cols}, bytes_up, bytes_down FROM {minute}, b
    WHERE {where} AND ts >= GREATEST(b.start, b.cut)
)
"""
_HOST_WHERE = "ip = $1::inet"


async def _usage_window(pool: asyncpg.Pool, range: str) -> tuple[str, datetime | None]:
    """(intervalo SQL, corte hora/minuto) para un rango de HOST_USAGE_RANGES.

    1h/24h salen completos de los tiers de minuto (retención 48 h): sin corte.
    7d/30d usan el tier horario hasta 'rollup_until' + minutos desde ahí.
    """
    if range not in HOST_USAGE_RANGES:
        raise HTTPException(400, f"range debe ser uno de {list(HOST_USAGE_RANGES)}")
    cut = None
    if range in ("7d", "30d"):
        raw = await db.meta_get(pool, "rollup_until")
        cut = datetime.fromisoformat(raw) if raw else None
    return HOST_USAGE_RANGES[range], cut


async def _apps_since(pool: asyncpg.Pool) -> datetime | None:
    return await pool.fetchval(
        """SELECT LEAST((SELECT min(ts) FROM traffic_app_host_hour),
                        (SELECT min(ts) FROM traffic_app_host_min))"""
    )


@app.get("/api/hosts/{ip}/usage")
async def host_usage(ip: str, range: str = "24h", limit: int = 25,
                     role: str = Depends(require_view)):
    """Consumo histórico de un host desde PostgreSQL: total, por categoría y por app.

    No depende de ntopng: responde aunque el host ya no esté activo.
    """
    ip = _valid_ip(ip)
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    limit = max(1, min(limit, 100))
    raw_since = await db.meta_get(pool, "internet_since")
    inet_since = datetime.fromisoformat(raw_since) if raw_since else None

    total = await pool.fetchrow(
        _USAGE_SQL.format(cols="ip, bytes_internet, ts", where=_HOST_WHERE,
                          hour="traffic_hour", minute="traffic_min")
        + """SELECT COALESCE(SUM(bytes_up), 0)::bigint AS bytes_up,
                    COALESCE(SUM(bytes_down), 0)::bigint AS bytes_down,
                    COALESCE(SUM(bytes_internet), 0)::bigint AS bytes_internet,
                    -- tráfico anterior a la clasificación internet / red interna
                    COALESCE(SUM(CASE WHEN ts < COALESCE($4::timestamptz, 'infinity')
                                      THEN bytes_up + bytes_down END), 0)::bigint
                      AS bytes_unclassified
             FROM unified""",
        ip, interval, cut, inet_since,
    )
    cats = await pool.fetch(
        _USAGE_SQL.format(cols="category", where=_HOST_WHERE, hour="traffic_cat_hour",
                          minute="traffic_cat_min")
        + """SELECT category, SUM(bytes_up)::bigint AS bytes_up,
                    SUM(bytes_down)::bigint AS bytes_down
             FROM unified GROUP BY 1 ORDER BY SUM(bytes_up + bytes_down) DESC""",
        ip, interval, cut,
    )
    apps_rows = await pool.fetch(
        _USAGE_SQL.format(cols="app, category", where=_HOST_WHERE,
                          hour="traffic_app_host_hour", minute="traffic_app_host_min")
        + """SELECT app, MAX(category) AS category, SUM(bytes_up)::bigint AS bytes_up,
                    SUM(bytes_down)::bigint AS bytes_down
             FROM unified GROUP BY 1 ORDER BY SUM(bytes_up + bytes_down) DESC
             LIMIT $4""",
        ip, interval, cut, limit,
    )
    # sitios (dominios de internet) desde el registro de flujos: "a dónde va"
    domains = await pool.fetch(
        """WITH u AS (
             SELECT domain, bytes FROM flows_hour
             WHERE local_ip=$1::inet AND scope='internet' AND domain<>''
               AND ts >= now() - ($2::text)::interval
               AND ts < COALESCE($3::timestamptz, now() - ($2::text)::interval)
             UNION ALL
             SELECT domain, bytes FROM flows_min
             WHERE local_ip=$1::inet AND scope='internet' AND domain<>''
               AND ts >= GREATEST(now() - ($2::text)::interval,
                                  COALESCE($3::timestamptz, '-infinity')))
           SELECT domain, SUM(bytes)::bigint AS bytes FROM u
           GROUP BY domain ORDER BY 2 DESC LIMIT $4""",
        ip, interval, cut, limit,
    )
    # comparación: promedio por equipo de la red y puesto del equipo (mismo período)
    cmp_row = await pool.fetchrow(
        _USAGE_SQL.format(cols="ip, bytes_internet", where="true",
                          hour="traffic_hour", minute="traffic_min")
        + """, per AS (SELECT ip, SUM(bytes_up + bytes_down) AS t,
                              SUM(bytes_internet) AS i FROM unified GROUP BY ip)
             SELECT AVG(t)::bigint AS avg_total, AVG(i)::bigint AS avg_internet,
                    COUNT(*) AS hosts,
                    (SELECT COUNT(*) + 1 FROM per WHERE t >
                        COALESCE((SELECT t FROM per WHERE ip = $1::inet), 0)) AS rank
             FROM per""",
        ip, interval, cut,
    )
    apps_since = await _apps_since(pool)
    return {
        "ip": ip,
        "range": range,
        "net_avg_total": cmp_row["avg_total"] or 0,
        "net_avg_internet": cmp_row["avg_internet"] or 0,
        "net_hosts": cmp_row["hosts"],
        "rank": cmp_row["rank"],
        "bytes_up": total["bytes_up"],
        "bytes_down": total["bytes_down"],
        "bytes_internet": total["bytes_internet"],
        "bytes_unclassified": total["bytes_unclassified"],
        "internet_since": raw_since,
        "categories": [dict(r) for r in cats],
        "apps": [dict(r) for r in apps_rows],
        "domains": [{"domain": r["domain"], "bytes": r["bytes"], "name": friendly_site(r["domain"])} for r in domains],
        "apps_since": apps_since,
    }


@app.get("/api/app-hosts")
async def app_hosts(name: str, range: str = "7d", limit: int = 50,
                    role: str = Depends(require_view)):
    """Equipos que generaron el tráfico de una aplicación nDPI en el período.

    Responde "se gastaron N GB en la app X y fueron estos equipos". La app va
    como query param (los nombres nDPI pueden traer caracteres raros para un path).
    """
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    limit = max(1, min(limit, 200))
    rows = await pool.fetch(
        _USAGE_SQL.format(cols="ip", where="app = $1::text",
                          hour="traffic_app_host_hour", minute="traffic_app_host_min")
        + """, per_host AS (
                 SELECT ip, SUM(bytes_up)::bigint AS bytes_up,
                        SUM(bytes_down)::bigint AS bytes_down
                 FROM unified GROUP BY ip
             )
             SELECT host(p.ip) AS ip, COALESCE(h.hostname, '') AS hostname,
                    COALESCE(u.username, '') AS ad_user,
                    COALESCE(d.vendor, '') AS vendor,
                    p.bytes_up, p.bytes_down,
                    SUM(p.bytes_up + p.bytes_down) OVER ()::bigint AS app_total,
                    COUNT(*) OVER () AS hosts_total
             FROM per_host p
             LEFT JOIN hostnames h ON h.ip = p.ip
             LEFT JOIN ip_user u ON u.ip = p.ip
                  AND u.seen_at > now() - ($5::text)::interval
             LEFT JOIN LATERAL (SELECT vendor FROM devices d
                                WHERE d.ip = p.ip ORDER BY last_seen DESC LIMIT 1) d ON true
             ORDER BY p.bytes_up + p.bytes_down DESC
             LIMIT $4""",
        name, interval, cut, limit, f"{settings.user_map_ttl_hours} hours",
    )
    overrides = await _category_overrides()
    total = rows[0]["app_total"] if rows else 0
    return {
        "app": name,
        "category": categorize(name, overrides),
        "range": range,
        "bytes_total": total,
        "hosts_total": rows[0]["hosts_total"] if rows else 0,
        "hosts": [
            {k: r[k] for k in ("ip", "hostname", "ad_user", "vendor",
                               "bytes_up", "bytes_down")}
            for r in rows
        ],
        "apps_since": await _apps_since(pool),
    }


@app.get("/api/category-hosts")
async def category_hosts(name: str, range: str = "7d", limit: int = 50,
                         role: str = Depends(require_view)):
    """Equipos que consumieron una categoría (streaming, social, ...) y con qué apps."""
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    limit = max(1, min(limit, 200))
    rows = await pool.fetch(
        _USAGE_SQL.format(cols="ip", where="category = $1::text",
                          hour="traffic_cat_hour", minute="traffic_cat_min")
        + """, per_host AS (
                 SELECT ip, SUM(bytes_up)::bigint AS bytes_up,
                        SUM(bytes_down)::bigint AS bytes_down
                 FROM unified GROUP BY ip
             )
             SELECT host(p.ip) AS ip, COALESCE(h.hostname, '') AS hostname,
                    COALESCE(u.username, '') AS ad_user,
                    COALESCE(d.vendor, '') AS vendor,
                    p.bytes_up, p.bytes_down,
                    SUM(p.bytes_up + p.bytes_down) OVER ()::bigint AS cat_total,
                    COUNT(*) OVER () AS hosts_total
             FROM per_host p
             LEFT JOIN hostnames h ON h.ip = p.ip
             LEFT JOIN ip_user u ON u.ip = p.ip
                  AND u.seen_at > now() - ($5::text)::interval
             LEFT JOIN LATERAL (SELECT vendor FROM devices d
                                WHERE d.ip = p.ip ORDER BY last_seen DESC LIMIT 1) d ON true
             ORDER BY p.bytes_up + p.bytes_down DESC
             LIMIT $4""",
        name, interval, cut, limit, f"{settings.user_map_ttl_hours} hours",
    )
    # apps de la categoría, por equipo (la categoría quedó congelada al escribir)
    pairs = await pool.fetch(
        _USAGE_SQL.format(cols="ip, app", where="category = $1::text",
                          hour="traffic_app_host_hour", minute="traffic_app_host_min")
        + """SELECT host(ip) AS ip, app, SUM(bytes_up + bytes_down)::bigint AS total
             FROM unified GROUP BY ip, app""",
        name, interval, cut,
    )
    host_apps: dict[str, list[dict]] = {}
    apps: dict[str, dict] = {}
    for r in pairs:
        host_apps.setdefault(r["ip"], []).append({"app": r["app"], "total": r["total"]})
        a = apps.setdefault(r["app"], {"app": r["app"], "total": 0, "hosts": 0})
        a["total"] += r["total"]
        a["hosts"] += 1
    for lst in host_apps.values():
        lst.sort(key=lambda x: x["total"], reverse=True)
    return {
        "category": name,
        "range": range,
        "bytes_total": rows[0]["cat_total"] if rows else 0,
        "hosts_total": rows[0]["hosts_total"] if rows else 0,
        "hosts": [
            {**{k: r[k] for k in ("ip", "hostname", "ad_user", "vendor",
                                  "bytes_up", "bytes_down")},
             "apps": host_apps.get(r["ip"], [])[:3]}
            for r in rows
        ],
        "apps": sorted(apps.values(), key=lambda a: a["total"], reverse=True)[:30],
        "apps_since": await _apps_since(pool),
    }


# --- Registro de conexiones (flujos persistidos) --------------------------------
# Une el tier de minuto (48 h) con el horario (30 d) por el corte 'rollup_until',
# igual que _USAGE_SQL. {where} y las columnas salen de constantes del módulo.
_FLOWS_SQL = """
WITH b AS (
    SELECT now() - ($2::text)::interval AS start,
           COALESCE($3::timestamptz, now() - ($2::text)::interval) AS cut
), unified AS (
    SELECT ts, local_ip, remote_ip, srv_port, l7, scope, direction, domain, bytes
    FROM flows_hour, b WHERE {where} AND ts >= b.start AND ts < b.cut
    UNION ALL
    SELECT ts, local_ip, remote_ip, srv_port, l7, scope, direction, domain, bytes
    FROM flows_min, b WHERE {where} AND ts >= GREATEST(b.start, b.cut)
)
"""


@app.get("/api/domains-top")
async def domains_top(range: str = "24h", limit: int = 100, role: str = Depends(require_view)):
    """Dominios de internet (SNI) que más tráfico concentran en toda la red."""
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    limit = max(1, min(limit, 500))
    rows = await pool.fetch(
        """WITH b AS (SELECT now() - ($1::text)::interval AS start,
                            COALESCE($2::timestamptz, now() - ($1::text)::interval) AS cut),
           unified AS (
               SELECT domain, bytes FROM domain_hour, b WHERE ts >= b.start AND ts < b.cut
               UNION ALL
               SELECT domain, bytes FROM domain_min, b WHERE ts >= GREATEST(b.start, b.cut))
           SELECT domain, SUM(bytes)::bigint AS bytes FROM unified
           GROUP BY domain ORDER BY SUM(bytes) DESC LIMIT $3""",
        interval, cut, limit,
    )
    since = await pool.fetchval(
        "SELECT LEAST((SELECT min(ts) FROM domain_hour),(SELECT min(ts) FROM domain_min))")
    return {"range": range, "since": since, "domains": [{"domain": r["domain"], "bytes": r["bytes"], "name": friendly_site(r["domain"])} for r in rows]}


@app.get("/api/hosts/{ip}/flows")
async def host_flows(ip: str, range: str = "24h", limit: int = 100,
                     role: str = Depends(require_view)):
    """Con quién habló un equipo: conexiones registradas, agrupadas por destino."""
    ip = _valid_ip(ip)
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    limit = max(1, min(limit, 500))
    rows = await pool.fetch(
        _FLOWS_SQL.format(where="local_ip = $1::inet")
        + """SELECT host(remote_ip) AS remote_ip, srv_port, l7,
                    MAX(scope) AS scope, MAX(direction) AS direction,
                    MAX(domain) AS domain, SUM(bytes)::bigint AS bytes
             FROM unified GROUP BY remote_ip, srv_port, l7
             ORDER BY SUM(bytes) DESC LIMIT $4""",
        ip, interval, cut, limit,
    )
    tot = await pool.fetchrow(
        _FLOWS_SQL.format(where="local_ip = $1::inet")
        + """SELECT COALESCE(SUM(bytes),0)::bigint AS total,
                    COALESCE(SUM(bytes) FILTER (WHERE scope='internet'),0)::bigint AS internet,
                    COUNT(DISTINCT remote_ip) AS remotos FROM unified""",
        ip, interval, cut,
    )
    # nombre del remoto si es un host conocido de la red
    names = {}
    if rows:
        for r in await pool.fetch(
            "SELECT host(ip) AS ip, hostname FROM hostnames WHERE ip = ANY($1::inet[])",
            [r["remote_ip"] for r in rows]):
            names[r["ip"]] = r["hostname"]
    return {"ip": ip, "range": range,
            "bytes_total": tot["total"], "bytes_internet": tot["internet"],
            "remotos": tot["remotos"],
            "flows": [dict(r, remote_name=names.get(r["remote_ip"], ""),
                           site=friendly_site(r["domain"]) if r["domain"] else "") for r in rows]}


@app.get("/api/flows-top")
async def flows_top(range: str = "24h", scope: str = "", limit: int = 100,
                    role: str = Depends(require_view)):
    """Top de conexiones de toda la red. scope vacío = todas; internet | interno."""
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    limit = max(1, min(limit, 500))
    if scope not in ("", "internet", "interno"):
        raise HTTPException(400, "scope debe ser internet, interno o vacío")
    scope_sql = "AND scope = $3::text" if scope else ""
    limit_n = "$4" if scope else "$3"
    # $1 intervalo, $2 corte, $3 scope (si hay), luego el límite
    sql = """
        WITH b AS (SELECT now() - ($1::text)::interval AS start,
                          COALESCE($2::timestamptz, now() - ($1::text)::interval) AS cut),
        unified AS (
            SELECT local_ip, remote_ip, srv_port, l7, scope, domain, bytes
            FROM flows_hour, b WHERE ts >= b.start AND ts < b.cut {sc}
            UNION ALL
            SELECT local_ip, remote_ip, srv_port, l7, scope, domain, bytes
            FROM flows_min, b WHERE ts >= GREATEST(b.start, b.cut) {sc}
        )
        SELECT host(local_ip) AS local_ip, host(remote_ip) AS remote_ip, srv_port, l7,
               MAX(scope) AS scope, MAX(domain) AS domain, SUM(bytes)::bigint AS bytes
        FROM unified GROUP BY local_ip, remote_ip, srv_port, l7
        ORDER BY SUM(bytes) DESC LIMIT {lim}""".format(sc=scope_sql, lim=limit_n)
    if scope:
        rows = await pool.fetch(sql, interval, cut, scope, limit)
    else:
        rows = await pool.fetch(sql, interval, cut, limit)
    ips = {r["local_ip"] for r in rows} | {r["remote_ip"] for r in rows}
    names = {}
    if ips:
        for r in await pool.fetch(
            "SELECT host(ip) AS ip, hostname FROM hostnames WHERE ip = ANY($1::inet[])",
            list(ips)):
            names[r["ip"]] = r["hostname"]
    return {"range": range, "scope": scope,
            "flows": [dict(r, local_name=names.get(r["local_ip"], ""),
                           remote_name=names.get(r["remote_ip"], ""),
                           site=friendly_site(r["domain"]) if r["domain"] else "")
                      for r in rows]}


@app.get("/api/flows")
async def flows_list(host: str = "", l7: str = "", port: int = 0,
                     sort: str = "bytes", limit: int = 100,
                     role: str = Depends(require_view)):
    """Tabla en vivo de flujos activos (proxy de ntopng, no se persiste)."""
    nt: NtopngClient = state["nt"]
    try:
        raw_flows = await nt.active_flows(host=host)
    except Exception:
        raise HTTPException(503, "ntopng no responde")
    if not state["hostname_cache"]:
        await refresh_hostname_cache()

    out = []
    for raw in raw_flows:
        f = nt.flow_row(raw)
        if l7 and l7.lower() not in f["l7"].lower():
            continue
        if port and port not in (f["cli_port"], f["srv_port"]):
            continue
        f["cli_local"] = settings.is_local_ip(f["cli_ip"])
        f["srv_local"] = settings.is_local_ip(f["srv_ip"])
        f["srv_country"] = "" if f["srv_local"] else geo_country(f["srv_ip"], settings.geoip_mmdb)
        cached = state["hostname_cache"].get(f["cli_ip"])
        if cached and not f["cli_name"]:
            f["cli_name"] = cached[0]
        out.append(f)

    key = sort if sort in ("bytes", "thpt_bps", "duration_s") else "bytes"
    out.sort(key=lambda f: f[key], reverse=True)
    return out[: min(limit, 300)]


# ---------------------------------------------------------------------------
# Navegación: buscador, tarjeta flotante, IP externa, sitio, usuarios, resumen
# ---------------------------------------------------------------------------
# medianoche de hoy en hora argentina (el server corre en US/Eastern)
_TODAY = db.day_start_sql()
# tamaño de bucket de las series de flujos según el rango
_FLOW_BUCKETS = {"1h": 300, "24h": 3600, "7d": 3 * 3600, "30d": 86400}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _names_users(pool: asyncpg.Pool, ips: list[str]) -> dict[str, dict]:
    """ip -> {hostname, user, vendor} para decorar listas (una sola consulta)."""
    if not ips:
        return {}
    rows = await pool.fetch(
        """SELECT host(x.ip) AS ip, COALESCE(h.hostname, d.hostname, '') AS hostname,
                  COALESCE(u.username, '') AS user,
                  COALESCE(d.vendor, '') AS vendor
           FROM unnest($1::inet[]) AS x(ip)
           LEFT JOIN hostnames h ON h.ip = x.ip
           LEFT JOIN ip_user u ON u.ip = x.ip AND u.seen_at > now() - ($2::text)::interval
           LEFT JOIN LATERAL (SELECT hostname, ad_user, vendor FROM devices d
                              WHERE d.ip = x.ip ORDER BY last_seen DESC LIMIT 1) d ON true""",
        ips, f"{settings.user_map_ttl_hours} hours",
    )
    return {r["ip"]: dict(r) for r in rows}


@app.get("/api/search")
async def search(q: str = "", role: str = Depends(require_view)):
    """Buscador global: equipos (IP, nombre, usuario, MAC, fabricante), usuarios,
    aplicaciones, sitios y, si lo escrito es una IP, la IP misma."""
    q = q.strip()[:64]
    if len(q) < 2:
        return {"results": []}
    pool: asyncpg.Pool = state["pool"]
    like = _like(q)
    hexq = re.sub(r"[^0-9a-fA-F]", "", q)
    mac_like = _like(hexq.lower()) if len(hexq) >= 4 and re.fullmatch(r"[0-9a-fA-F:.\-]+", q) else ""
    out: list[dict] = []
    try:
        ipaddress.ip_address(q)
        out.append({"type": "ip", "ip": q, "local": settings.is_local_ip(q)})
    except ValueError:
        pass
    hosts = await pool.fetch(
        """SELECT * FROM (
             SELECT DISTINCT ON (d.ip) host(d.ip) AS ip,
                    COALESCE(h.hostname, d.hostname, '') AS hostname,
                    COALESCE(u.username, '') AS user,
                    d.mac::text AS mac, COALESCE(d.vendor, '') AS vendor, d.last_seen
             FROM devices d
             LEFT JOIN hostnames h ON h.ip = d.ip
             LEFT JOIN ip_user u ON u.ip = d.ip AND u.seen_at > now() - ($3::text)::interval
             WHERE d.ip IS NOT NULL AND (
                   host(d.ip) LIKE $1 OR h.hostname ILIKE $1 OR d.hostname ILIKE $1
                   OR u.username ILIKE $1 OR d.vendor ILIKE $1
                   OR ($2 <> '' AND replace(d.mac::text, ':', '') LIKE $2))
             ORDER BY d.ip, d.last_seen DESC) x
           ORDER BY last_seen DESC LIMIT 8""",
        like, mac_like, f"{settings.user_map_ttl_hours} hours",
    )
    out += [{"type": "host", **{k: r[k] for k in ("ip", "hostname", "user", "mac", "vendor")}}
            for r in hosts]
    users = await pool.fetch(
        """SELECT username, COUNT(*) AS ips FROM ip_user
           WHERE username ILIKE $1 GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5""", like)
    out += [{"type": "user", "user": r["username"], "ips": r["ips"]} for r in users]
    apps_rows = await pool.fetch(
        """SELECT app, MAX(category) AS category FROM app_hour
           WHERE ts > now() - interval '30 days' AND app ILIKE $1
           GROUP BY app ORDER BY SUM(bytes_up + bytes_down) DESC LIMIT 5""", like)
    out += [{"type": "app", "app": r["app"], "category": r["category"]} for r in apps_rows]
    ql = q.lower()
    sites = sorted({n for n in SITE_NAMES.values() if ql in n.lower()})
    for r in await pool.fetch(
            """SELECT domain FROM domain_hour WHERE ts > now() - interval '7 days'
                 AND domain ILIKE $1 GROUP BY domain ORDER BY SUM(bytes) DESC LIMIT 10""", like):
        n = friendly_site(r["domain"])
        if n not in sites:
            sites.append(n)
    out += [{"type": "site", "site": n} for n in sites[:6]]
    return {"results": out}


@app.get("/api/ipcard/{ip}")
async def ip_card(ip: str, role: str = Depends(require_view)):
    """Datos cortos para la tarjeta al pasar el mouse sobre una IP."""
    ip = _valid_ip(ip)
    pool: asyncpg.Pool = state["pool"]
    if settings.is_local_ip(ip):
        info = (await _names_users(pool, [ip])).get(ip, {})
        dev = await pool.fetchrow(
            """SELECT mac::text AS mac, last_seen FROM devices WHERE ip = $1::inet
               ORDER BY last_seen DESC LIMIT 1""", ip)
        today = await pool.fetchrow(
            f"""SELECT COALESCE(SUM(bytes_up + bytes_down), 0)::bigint AS total,
                       COALESCE(SUM(bytes_internet), 0)::bigint AS internet
                FROM traffic_min WHERE ip = $1::inet AND ts >= {_TODAY}""", ip)
        return {"ip": ip, "local": True, **info,
                "mac": dev["mac"] if dev else "", "last_seen": dev and dev["last_seen"],
                "today_total": today["total"], "today_internet": today["internet"]}
    row = await pool.fetchrow(
        """SELECT COALESCE(MAX(domain) FILTER (WHERE domain <> ''), '') AS domain,
                  COUNT(DISTINCT local_ip) AS hosts, COALESCE(SUM(bytes), 0)::bigint AS bytes
           FROM flows_min WHERE remote_ip = $1::inet AND ts > now() - interval '24 hours'""", ip)
    return {"ip": ip, "local": False,
            "country": geo_country(ip, settings.geoip_mmdb),
            "domain": row["domain"], "site": friendly_site(row["domain"]) if row["domain"] else "",
            "hosts_24h": row["hosts"], "bytes_24h": row["bytes"]}


@app.get("/api/ip/{ip}")
async def ip_detail(ip: str, range: str = "24h", role: str = Depends(require_view)):
    """Una IP vista como destino: qué equipos de la red hablaron con ella,
    por qué servicios, con qué dominio, cuánto y cuándo."""
    ip = _valid_ip(ip)
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    bucket = _FLOW_BUCKETS[range]
    where = "remote_ip = $1::inet"
    per_host = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + """SELECT host(local_ip) AS ip, SUM(bytes)::bigint AS bytes,
                    MIN(ts) AS first, MAX(ts) AS last,
                    string_agg(DISTINCT srv_port::text || '/' || l7, ', ') AS services
             FROM unified GROUP BY local_ip ORDER BY 2 DESC LIMIT 200""",
        ip, interval, cut)
    services = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + """SELECT srv_port, l7, MAX(direction) AS direction, SUM(bytes)::bigint AS bytes
             FROM unified GROUP BY srv_port, l7 ORDER BY 4 DESC LIMIT 20""",
        ip, interval, cut)
    doms = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + """SELECT domain, SUM(bytes)::bigint AS bytes FROM unified
             WHERE domain <> '' GROUP BY domain ORDER BY 2 DESC LIMIT 10""",
        ip, interval, cut)
    series = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + f"""SELECT to_timestamp(floor(extract(epoch FROM ts) / {bucket}) * {bucket}) AS t,
                     SUM(bytes)::bigint AS bytes FROM unified GROUP BY 1 ORDER BY 1""",
        ip, interval, cut)
    names = await _names_users(pool, [r["ip"] for r in per_host])
    hn = await pool.fetchval("SELECT hostname FROM hostnames WHERE ip = $1::inet", ip)
    return {
        "ip": ip, "range": range, "bucket_s": bucket,
        "local": settings.is_local_ip(ip), "hostname": hn or "",
        "country": "" if settings.is_local_ip(ip) else geo_country(ip, settings.geoip_mmdb),
        "bytes_total": sum(r["bytes"] for r in per_host),
        "first": min((r["first"] for r in per_host), default=None),
        "last": max((r["last"] for r in per_host), default=None),
        "hosts": [{**dict(r), **names.get(r["ip"], {})} for r in per_host],
        "services": [dict(r) for r in services],
        "domains": [{"domain": r["domain"], "site": friendly_site(r["domain"]),
                     "bytes": r["bytes"]} for r in doms],
        "series": [dict(r) for r in series],
    }


@app.get("/api/site-hosts")
async def site_hosts(name: str, range: str = "24h", role: str = Depends(require_view)):
    """Un sitio (nombre amigable, p. ej. 'Prime Video'): qué equipos entraron,
    cuánto consumió cada uno y en qué horario."""
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    bucket = _FLOW_BUCKETS[range]
    doms = site_domains(name)
    where = "domain = ANY($1::text[]) AND scope = 'internet'"
    per_host = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + """SELECT host(local_ip) AS ip, SUM(bytes)::bigint AS bytes,
                    MIN(ts) AS first, MAX(ts) AS last
             FROM unified GROUP BY local_ip ORDER BY 2 DESC LIMIT 200""",
        doms, interval, cut)
    by_domain = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + """SELECT domain, SUM(bytes)::bigint AS bytes FROM unified
             GROUP BY domain ORDER BY 2 DESC""",
        doms, interval, cut)
    series = await pool.fetch(
        _FLOWS_SQL.format(where=where)
        + f"""SELECT to_timestamp(floor(extract(epoch FROM ts) / {bucket}) * {bucket}) AS t,
                     SUM(bytes)::bigint AS bytes FROM unified GROUP BY 1 ORDER BY 1""",
        doms, interval, cut)
    names = await _names_users(pool, [r["ip"] for r in per_host])
    return {
        "site": name, "range": range, "bucket_s": bucket,
        "bytes_total": sum(r["bytes"] for r in per_host),
        "hosts_total": len(per_host),
        "hosts": [{**dict(r), **names.get(r["ip"], {})} for r in per_host],
        "domains": [dict(r) for r in by_domain],
        "series": [dict(r) for r in series],
    }


@app.get("/api/sites-top")
async def sites_top(range: str = "24h", limit: int = 50, role: str = Depends(require_view)):
    """Sitios de internet agrupados por nombre amigable (varios dominios -> un sitio),
    con la cantidad de equipos que entraron a cada uno."""
    pool: asyncpg.Pool = state["pool"]
    interval, cut = await _usage_window(pool, range)
    rows = await pool.fetch(
        """WITH b AS (SELECT now() - ($1::text)::interval AS start,
                            COALESCE($2::timestamptz, now() - ($1::text)::interval) AS cut),
           unified AS (
               SELECT domain, bytes FROM domain_hour, b WHERE ts >= b.start AND ts < b.cut
               UNION ALL
               SELECT domain, bytes FROM domain_min, b WHERE ts >= GREATEST(b.start, b.cut))
           SELECT domain, SUM(bytes)::bigint AS bytes FROM unified
           GROUP BY domain ORDER BY 2 DESC LIMIT 400""",
        interval, cut)
    agg: dict[str, dict] = {}
    for r in rows:
        n = friendly_site(r["domain"])
        a = agg.setdefault(n, {"site": n, "bytes": 0, "domains": []})
        a["bytes"] += r["bytes"]
        a["domains"].append(r["domain"])
    top = sorted(agg.values(), key=lambda a: a["bytes"], reverse=True)[:max(1, min(limit, 100))]
    # equipos por sitio (desde el registro de flujos)
    all_doms = [d for a in top for d in a["domains"]]
    pairs = await pool.fetch(
        _FLOWS_SQL.format(where="domain = ANY($1::text[]) AND scope = 'internet'")
        + "SELECT DISTINCT domain, local_ip FROM unified",
        all_doms, interval, cut) if all_doms else []
    hosts_by_dom: dict[str, set] = {}
    for p in pairs:
        hosts_by_dom.setdefault(p["domain"], set()).add(p["local_ip"])
    for a in top:
        a["hosts"] = len(set().union(*(hosts_by_dom.get(d, set()) for d in a["domains"])))
    # el registro de sitios es una muestra (flujos principales con nombre SNI):
    # qué parte del tráfico de internet del período cubre (auditoría H22)
    # Se mide sólo sobre el tramo en que hay registro de sitios (tier de minutos):
    # comparar contra todo el rango subestima si el registro es más corto.
    cov = await pool.fetchrow(
        """WITH s AS (SELECT GREATEST(now() - ($1::text)::interval,
                                     (SELECT min(ts) FROM domain_min)) AS start)
           SELECT (SELECT COALESCE(SUM(bytes), 0) FROM domain_min, s WHERE ts >= s.start)::bigint AS named,
                  (SELECT COALESCE(SUM(bytes_internet), 0) FROM traffic_min, s
                   WHERE ts >= s.start)::bigint AS inet,
                  (SELECT start FROM s) AS since""", interval)
    inet, named = cov["inet"], cov["named"]
    coverage = round(min(100.0, 100.0 * named / inet), 1) if inet else 0.0
    # de dónde salió el nombre: SNI (lo dice la conexión) o mapa DNS pasivo
    src = await pool.fetchrow(
        """SELECT COALESCE(SUM(bytes) FILTER (WHERE domain_src = 'dns'), 0)::bigint AS dns,
                  COALESCE(SUM(bytes) FILTER (WHERE domain <> '' AND domain_src <> 'dns'), 0)::bigint AS sni
           FROM flows_min WHERE scope = 'internet' AND ts >= $1""", cov["since"])
    pct = lambda b: round(min(100.0, 100.0 * b / inet), 1) if inet else 0.0  # noqa: E731
    return {"range": range, "sites": top, "coverage_pct": coverage,
            "coverage_sni_pct": pct(src["sni"]), "coverage_dns_pct": pct(src["dns"]),
            "internet_bytes": inet, "named_bytes": named, "coverage_since": cov["since"]}


_RANGE_DELTA = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1), "24h": timedelta(hours=24),
                "7d": timedelta(days=7), "30d": timedelta(days=30)}


async def _equipos_por_ip(pool: asyncpg.Pool, ips: list[str], since: datetime) -> dict[str, int]:
    """IP -> cantidad de MAC distintas que la usaron desde `since` (historial H04)."""
    if not ips:
        return {}
    rows = await pool.fetch(
        """SELECT host(ip) AS ip, count(DISTINCT mac) AS n FROM ip_assignments
           WHERE ip = ANY($1::inet[]) AND last_seen >= $2 GROUP BY ip""", ips, since)
    return {r["ip"]: r["n"] for r in rows}


async def _names_in_window(pool: asyncpg.Pool, ips: list[str], start: datetime,
                           end: datetime) -> dict[str, str]:
    """IP -> nombre(s) de los equipos que la tuvieron en [start, end); si no hay
    historial para el período, el nombre actual marcado '(actual)'."""
    if not ips:
        return {}
    rows = await pool.fetch(
        """SELECT host(x.ip) AS ip,
                  COALESCE((SELECT string_agg(DISTINCT COALESCE(NULLIF(a.hostname, ''), a.mac::text), ' / ')
                            FROM ip_assignments a
                            WHERE a.ip = x.ip AND a.first_seen < $3 AND a.last_seen >= $2),
                           NULLIF(h.hostname, '') || ' (actual)', '') AS name
           FROM unnest($1::inet[]) AS x(ip) LEFT JOIN hostnames h ON h.ip = x.ip""",
        ips, start, end)
    return {r["ip"]: r["name"] for r in rows}


async def _user_window(range: str) -> tuple[datetime, datetime, datetime | None, str]:
    """(inicio, fin, corte hora/minuto, TTL) para la atribución por sesiones."""
    _, cut = await _usage_window(state["pool"], range)
    end = datetime.now(timezone.utc)
    return end - _RANGE_DELTA[range], end, cut, f"{settings.user_map_ttl_hours} hours"


@app.get("/api/users-usage")
async def users_usage(range: str = "24h", role: str = Depends(require_view)):
    """Usuarios de AD con sus equipos y consumo del período. Cada minuto/hora de
    tráfico se atribuye al usuario que tenía la IP EN ESE MOMENTO (sesiones
    Kerberos + historial IP -> equipo; auditoría H04)."""
    pool: asyncpg.Pool = state["pool"]
    start, end, cut, ttl = await _user_window(range)
    rows = await attribution.usage_by_user(pool, start, end, cut, ttl)
    names = await _names_in_window(pool, sorted({r["ip"] for r in rows}), start, end)
    users: dict[str, dict] = {}
    for r in rows:
        u = users.setdefault(r["username"].lower(), {
            "user": r["username"], "ips": [], "hostnames": [], "total": 0, "internet": 0,
            "seen": None})
        u["ips"].append(r["ip"])
        u["hostnames"].append(names.get(r["ip"], ""))
        u["total"] += r["total"] or 0
        u["internet"] += r["internet"] or 0
        u["seen"] = max(filter(None, [u["seen"], r["last"]]), default=None)
    return {"range": range, "history_since": await attribution.history_since(pool),
            "users": sorted(users.values(), key=lambda u: u["internet"], reverse=True)}


@app.get("/api/user/{name}")
async def user_detail(name: str, range: str = "24h", role: str = Depends(require_view)):
    """Un usuario de AD: equipos que usó en el período, consumo de cada uno y a
    qué sitios fue, atribuido por sesiones (auditoría H04)."""
    pool: asyncpg.Pool = state["pool"]
    start, end, cut, ttl = await _user_window(range)
    rows = await attribution.usage_by_user(pool, start, end, cut, ttl, name)
    ips = sorted({r["ip"] for r in rows})
    names = await _names_in_window(pool, ips, start, end)
    macs = {r["ip"]: r["mac"] for r in await pool.fetch(
        """SELECT DISTINCT ON (ip) host(ip) AS ip, mac::text AS mac FROM ip_assignments
           WHERE ip = ANY($1::inet[]) AND first_seen < $3 AND last_seen >= $2
           ORDER BY ip, last_seen DESC""", ips, start, end)} if ips else {}
    sites: dict[str, int] = {}
    for r in await attribution.sites_by_user(pool, start, end, cut, ttl, name):
        n = friendly_site(r["domain"])
        sites[n] = sites.get(n, 0) + r["bytes"]
    devices_out = sorted(({
        "ip": r["ip"], "hostname": names.get(r["ip"], ""), "mac": macs.get(r["ip"], ""),
        "last_seen": r["last"], "total": r["total"] or 0, "internet": r["internet"] or 0,
    } for r in rows), key=lambda d: d["total"], reverse=True)
    return {
        "user": rows[0]["username"] if rows else name, "range": range,
        "history_since": await attribution.history_since(pool),
        "devices": devices_out,
        "total": sum(d["total"] for d in devices_out),
        "internet": sum(d["internet"] for d in devices_out),
        "sites": [{"site": k, "bytes": v} for k, v in
                  sorted(sites.items(), key=lambda kv: kv[1], reverse=True)[:25]],
    }


@app.get("/api/hosts/{ip}/assignments")
async def host_assignments(ip: str, range: str = "24h", role: str = Depends(require_view)):
    """Equipos (MAC) que usaron esta IP en el período, con el consumo de cada tramo.
    El consumo por tramo sale del tier de minutos (últimos días)."""
    ip = _valid_ip(ip)
    if range not in _RANGE_DELTA:
        raise HTTPException(400, f"range debe ser uno de {list(_RANGE_DELTA)}")
    pool: asyncpg.Pool = state["pool"]
    end = datetime.now(timezone.utc)
    start = end - _RANGE_DELTA[range]
    return {"ip": ip, "range": range,
            "history_since": await attribution.history_since(pool),
            "segments": await attribution.assignments(pool, ip, start, end)}


@app.get("/api/hosts/{ip}/heatmap")
async def host_heatmap(ip: str, role: str = Depends(require_view)):
    """Tráfico por hora de los últimos 7 días (el navegador arma la grilla 7x24
    en su hora local)."""
    ip = _valid_ip(ip)
    pool: asyncpg.Pool = state["pool"]
    _, cut = await _usage_window(pool, "7d")
    rows = await pool.fetch(
        _USAGE_SQL.format(cols="ts, bytes_internet", where=_HOST_WHERE,
                          hour="traffic_hour", minute="traffic_min")
        + """SELECT date_trunc('hour', ts) AS t,
                    SUM(bytes_up + bytes_down)::bigint AS total,
                    SUM(bytes_internet)::bigint AS internet
             FROM unified GROUP BY 1 ORDER BY 1""",
        ip, "7 days", cut)
    return {"ip": ip, "hours": [dict(r) for r in rows]}


@app.get("/api/overview")
async def overview(role: str = Depends(require_view)):
    """Datos del Resumen: hoy vs ayer a la misma hora, top equipos, sitios y
    usuarios de hoy (hora argentina)."""
    pool: asyncpg.Pool = state["pool"]
    ttl = f"{settings.user_map_ttl_hours} hours"
    cmp = await pool.fetchrow(
        f"""SELECT
              COALESCE(SUM(bytes_up + bytes_down) FILTER (WHERE ts >= {_TODAY}), 0)::bigint AS t_today,
              COALESCE(SUM(bytes_internet) FILTER (WHERE ts >= {_TODAY}), 0)::bigint AS i_today,
              COUNT(DISTINCT ip) FILTER (WHERE ts >= {_TODAY}) AS h_today,
              COALESCE(SUM(bytes_up + bytes_down) FILTER (WHERE ts < now() - interval '1 day'), 0)::bigint AS t_yday,
              COALESCE(SUM(bytes_internet) FILTER (WHERE ts < now() - interval '1 day'), 0)::bigint AS i_yday,
              COUNT(DISTINCT ip) FILTER (WHERE ts < now() - interval '1 day') AS h_yday
            FROM traffic_min
            WHERE ts >= {_TODAY} - interval '1 day'
              AND (ts >= {_TODAY} OR ts < now() - interval '1 day')""")
    alerts_cmp = await pool.fetchrow(
        f"""SELECT COUNT(*) FILTER (WHERE ts >= {_TODAY}) AS today,
                   COUNT(*) FILTER (WHERE ts >= {_TODAY} - interval '1 day'
                                    AND ts < now() - interval '1 day') AS yday
            FROM alerts WHERE ts >= {_TODAY} - interval '1 day'""")
    top_hosts = await pool.fetch(
        f"""SELECT host(ip) AS ip, SUM(bytes_internet)::bigint AS internet,
                   SUM(bytes_up + bytes_down)::bigint AS total
            FROM traffic_min WHERE ts >= {_TODAY}
            GROUP BY ip ORDER BY 2 DESC LIMIT 8""")
    names = await _names_users(pool, [r["ip"] for r in top_hosts])
    doms = await pool.fetch(
        f"""SELECT domain, SUM(bytes)::bigint AS bytes FROM domain_min
            WHERE ts >= {_TODAY} GROUP BY domain ORDER BY 2 DESC LIMIT 200""")
    sites: dict[str, int] = {}
    for r in doms:
        n = friendly_site(r["domain"])
        sites[n] = sites.get(n, 0) + r["bytes"]
    # usuarios de hoy atribuidos por sesión (quién tenía cada IP en cada minuto)
    now = datetime.now(timezone.utc)
    midnight = datetime.now(settings.zone()).replace(hour=0, minute=0, second=0, microsecond=0)
    per_user: dict[str, dict] = {}
    for r in await attribution.usage_by_user(pool, midnight, now, None, ttl):
        u = per_user.setdefault(r["username"].lower(), {"user": r["username"], "ips": 0,
                                                        "internet": 0, "total": 0})
        u["ips"] += 1
        u["internet"] += r["internet"] or 0
        u["total"] += r["total"] or 0
    users = sorted(per_user.values(), key=lambda u: u["internet"], reverse=True)[:8]
    eq = await _equipos_por_ip(pool, [r["ip"] for r in top_hosts], midnight)
    return {
        "today": {"total": cmp["t_today"], "internet": cmp["i_today"],
                  "hosts": cmp["h_today"], "alerts": alerts_cmp["today"]},
        "yesterday": {"total": cmp["t_yday"], "internet": cmp["i_yday"],
                      "hosts": cmp["h_yday"], "alerts": alerts_cmp["yday"]},
        "top_hosts": [{**dict(r), **names.get(r["ip"], {}), "equipos": eq.get(r["ip"], 0)}
                      for r in top_hosts],
        "top_sites": [{"site": k, "bytes": v} for k, v in
                      sorted(sites.items(), key=lambda kv: kv[1], reverse=True)[:8]],
        "top_users": users,
    }


@app.get("/api/access-log")
async def access_log(limit: int = 300, role: str = Depends(require_admin)):
    """Últimas consultas de datos personales (quién, desde dónde, qué)."""
    rows = await state["pool"].fetch(
        """SELECT ts, username AS user, role, client_ip, method, path, query, status
           FROM access_log ORDER BY ts DESC LIMIT $1""", max(1, min(limit, 2000)))
    return [dict(r) for r in rows]


@app.get("/api/interface/realtime")
async def interface_realtime(role: str = Depends(require_view)):
    """Buffer del gráfico realtime (muestras de ~2 s, últimos ~30 min)."""
    return list(state["rt_buf"])


@app.get("/api/devices")
async def devices(only_new: bool = False, role: str = Depends(require_view)):
    pool: asyncpg.Pool = state["pool"]
    where = "WHERE NOT is_known" if only_new else ""
    rows = await pool.fetch(
        f"""SELECT mac::text, host(ip) AS ip, hostname, vendor, ad_user,
                   first_seen, last_seen, is_known
            FROM devices {where} ORDER BY first_seen DESC LIMIT 500"""
    )
    return [dict(r) for r in rows]


@app.post("/api/devices/{mac}/ack")
async def ack_device(mac: str, role: str = Depends(require_admin)):
    result = await state["pool"].execute(
        "UPDATE devices SET is_known = true WHERE mac = $1::macaddr", mac
    )
    return {"ok": result.endswith("1")}


@app.get("/api/alerts")
async def alerts(limit: int = 100, kind: str = "", only_open: bool = False,
                 ip: str = "", role: str = Depends(require_view)):
    where, args = [], []
    if kind:
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    if ip:
        args.append(_valid_ip(ip))
        n = len(args)
        where.append(f"(meta->>'ip' IN (${n}, ${n} || '/32') OR meta->>'remote' = ${n})")
    if only_open:
        where.append("NOT acked")
    args.append(min(limit, 500))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await state["pool"].fetch(
        f"""SELECT id, ts, kind, severity, message, meta, acked FROM alerts
            {where_sql} ORDER BY ts DESC LIMIT ${len(args)}""", *args,
    )
    return [dict(r) for r in rows]


@app.post("/api/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int, role: str = Depends(require_admin)):
    await state["pool"].execute("UPDATE alerts SET acked = true WHERE id = $1", alert_id)
    return {"ok": True}


@app.get("/api/report")
async def report(range: str = "day", ref: str = "", format: str = "csv",
                 role: str = Depends(require_user)):
    """Descarga de reporte. range=day|week|month, ref=fecha AAAA-MM-DD (default: ayer)."""
    if range not in ("day", "week", "month"):
        raise HTTPException(400, "range debe ser day, week o month")
    try:
        ref_date = date.fromisoformat(ref) if ref else settings.today()
    except ValueError:
        raise HTTPException(400, "ref inválida, usar AAAA-MM-DD")
    data = await reports.gather_report_data(state["pool"], range, ref_date)
    stamp = ref_date.isoformat()
    if format == "pdf":
        content = reports.build_pdf(data)
        return Response(content, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="netmon_top_{range}_{stamp}.pdf"'})
    content = reports.build_csv(data)
    return Response(content, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="netmon_top_{range}_{stamp}.csv"'})


@app.get("/api/report/overview")
async def report_overview(desde: str = "", hasta: str = "", limit: int = 20,
                          role: str = Depends(require_user)):
    """Datos para la vista de Reportes en pantalla: serie de consumo por día,
    totales, top de equipos y categorías. desde/hasta en AAAA-MM-DD (días
    inclusive, máx. 31); por defecto los últimos 30 días hasta ayer."""
    try:
        hasta_d = date.fromisoformat(hasta) if hasta else settings.today() - timedelta(days=1)
        desde_d = date.fromisoformat(desde) if desde else hasta_d - timedelta(days=29)
        start, end, label = reports.period_bounds_custom(desde_d, hasta_d)
    except ValueError as exc:
        raise HTTPException(400, f"fechas inválidas: {exc}")
    limit = max(1, min(limit, 100))
    data = await reports.gather_overview(state["pool"], start, end, limit)
    data["label"] = label
    data["desde"] = desde_d.isoformat()
    data["hasta"] = hasta_d.isoformat()
    return data


@app.get("/api/report/host")
async def report_host(ip: str, range: str = "day", ref: str = "", desde: str = "",
                      hasta: str = "", format: str = "pdf", mac: str = "",
                      role: str = Depends(require_user)):
    """Reporte de auditoría de un equipo: consumo hora por hora, picos y apps.

    range=day|week (con ref=AAAA-MM-DD, default hoy) o range=custom con
    desde/hasta (días inclusive, máx. 31). mac opcional: cuando varios equipos
    tuvieron la IP, limita el reporte al equipo con esa MAC (sus franjas).
    """
    ip = _valid_ip(ip)
    mac = _valid_mac(mac) if mac else None
    try:
        if range == "custom":
            start, end, label = reports.period_bounds_custom(
                date.fromisoformat(desde), date.fromisoformat(hasta))
        elif range in ("day", "week"):
            start, end, label = reports.period_bounds(
                range, date.fromisoformat(ref) if ref else settings.today())
        else:
            raise HTTPException(400, "range debe ser day, week o custom")
    except ValueError as exc:
        raise HTTPException(400, f"fechas inválidas: {exc}")
    data = await reports.gather_host_report(state["pool"], ip, start, end, label, mac)
    stem = f"netmon_equipo_{ip}_{start.date().isoformat()}"
    if range != "day":
        stem += f"_a_{(end - timedelta(days=1)).date().isoformat()}"
    if mac:
        stem += f"_{mac.replace(':', '')}"
    if format == "csv":
        return Response(reports.build_host_csv(data), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{stem}.csv"'})
    return Response(reports.build_host_pdf(data), media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{stem}.pdf"'})


# --- Borrado de consumos (función oculta, solo admin) ---------------------------
PURGE_MAX_DAYS = 400


def _purge_bounds(desde: str, hasta: str) -> tuple[datetime, datetime]:
    """Acepta día (AAAA-MM-DD, incluye el día completo de 'hasta') u hora
    (AAAA-MM-DDTHH:MM, límites exactos). Zona del negocio."""
    z = settings.zone()

    def one(s: str) -> tuple[datetime, bool]:
        if "T" in s:                                   # datetime-local (por hora)
            return datetime.fromisoformat(s).replace(tzinfo=z), True
        return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=z), False

    start, _ = one(desde)
    end, end_is_dt = one(hasta)
    if not end_is_dt:                                  # por día: incluir todo 'hasta'
        end = end + timedelta(days=1)
    if end <= start:
        raise ValueError("'hasta' debe ser posterior a 'desde'")
    if (end - start).days > PURGE_MAX_DAYS:
        raise ValueError(f"el rango no puede superar {PURGE_MAX_DAYS} días")
    return start, end


class PurgeBody(BaseModel):
    ip: str
    desde: str
    hasta: str
    mac: str = ""
    note: str = ""
    confirm: bool = False


@app.get("/api/admin/purge/preview")
async def purge_preview(ip: str, desde: str, hasta: str, mac: str = "",
                        role: str = Depends(require_admin)):
    """Cuenta cuántas filas de consumo se borrarían (no borra). Solo admin."""
    ip = _valid_ip(ip)
    mac = _valid_mac(mac) if mac else None
    try:
        start, end = _purge_bounds(desde, hasta)
    except ValueError as exc:
        raise HTTPException(400, f"fechas inválidas: {exc}")
    cut = await reports._rollup_until(state["pool"])
    return await purge.preview(state["pool"], ip, start, end, mac, cut)


@app.post("/api/admin/purge")
async def purge_exec(body: PurgeBody, request: Request, role: str = Depends(require_admin)):
    """Borra consumos de un equipo (respalda y audita). Solo admin, con confirmación."""
    if not body.confirm:
        raise HTTPException(400, "Falta la confirmación explícita")
    ip = _valid_ip(body.ip)
    mac = _valid_mac(body.mac) if body.mac else None
    try:
        start, end = _purge_bounds(body.desde, body.hasta)
    except ValueError as exc:
        raise HTTPException(400, f"fechas inválidas: {exc}")
    user = getattr(request.state, "user", "admin")
    res = await purge.execute(state["pool"], ip, start, end, mac, user, body.note)
    log.warning("PURGA de consumo por %s: ip=%s mac=%s %s..%s -> %s filas (backup %s)",
                user, ip, mac, body.desde, body.hasta, res["total"], res["backup"])
    return res


@app.get("/api/admin/purge/sites")
async def purge_sites(ip: str, desde: str, hasta: str, mac: str = "",
                      role: str = Depends(require_admin)):
    """Lista el consumo del equipo por sitio (para elegir cuáles borrar). Solo admin."""
    ip = _valid_ip(ip)
    mac = _valid_mac(mac) if mac else None
    try:
        start, end = _purge_bounds(desde, hasta)
    except ValueError as exc:
        raise HTTPException(400, f"fechas inválidas: {exc}")
    cut = await reports._rollup_until(state["pool"])
    return {"sites": await purge.sites(state["pool"], ip, start, end, mac, cut)}


class PurgeSitesBody(BaseModel):
    ip: str
    desde: str
    hasta: str
    mac: str = ""
    sites: list[str] = []
    note: str = ""
    confirm: bool = False


@app.post("/api/admin/purge/sites")
async def purge_sites_exec(body: PurgeSitesBody, request: Request,
                           role: str = Depends(require_admin)):
    """Borra el tráfico de los sitios elegidos para ese equipo (respalda + audita)."""
    if not body.confirm:
        raise HTTPException(400, "Falta la confirmación explícita")
    if not body.sites:
        raise HTTPException(400, "No se seleccionó ningún sitio")
    ip = _valid_ip(body.ip)
    mac = _valid_mac(body.mac) if body.mac else None
    try:
        start, end = _purge_bounds(body.desde, body.hasta)
    except ValueError as exc:
        raise HTTPException(400, f"fechas inválidas: {exc}")
    user = getattr(request.state, "user", "admin")
    res = await purge.delete_sites(state["pool"], ip, start, end, mac, body.sites, user, body.note)
    log.warning("PURGA de sitios por %s: ip=%s mac=%s %s..%s sitios=%s -> %s filas (backup %s)",
                user, ip, mac, body.desde, body.hasta, body.sites, res["total"], res["backup"])
    return res


SAFE_FILENAME = re.compile(r"^[\w][\w.\-]*$")


@app.get("/api/reports/list")
async def reports_list(role: str = Depends(require_user)):
    """Reportes ya generados (por el timer semanal o a mano) en reports_dir."""
    rdir = Path(settings.reports_dir)
    if not rdir.exists():
        return []
    files = sorted(rdir.glob("netmon_*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"name": p.name, "size": p.stat().st_size,
         "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat()}
        for p in files[:100]
    ]


@app.get("/api/reports/file/{name}")
async def report_file(name: str, role: str = Depends(require_user)):
    if not SAFE_FILENAME.match(name):
        raise HTTPException(400, "nombre inválido")
    path = Path(settings.reports_dir) / name
    if not path.is_file():
        raise HTTPException(404, "no existe")
    return FileResponse(path, filename=name)


# ---------------------------------------------------------------------------
# Configuración: reglas de alerta y mapeo de categorías (solo admin)
# ---------------------------------------------------------------------------

class RuleBody(BaseModel):
    enabled: bool
    severity: str
    params: dict


@app.get("/api/rules")
async def rules_list(role: str = Depends(require_admin)):
    rows = await state["pool"].fetch("SELECT * FROM alert_rules ORDER BY id")
    return [dict(r) for r in rows]


@app.post("/api/rules/{rule_id}")
async def rule_update(rule_id: str, body: RuleBody, role: str = Depends(require_admin)):
    if body.severity not in ("info", "warning", "critical"):
        raise HTTPException(400, "severity inválida")
    result = await state["pool"].execute(
        """UPDATE alert_rules SET enabled = $2, severity = $3, params = $4,
                  updated_at = now() WHERE id = $1""",
        rule_id, body.enabled, body.severity, body.params,
    )
    if not result.endswith("1"):
        raise HTTPException(404, "regla inexistente")
    return {"ok": True}


class CategoryBody(BaseModel):
    app: str
    category: str


@app.get("/api/category-map")
async def category_map(role: str = Depends(require_admin)):
    """Apps vistas en los últimos 7 días con su categoría efectiva + overrides."""
    pool: asyncpg.Pool = state["pool"]
    overrides = await _category_overrides()
    # app_hour hasta 'rollup_until' + app_min desde esa marca (sin solaparse)
    raw = await db.meta_get(pool, "rollup_until")
    cut = datetime.fromisoformat(raw) if raw else None
    seen = await pool.fetch(
        """SELECT app, SUM(bytes_up + bytes_down) AS total FROM (
             SELECT app, bytes_up, bytes_down FROM app_hour
             WHERE ts > now() - interval '7 days' AND ts < $1::timestamptz
             UNION ALL
             SELECT app, bytes_up, bytes_down FROM app_min
             WHERE ts >= GREATEST(now() - interval '7 days',
                                  COALESCE($1::timestamptz, '-infinity'))
           ) x GROUP BY app ORDER BY 2 DESC LIMIT 200""",
        cut,
    )
    apps_seen = [
        {"app": r["app"], "bytes": r["total"],
         "category": categorize(r["app"], overrides),
         "overridden": r["app"].lower() in overrides}
        for r in seen
    ]
    return {"categories": CATEGORY_ORDER, "apps": apps_seen,
            "overrides": [{"app": a, "category": c} for a, c in sorted(overrides.items())]}


@app.post("/api/category-map")
async def category_set(body: CategoryBody, role: str = Depends(require_admin)):
    if body.category not in CATEGORY_ORDER:
        raise HTTPException(400, f"categoría debe ser una de {CATEGORY_ORDER}")
    await state["pool"].execute(
        """INSERT INTO category_map (app, category) VALUES (lower($1), $2)
           ON CONFLICT (app) DO UPDATE SET category = EXCLUDED.category,
             updated_at = now()""",
        body.app.strip(), body.category,
    )
    return {"ok": True}


@app.delete("/api/category-map/{app_name}")
async def category_delete(app_name: str, role: str = Depends(require_admin)):
    await state["pool"].execute("DELETE FROM category_map WHERE app = lower($1)", app_name)
    return {"ok": True}


@app.get("/api/config-info")
async def config_info(role: str = Depends(require_admin)):
    """Estado de la configuración para mostrar (solo lectura) en la vista Config."""
    return {
        "smtp": bool(settings.smtp_host and settings.smtp_to),
        "webhook": bool(settings.webhook_url),
        "geoip": Path(settings.geoip_mmdb).exists(),
        "blocklist": Path(settings.blocklist_path).exists(),
        "ntopng_url": settings.ntopng_url,
        "targets": [{"key": k, "address": a} for k, a in settings.ping_targets()],
        "retention": {
            "minuto": f"{settings.retention_min_hours} h",
            "5 minutos": f"{settings.retention_5min_days} días",
            "hora": f"{settings.retention_hour_days} días",
            "ping": f"{settings.ping_retention_days} días",
        },
        "local_networks": settings.local_networks,
        "disk": _disk_info("/"),
    }


def _disk_info(path: str) -> dict:
    du = shutil.disk_usage(path)
    return {"path": path, "used_pct": round(100.0 * du.used / (du.used + du.free), 1),
            "free_gb": round(du.free / 1024 ** 3, 1), "total_gb": round(du.total / 1024 ** 3, 1)}


# ---------------------------------------------------------------------------
# WebSocket en vivo
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if state.get("pool") is None:          # base todavía no disponible (H20)
        await ws.close(code=1013)
        return
    # auth manual: cookie de sesión (admin o viewer) o ?token=
    sess = await _session_from_cookie(ws.cookies.get(SESSION_COOKIE))
    if not sess and not _token_ok(ws.query_params.get("token")) \
            and not _kiosk_cookie_ok(ws.cookies.get(KIOSK_COOKIE)):
        await ws.close(code=4401)
        return
    await ws.accept()
    state["ws_clients"].add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive del cliente; ignoramos el contenido
    except WebSocketDisconnect:
        pass
    finally:
        state["ws_clients"].discard(ws)


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


async def broadcast(payload: dict) -> None:
    if not state["ws_clients"]:
        return
    msg = json.dumps(payload, default=_json_default)
    dead = []
    for ws in state["ws_clients"]:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state["ws_clients"].discard(ws)


async def refresh_hostname_cache() -> None:
    """Cache IP -> (hostname, usuario) para decorar el snapshot en vivo sin JOIN por push."""
    pool: asyncpg.Pool = state["pool"]
    rows = await pool.fetch(
        """SELECT host(h.ip) AS ip, h.hostname, u.username
           FROM hostnames h
           LEFT JOIN ip_user u ON u.ip = h.ip
             AND u.seen_at > now() - ($1::text)::interval""",
        f"{settings.user_map_ttl_hours} hours",
    )
    state["hostname_cache"] = {
        r["ip"]: (r["hostname"] or "", r["username"] or "") for r in rows
    }


async def realtime_loop() -> None:
    """Task liviano: muestrea throughput/pps de la interfaz cada rt_interval
    segundos, alimenta el ring buffer y lo empuja por WebSocket. Es lo que hace
    'fluir' el gráfico realtime sin recargar nada."""
    nt: NtopngClient = state["nt"]
    while True:
        try:
            data = await nt.interface_data()
            sample = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "bps": as_num(pick(data, "throughput_bps", "thpt.bps",
                                   "throughput.bps", default=0)),
                "pps": as_num(pick(data, "throughput_pps", "thpt.pps",
                                   "throughput.pps", default=0)),
                "packets": as_num(pick(data, "packets", "stats.packets", default=0)),
            }
            state["rt_buf"].append(sample)
            await broadcast({"type": "rt", "sample": sample})
        except Exception:
            # sin ntopng no hay muestra; el front muestra "sin datos"
            await broadcast({"type": "rt", "sample": None})
        await asyncio.sleep(settings.rt_interval)


def live_rates(prev: dict, cur: dict, cap_bps: float) -> dict[str, tuple[float, float]]:
    """ip -> (subida, bajada) en bits/s entre dos lecturas (sent, rcvd, t) de cada host.

    Usa el tiempo REAL entre las dos lecturas de ese host (auditoría H01, causa 3:
    con el intervalo de la vuelta, los hosts de las últimas páginas del listado
    salían hasta 60 % inflados). Host nuevo, contador que retrocede o tiempo no
    positivo -> 0. Tope por host = capacidad del enlace.
    """
    out: dict[str, tuple[float, float]] = {}
    for ip, (sent, rcvd, t) in cur.items():
        p = prev.get(ip)
        if not p or t <= p[2]:
            out[ip] = (0.0, 0.0)
            continue
        dt = t - p[2]
        up = (sent - p[0]) * 8 / dt if sent >= p[0] else 0.0
        down = (rcvd - p[1]) * 8 / dt if rcvd >= p[1] else 0.0
        out[ip] = (min(up, cap_bps), min(down, cap_bps))
    return out


async def live_loop() -> None:
    """Task de fondo: ntopng -> WebSocket cada live_interval segundos."""
    nt: NtopngClient = state["nt"]
    pool: asyncpg.Pool = state["pool"]
    cache_age = 0.0
    while True:
        try:
            if cache_age <= 0:
                await refresh_hostname_cache()
                cache_age = 60.0

            # subida/bajada por delta de contadores acumulados (bytes.sent/recvd):
            # ntopng en SPAN no separa dirección en thpt, pero los contadores por
            # host sí. Mismo criterio que el colector (up=sent, down=rcvd).
            prev = state.get("live_bw") or {}
            cap = settings.link_mbps * 1_000_000          # bits/s por host
            snap = await nt.active_hosts()
            entries = [(nt.host_row_basics(r)[0], nt.host_row_basics(r)[1], r.get("vlan", 0)) for r in snap]
            # una entrada por host real (descarta duplicados por VLAN y MAC de router),
            # igual que el colector -> el total coincide con el gráfico histórico
            weights = {}
            for r in snap:
                sent_, rcvd_ = nt.row_counters(r)
                weights[(nt.host_row_basics(r)[0], r.get("vlan", 0))] = sent_ + rcvd_
            own = {(i, v) for i, v in own_host_entries(entries, weights) if settings.is_local_ip(i)}
            agg: dict[str, list] = {}   # ip -> [sent, rcvd, hora de lectura] (sus VLAN propias)
            for r in snap:
                ip = nt.host_row_basics(r)[0]
                vlan = r.get("vlan", 0)
                if (ip, vlan) not in own:
                    continue
                sent, rcvd = nt.row_counters(r)
                a = agg.setdefault(ip, [0, 0, 0.0])
                a[0] += sent
                a[1] += rcvd
                a[2] = max(a[2], r.get("_nm_t", 0.0))
            cur = {ip: (a[0], a[1], a[2]) for ip, a in agg.items()}
            rates = live_rates(prev.get("hosts", {}), cur, cap)
            live_hosts = []
            total_up = total_down = 0.0
            for ip in cur:
                up_bps, down_bps = rates[ip]
                total_up += up_bps
                total_down += down_bps
                cached = state["hostname_cache"].get(ip, ("", ""))
                live_hosts.append({
                    "ip": ip, "hostname": cached[0], "user": cached[1],
                    "up_bps": up_bps, "down_bps": down_bps,
                })
            state["live_bw"] = {"hosts": cur}
            live_hosts.sort(key=lambda h: h["up_bps"] + h["down_bps"], reverse=True)

            targets = [dict(t) for t in await pool.fetch(
                "SELECT target, address, up, since, last_rtt_ms, last_loss_pct, updated_at "
                "FROM target_state ORDER BY target")]
            alerts_open = await pool.fetchval("SELECT COUNT(*) FROM alerts WHERE NOT acked")

            await broadcast({
                "type": "live",
                "ts": datetime.utcnow().isoformat() + "Z",
                "totals": {"up_bps": total_up, "down_bps": total_down},
                "hosts": live_hosts[:20],
                "targets": targets,
                "alerts_open": alerts_open,
            })
        except Exception:
            log.exception("live_loop: ciclo falló (¿ntopng caído?)")
            await broadcast({"type": "live_error",
                             "message": "Sin datos de ntopng en este momento"})
        cache_age -= settings.live_interval
        await asyncio.sleep(settings.live_interval)


# ---------------------------------------------------------------------------
# Ciclo de vida + frontend estático
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    state["nt"] = NtopngClient()
    # la base puede no estar lista (arranque del servidor, mantenimiento): la API
    # levanta igual, responde 503 y se conecta en segundo plano (auditoría H20)
    state["db_task"] = asyncio.create_task(_connect_db())
    state["rt_task"] = asyncio.create_task(realtime_loop())
    log.info("API iniciada en %s:%d", settings.listen_host, settings.listen_port)


async def _connect_db() -> None:
    delay = 2.0
    while state.get("pool") is None:
        try:
            state["pool"] = await db.create_pool()
        except (OSError, asyncpg.PostgresError) as exc:
            log.error("PostgreSQL no disponible (%s); reintento en %.0f s", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    log.info("conectado a PostgreSQL")
    state["live_task"] = asyncio.create_task(live_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in ("db_task", "live_task", "rt_task"):
        if state.get(task):
            state[task].cancel()
    await state["nt"].close()
    if state.get("pool"):
        await state["pool"].close()


frontend = Path(settings.frontend_dir)
if not frontend.exists():  # desarrollo: usar el frontend del repo
    frontend = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
async def index():
    return FileResponse(frontend / "index.html")


@app.get("/kiosk")
async def kiosk(request: Request):
    """Misma SPA; el frontend detecta la ruta y entra en modo pantalla fija
    (sin navegación, tipografía ampliada, rotación Resumen/Estado cada 30 s).
    Con ?token= válido deja una cookie y redirige a /kiosk sin el token."""
    if _token_ok(request.query_params.get("token")):
        resp = RedirectResponse("/kiosk", status_code=303)
        resp.set_cookie(KIOSK_COOKIE, kiosk_signer.dumps({"k": _kiosk_fingerprint()}),
                        max_age=KIOSK_COOKIE_MAX_AGE, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
        return resp
    return FileResponse(frontend / "index.html")


app.mount("/static", StaticFiles(directory=str(frontend)), name="static")
