"""Lista de reputación de IPs (FireHOL level1) para detectar hosts internos
hablando con direcciones de mala reputación.

Formato .netset: una IP o CIDR IPv4 por línea, comentarios con '#'. La lista se
descarga semanalmente (netmon-feeds.timer); acá solo se parsea a rangos de
enteros ordenados para hacer lookup O(log n) con bisect. Se recarga sola si
cambia el mtime del archivo. Si el archivo no existe, la regla queda inactiva.
"""

from __future__ import annotations

import bisect
import ipaddress
import logging
from pathlib import Path

log = logging.getLogger("netmon.blocklist")


class Blocklist:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime: float = -1.0
        self._starts: list[int] = []   # inicio de cada rango, ordenado
        self._ends: list[int] = []     # fin (inclusive) paralelo a _starts

    def _load(self) -> None:
        ranges: list[tuple[int, int]] = []
        try:
            with self.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        net = ipaddress.ip_network(line, strict=False)
                    except ValueError:
                        continue
                    if net.version != 4:
                        continue
                    first = int(net.network_address)
                    ranges.append((first, first + net.num_addresses - 1))
        except OSError:
            log.warning("blocklist no disponible en %s", self.path)
            self._starts, self._ends = [], []
            return
        ranges.sort()
        self._starts = [r[0] for r in ranges]
        self._ends = [r[1] for r in ranges]
        log.info("blocklist cargada: %d rangos", len(ranges))

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = -1.0
        if mtime != self._mtime:
            self._mtime = mtime
            self._load()

    @property
    def available(self) -> bool:
        self._refresh()
        return bool(self._starts)

    def contains(self, ip: str) -> bool:
        """True si la IP (v4) cae en algún rango de la lista."""
        self._refresh()
        if not self._starts:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.version != 4:
            return False
        # FireHOL level1 incluye bogons/RFC1918: una IP privada (otra sede, VPN)
        # no es "mala reputación" (auditoría H28: 286/286 alertas eran falsas)
        if addr.is_private or addr.is_reserved or addr.is_loopback \
                or addr.is_link_local or addr.is_multicast:
            return False
        n = int(addr)
        i = bisect.bisect_right(self._starts, n) - 1
        return i >= 0 and n <= self._ends[i]
