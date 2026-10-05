"""Cliente async mínimo para la REST API v2 de ntopng (Community Edition).

Endpoints usados (probados contra ntopng 5.x/6.x CE):
  GET /lua/rest/v2/get/host/active.lua     -> lista de hosts activos (paginada)
  GET /lua/rest/v2/get/host/data.lua       -> detalle de un host (contadores + nDPI)
  GET /lua/rest/v2/get/interface/data.lua  -> totales de la interfaz

ntopng envuelve todo en {"rc": 0, "rc_str": "OK", "rsp": {...}}.
Los nombres de campo variaron entre versiones ("bytes.sent" vs "bytes_sent",
"thpt" dict vs "throughput_bps"), por eso `pick()` prueba varios candidatos.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import get_settings

log = logging.getLogger("netmon.ntopng")


def pick(d: dict, *candidates: str, default: Any = None) -> Any:
    """Busca la primera clave presente. Soporta rutas anidadas 'a.b' además de
    claves literales con punto (ntopng usa 'bytes.sent' como clave literal)."""
    for key in candidates:
        if key in d:
            return d[key]
        if "." in key:  # probar como ruta anidada
            cur: Any = d
            ok = True
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok:
                return cur
    return default


def as_num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Una MAC que aparece con varias IP en el mismo snapshot es de un router/firewall
# (p. ej. la virtual VRRP 00:00:5e:00:01:xx del gateway).
ROUTER_MAC_MIN_IPS = 3
VRRP_MAC_PREFIXES = ("00:00:5e:00:01:", "00:00:5e:00:02:")


def router_macs(entries: list[tuple[str, str, Any]]) -> set[str]:
    """MACs de router/firewall en un snapshot de [(ip, mac, vlan)]."""
    ips_per_mac: dict[str, set[str]] = {}
    for ip, mac, _ in entries:
        ips_per_mac.setdefault((mac or "").lower(), set()).add(ip)
    return {m for m, ips in ips_per_mac.items()
            if m.startswith(VRRP_MAC_PREFIXES) or len(ips) >= ROUTER_MAC_MIN_IPS}


def own_host_entries(entries: list[tuple[str, str, Any]],
                     weights: dict | None = None) -> list[tuple[str, Any]]:
    """De [(ip, mac, vlan)] devuelve las (ip, vlan) que representan al host real.

    El SPAN del troncal del firewall ve el tráfico inter-VLAN dos veces: en la
    VLAN del host (con su MAC) y en la VLAN del otro extremo (con la MAC del
    router). ntopng crea una entrada por VLAN; si la IP tiene alguna entrada con
    MAC propia, las de MAC de router se descartan para no contar doble.

    Si sólo tiene entradas de router (su MAC no llega al espejo), son el MISMO
    tráfico visto en distintos tramos: con `weights` {(ip, vlan): bytes} se
    conserva sólo la copia de más tráfico (auditoría H30: sumarlas contaba ~6 %
    de más). Sin weights se conservan todas (comportamiento anterior).
    """
    routers = router_macs(entries)
    by_ip: dict[str, list[tuple[str, Any]]] = {}
    for ip, mac, vlan in entries:
        by_ip.setdefault(ip, []).append(((mac or "").lower(), vlan))

    out: list[tuple[str, Any]] = []
    for ip, lst in by_ip.items():
        own = [(m, v) for m, v in lst if m not in routers]
        if own:
            out.extend((ip, v) for _, v in own)
        elif weights is not None:
            best = max(dict.fromkeys(v for _, v in lst), key=lambda v: weights.get((ip, v), 0))
            out.append((ip, best))
        else:
            out.extend((ip, v) for _, v in lst)
    return out


class NtopngClient:
    def __init__(self) -> None:
        s = get_settings()
        headers = {}
        auth = None
        if s.ntopng_token:
            headers["Authorization"] = f"Token {s.ntopng_token}"
        elif s.ntopng_user:
            auth = (s.ntopng_user, s.ntopng_pass)
        self.ifid = s.ntopng_ifid
        # sin keep-alive: ntopng cierra conexiones ociosas sin avisar y reusar una
        # muerta da "Server disconnected". Es loopback: abrir una nueva no cuesta.
        self._client = httpx.AsyncClient(
            base_url=s.ntopng_url, headers=headers, auth=auth, timeout=15.0,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        try:
            resp = await self._client.get(path, params=params or {})
        except httpx.RemoteProtocolError:
            # ntopng cierra conexiones keep-alive sin avisar; un GET es idempotente
            resp = await self._client.get(path, params=params or {})
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("rc", 0) != 0:
            raise RuntimeError(f"ntopng rc={payload.get('rc')} {payload.get('rc_str')} en {path}")
        return payload.get("rsp", payload) if isinstance(payload, dict) else payload

    # ------------------------------------------------------------------
    PER_PAGE = 250

    async def _paged(self, path: str, params: dict, max_pages: int,
                     warn_cap: bool = True, key=None) -> list[dict]:
        """Recorre todas las páginas de un endpoint de listado de ntopng.

        Esta versión de ntopng no devuelve 'totalRows': el fin se detecta por
        una página incompleta. Si viene 'totalRows' también se respeta.

        La lista se reordena mientras se pagina (entran y salen hosts), así que
        una misma fila puede venir en dos páginas: con `key` se descartan las
        repetidas (medido: 10-50 repetidas por lectura de ~4000 hosts).
        """
        rows: list[dict] = []
        seen: set = set()
        for page in range(1, max_pages + 1):
            rsp = await self._get(path, {**params, "perPage": self.PER_PAGE, "currentPage": page})
            # hora de lectura de esta página: recorrer el listado tarda 5-13 s, así que
            # las tasas se calculan con el tiempo real de cada fila, no el de la vuelta
            read_t = time.monotonic()
            if isinstance(rsp, list):        # algunas versiones devuelven lista directa
                data, total = rsp, None
            else:
                data = rsp.get("data", [])
                total = rsp.get("totalRows")
            for row in data:
                if key is not None:
                    k = key(row)
                    if k in seen:
                        continue
                    seen.add(k)
                row["_nm_t"] = read_t
                rows.append(row)
            if len(data) < self.PER_PAGE or (total is not None and len(rows) >= int(total)):
                break
        else:
            if warn_cap:
                log.warning("%s: se alcanzó el tope de %d páginas (%d filas)", path, max_pages, len(rows))
        return rows

    async def active_hosts(self) -> list[dict]:
        """Todos los hosts activos de la interfaz (tope: 40 páginas = 10.000 hosts)."""
        return await self._paged("/lua/rest/v2/get/host/active.lua", {"ifid": self.ifid}, 40,
                                 key=lambda r: (self.host_row_basics(r)[0], r.get("vlan", 0)))

    async def top_flows(self, limit: int = 6000) -> list[dict]:
        """Flujos activos ordenados por bytes desc (los de mayor volumen primero).

        ntopng tarda ~1 min en volcar los ~50k flujos activos; el top por bytes
        cubre todo lo significativo en pocos segundos (el resto son <10 KB).
        """
        rows: list[dict] = []
        page = 1
        params = {"ifid": self.ifid, "sortColumn": "column_bytes", "sortOrder": "desc"}
        per = 1000
        while len(rows) < limit:
            rsp = await self._get("/lua/rest/v2/get/flow/active.lua",
                                  {**params, "perPage": per, "currentPage": page})
            data = rsp.get("data", []) if isinstance(rsp, dict) else rsp
            rows.extend(data)
            if len(data) < per:
                break
            page += 1
        return rows[:limit]

    async def host_data(self, ip: str) -> dict:
        """Detalle de un host: contadores acumulados + desglose nDPI por protocolo."""
        return await self._get(
            "/lua/rest/v2/get/host/data.lua", {"ifid": self.ifid, "host": ip}
        )

    async def interface_data(self) -> dict:
        return await self._get("/lua/rest/v2/get/interface/data.lua", {"ifid": self.ifid})

    async def active_flows(self, host: str = "", max_pages: int = 8) -> list[dict]:
        """Flujos activos de la interfaz (paginado, con tope de seguridad).

        `host` filtra en el lado de ntopng si se pasa. max_pages*250 flujos
        alcanza de sobra para 100 puestos; si hay más, están primero los que
        ordena ntopng y el resto no cambia la foto.
        """
        params = {"ifid": self.ifid}
        if host:
            params["host"] = host
        # tope intencional: los flujos se ordenan en ntopng y el resto no cambia la foto
        return await self._paged("/lua/rest/v2/get/flow/active.lua", params, max_pages,
                                 warn_cap=False)

    @staticmethod
    def flow_row(row: dict) -> dict:
        """Normaliza una fila de active flows a un dict plano y estable.

        ntopng anida cliente/servidor y cambió nombres entre versiones;
        acá se aplanan con candidatos múltiples.
        """
        cli = row.get("client") if isinstance(row.get("client"), dict) else {}
        srv = row.get("server") if isinstance(row.get("server"), dict) else {}
        proto = row.get("protocol") if isinstance(row.get("protocol"), dict) else {}
        return {
            "cli_ip": str(pick(cli, "ip", default="") or pick(row, "cli.ip", "client_ip", default="")),
            "cli_port": int(as_num(pick(cli, "port", default=0) or pick(row, "cli.port", default=0))),
            "cli_name": str(pick(cli, "name", default="") or ""),
            "srv_ip": str(pick(srv, "ip", default="") or pick(row, "srv.ip", "server_ip", default="")),
            "srv_port": int(as_num(pick(srv, "port", default=0) or pick(row, "srv.port", default=0))),
            "srv_name": str(pick(srv, "name", default="") or ""),
            "l4": str(pick(proto, "l4", default="") or pick(row, "proto.l4", "l4_proto", default="")),
            "l7": str(pick(proto, "l7", default="") or pick(row, "proto.ndpi", "ndpi_proto", "application", default="")),
            "duration_s": int(as_num(pick(row, "duration", "flow_duration", default=0))),
            "first_seen": int(as_num(pick(row, "first_seen", default=0))),
            "bytes": int(as_num(pick(row, "bytes", "total_bytes", default=0))),
            # "thpt.bps" primero: en versiones nuevas thpt es un dict {"bps":...}
            "thpt_bps": as_num(pick(row, "thpt.bps", "throughput_bps", "thpt", default=0)),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def row_counters(row: dict) -> tuple[int, int]:
        """(enviados, recibidos) acumulados de una fila de active.lua.

        Es el mismo contador que host_counters() del detalle (ntopng lo llama
        'bytes.recvd' en el listado y 'bytes.rcvd' en el detalle).
        """
        sent = as_num(pick(row, "bytes.sent", "bytes_sent"))
        rcvd = as_num(pick(row, "bytes.recvd", "bytes.rcvd", "bytes_rcvd"))
        return int(sent), int(rcvd)

    @staticmethod
    def host_split(detail: dict) -> tuple[int, int] | None:
        """(bytes con redes locales, bytes con el resto) acumulados del host.

        ntopng tiene todo RFC1918 como red local, así que 'non_local' es el
        tráfico con internet. None si esta versión no trae los campos.
        """
        local = pick(detail, "local.bytes")
        non_local = pick(detail, "non_local.bytes")
        if local is None or non_local is None:
            return None
        return int(as_num(local)), int(as_num(non_local))

    @staticmethod
    def host_counters(detail: dict) -> tuple[int, int] | None:
        """(bytes_enviados, bytes_recibidos) acumulados desde que ntopng ve al host.

        None si la respuesta no trae los contadores: NO es lo mismo que 0 bytes
        (si ntopng cambia el nombre del campo, netmon tiene que enterarse en vez
        de registrar ceros en silencio; auditoría H16).
        """
        sent = pick(detail, "bytes.sent", "bytes_sent", "bytes_sent.total")
        rcvd = pick(detail, "bytes.rcvd", "bytes_rcvd", "bytes_rcvd.total")
        if sent is None or rcvd is None:
            return None
        return int(as_num(sent)), int(as_num(rcvd))

    @staticmethod
    def host_ndpi(detail: dict) -> dict[str, tuple[int, int]]:
        """{proto: (sent, rcvd)} del desglose nDPI del host."""
        out: dict[str, tuple[int, int]] = {}
        ndpi = detail.get("ndpi") or {}
        if not isinstance(ndpi, dict):
            return out
        for proto, stats in ndpi.items():
            if not isinstance(stats, dict):
                continue
            sent = as_num(pick(stats, "bytes.sent", "bytes_sent", "bytes.sent.total"))
            rcvd = as_num(pick(stats, "bytes.rcvd", "bytes_rcvd", "bytes.rcvd.total"))
            if sent or rcvd:
                out[str(proto)] = (int(sent), int(rcvd))
        return out

    @staticmethod
    def host_row_basics(row: dict) -> tuple[str, str, str, float | None, float | None]:
        """De una fila de active.lua: (ip, mac, nombre, bps_subida, bps_bajada);
        las tasas son None si ntopng no las desglosa por dirección."""
        ip = str(pick(row, "ip", "host", default="") or "")
        # active.lua puede devolver ip como dict {"ip": "..."} en versiones viejas
        if isinstance(pick(row, "ip"), dict):
            ip = str(pick(row, "ip.ip", default=""))
        mac = str(pick(row, "mac", "mac_address", default="") or "")
        name = str(pick(row, "name", "hostname", default="") or "")
        thpt = pick(row, "thpt", default=None)
        if isinstance(thpt, dict):
            up_bps = as_num(pick(thpt, "bps_sent", "upload", "bps"))
            down_bps = as_num(pick(thpt, "bps_rcvd", "download", default=0))
        else:
            # sin desglose por dirección no se sabe cuánto es subida o bajada:
            # desconocido (None), no "todo bajada" (auditoría H27)
            up_bps = down_bps = None
        return ip, mac, name, up_bps, down_bps
