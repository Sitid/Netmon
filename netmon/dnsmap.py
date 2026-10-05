"""Mapa DNS pasivo: qué nombre preguntó cada equipo antes de conectarse a una IP.

Buena parte del tráfico de internet va por QUIC o TLS con ECH y no trae el nombre
del sitio (SNI): sólo se ve la IP de destino. Pero antes de conectarse el equipo
resolvió ese nombre por DNS, y esas respuestas viajan en claro por el espejo.
Suricata ya las registra en eve.json (evento dns v2): este servicio las lee,
guarda "cliente X preguntó <nombre> y recibió <ip>" y el colector lo usa para
nombrar las conexiones sin SNI.

Límites: equipos con DNS cifrado (DoH/DoT, Private Relay) no aparecen; en CDNs
compartidas la IP puede servir varios sitios (por eso se prefiere la respuesta
que recibió el mismo cliente y se descartan respuestas de más de 12 h).

Correr con: python -m netmon.dnsmap
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
from datetime import datetime, timedelta

import asyncpg

from . import db

log = logging.getLogger("netmon.dnsmap")

EVE_PATH = "/var/log/suricata/eve.json"
MAX_AGE = timedelta(hours=12)        # respuesta DNS más vieja que esto no se usa
RETENTION = "2 days"
FLUSH_S = 30


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def parse_dns_line(line: str) -> list[tuple[str, str, str, datetime]]:
    """(cliente, ip_respondida, nombre_preguntado, hora) de una línea de eve.json.

    Sólo respuestas NOERROR con registros A/AAAA. El nombre es el que preguntó el
    cliente (rrname), no el final de la cadena CNAME (que suele ser un nombre
    interno de la CDN).
    """
    if '"dns"' not in line:
        return []
    try:
        ev = json.loads(line)
    except ValueError:
        return []
    if ev.get("event_type") != "dns":
        return []
    d = ev.get("dns") or {}
    if d.get("type") != "answer" or d.get("rcode") != "NOERROR":
        return []
    name = str(d.get("rrname") or "").strip(".").lower()
    if not name or name.endswith((".arpa", ".local")) or "." not in name:
        return []
    client = ev.get("src_ip") or ""
    try:
        ts = datetime.strptime(ev["timestamp"], "%Y-%m-%dT%H:%M:%S.%f%z")
        ipaddress.ip_address(client)
    except (KeyError, ValueError):
        return []
    out = []
    for a in d.get("answers") or []:
        if a.get("rrtype") in ("A", "AAAA"):
            try:
                ip = str(ipaddress.ip_address(a.get("rdata", "")))
            except ValueError:
                continue
            out.append((client, ip, name, ts))
    return out


# ---------------------------------------------------------------------------
# Lectura continua de eve.json (sigue la rotación de logrotate)
# ---------------------------------------------------------------------------
class EveTail:
    """Lee líneas nuevas de un archivo que crece y se rota (renombrar + crear).

    Detecta la rotación por cambio de inodo o porque el archivo se achicó; una
    línea incompleta al final queda en espera hasta que llegue su '\\n'.
    """

    def __init__(self, path: str, from_start: bool = False) -> None:
        self.path = path
        self._fh = None
        self._ino = None
        self._buf = ""
        self._open(seek_end=not from_start)

    def _open(self, seek_end: bool) -> None:
        try:
            fh = open(self.path, encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None
            return
        if self._fh:
            self._fh.close()
        self._fh = fh
        self._ino = os.fstat(fh.fileno()).st_ino
        self._buf = ""
        if seek_end:
            fh.seek(0, os.SEEK_END)

    def _rotated(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        return st.st_ino != self._ino or st.st_size < self._fh.tell()

    def read_lines(self, max_bytes: int = 64 * 1024 * 1024) -> list[str]:
        if self._fh is None:
            self._open(seek_end=False)
            if self._fh is None:
                return []
        lines = self._drain(max_bytes)
        if self._rotated():
            lines += self._drain(max_bytes)      # lo que quedaba en el archivo viejo
            self._open(seek_end=False)           # el nuevo, desde el principio
            lines += self._drain(max_bytes)
        return lines

    def _drain(self, max_bytes: int) -> list[str]:
        data = self._fh.read(max_bytes)
        if not data:
            return []
        data = self._buf + data
        *complete, self._buf = data.split("\n")
        return complete


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
async def record(pool: asyncpg.Pool, rows: list[tuple[str, str, str, datetime]]) -> None:
    """Guarda (cliente, ip, nombre, hora); por par se queda la respuesta más nueva."""
    latest: dict[tuple[str, str], tuple[str, datetime]] = {}
    for client, ip, name, ts in rows:
        k = (client, ip)
        if k not in latest or ts > latest[k][1]:
            latest[k] = (name, ts)
    if not latest:
        return
    await pool.executemany(
        """INSERT INTO dns_map (client_ip, answer_ip, domain, seen_at)
           VALUES ($1::inet, $2::inet, $3, $4)
           ON CONFLICT (client_ip, answer_ip) DO UPDATE
             SET domain = EXCLUDED.domain, seen_at = EXCLUDED.seen_at
           WHERE EXCLUDED.seen_at >= dns_map.seen_at""",
        [(c, ip, n, ts) for (c, ip), (n, ts) in latest.items()])


async def lookup(pool: asyncpg.Pool, pairs: list[tuple[str, str]],
                 now: datetime) -> dict[tuple[str, str], str]:
    """(ip_local, ip_remota) -> nombre, según las respuestas DNS de las últimas 12 h.

    Prefiere la respuesta que recibió ese mismo equipo; si no hay, la de cualquier
    otro (p. ej. el DC, que resuelve hacia afuera por los clientes).
    """
    if not pairs:
        return {}
    rows = await pool.fetch(
        """SELECT DISTINCT ON (x.l, x.r) host(x.l) AS l, host(x.r) AS r, m.domain
           FROM unnest($1::inet[], $2::inet[]) AS x(l, r)
           JOIN dns_map m ON m.answer_ip = x.r AND m.seen_at > $3::timestamptz - $4::interval
           ORDER BY x.l, x.r, (m.client_ip = x.l) DESC, m.seen_at DESC""",
        [p[0] for p in pairs], [p[1] for p in pairs], now, MAX_AGE)
    return {(r["l"], r["r"]): r["domain"] for r in rows}


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------
async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    path = os.environ.get("NETMON_EVE_PATH", EVE_PATH)
    pool = await db.create_pool()
    tail = EveTail(path, from_start=False)
    pending: list[tuple[str, str, str, datetime]] = []
    loop = asyncio.get_running_loop()
    last_flush = last_purge = loop.time()
    log.info("dnsmap iniciado leyendo %s", path)
    try:
        while True:
            lines = await asyncio.to_thread(tail.read_lines)
            for line in lines:
                pending.extend(parse_dns_line(line))
            now = loop.time()
            if now - last_flush >= FLUSH_S and pending:
                n = len(pending)
                await record(pool, pending)
                pending = []
                last_flush = now
                log.debug("dnsmap: %d respuestas guardadas", n)
            if now - last_purge >= 3600:
                await pool.execute(
                    "DELETE FROM dns_map WHERE seen_at < now() - ($1::text)::interval", RETENTION)
                last_purge = now
            if not lines:
                await asyncio.sleep(1.0)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
