"""Monitor de estado de red: latencia, pérdida y estado ON/OFF de enlaces.

Targets (de la configuración): gateway (WatchGuard), sondas de internet
(8.8.8.8, ...) y el DNS interno del dominio.

Cada NETMON_PING_INTERVAL segundos manda NETMON_PING_COUNT pings a cada target:
  * Actualiza target_state (estado instantáneo que muestra el dashboard).
  * Acumula y persiste promedios por minuto en ping_min (histórico/gráficos).
  * Máquina de estados de alertas:
      - 3 rondas seguidas con 100% de pérdida  -> link_down (critical)
      - primera ronda con respuesta tras caída -> link_up (info)
      - latencia o pérdida sobre umbral sostenida NETMON_DEGRADED_MINUTES
        minutos -> degraded (warning), una sola vez hasta que normalice.

ICMP sin root: usa icmplib en modo no privilegiado (requiere el sysctl
net.ipv4.ping_group_range, lo setea el instalador). Si no está disponible,
cae a modo privilegiado (CAP_NET_RAW, otorgada en la unit de systemd).

Correr con: python -m netmon.pinger
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone

import asyncpg
from icmplib import async_ping
from icmplib.exceptions import SocketPermissionError

from . import db
from .alerting import raise_alert
from .config import get_settings

log = logging.getLogger("netmon.pinger")

DOWN_AFTER_ROUNDS = 3  # rondas consecutivas 100% loss para declarar caída


class TargetMonitor:
    def __init__(self, key: str, address: str, privileged: bool) -> None:
        s = get_settings()
        self.key = key
        self.address = address
        self.privileged = privileged
        self.up = True
        self.fail_rounds = 0
        self.degraded_alerted = False
        # ventana deslizante de (rtt_avg, loss_pct) por ronda para degradación
        rounds_per_min = max(1, 60 // s.ping_interval)
        self.window: deque[tuple[float | None, float]] = deque(
            maxlen=rounds_per_min * s.degraded_minutes
        )
        # acumuladores del minuto en curso
        self.minute: datetime | None = None
        self.rtts: list[float] = []
        self.sent = 0
        self.lost = 0
        self.probe_errors = 0      # rondas seguidas en que el ping mismo falló

    async def probe(self) -> tuple[float | None, float]:
        """Una ronda de ping. Devuelve (rtt_avg_ms | None, loss_pct)."""
        s = get_settings()
        host = await async_ping(
            self.address, count=s.ping_count, interval=0.2,
            timeout=2, privileged=self.privileged,
        )
        loss_pct = host.packet_loss * 100.0
        rtt = host.avg_rtt if host.packets_received > 0 else None
        return rtt, loss_pct


async def handle_round(
    pool: asyncpg.Pool, mon: TargetMonitor, rtt: float | None, loss: float
) -> None:
    s = get_settings()
    now = datetime.now(timezone.utc)
    minute = now.replace(second=0, microsecond=0)

    # ---- estado instantáneo -------------------------------------------------
    was_up = mon.up
    if loss >= 100.0:
        mon.fail_rounds += 1
        if mon.fail_rounds >= DOWN_AFTER_ROUNDS and mon.up:
            mon.up = False
            msg = f"Enlace CAÍDO: {mon.key} ({mon.address}) sin respuesta " \
                  f"hace {mon.fail_rounds * s.ping_interval}s"
            await raise_alert(pool, "link_down", "critical", msg,
                              {"target": mon.key, "address": mon.address})
    else:
        mon.fail_rounds = 0
        if not mon.up:
            mon.up = True
            msg = f"Enlace RESTABLECIDO: {mon.key} ({mon.address}), rtt {rtt:.1f} ms"
            await raise_alert(pool, "link_up", "info", msg,
                              {"target": mon.key, "address": mon.address})

    await pool.execute(
        """INSERT INTO target_state (target, address, up, since, last_rtt_ms,
                                     last_loss_pct, updated_at)
           VALUES ($1, $2, $3, now(), $4, $5, now())
           ON CONFLICT (target) DO UPDATE SET
             address = EXCLUDED.address,
             up = EXCLUDED.up,
             since = CASE WHEN target_state.up <> EXCLUDED.up
                          THEN now() ELSE target_state.since END,
             last_rtt_ms = EXCLUDED.last_rtt_ms,
             last_loss_pct = EXCLUDED.last_loss_pct,
             updated_at = now()""",
        mon.key, mon.address, mon.up, rtt, loss,
    )
    if was_up != mon.up:
        log.warning("%s -> %s", mon.key, "UP" if mon.up else "DOWN")

    # ---- degradación sostenida ----------------------------------------------
    mon.window.append((rtt, loss))
    if len(mon.window) == mon.window.maxlen and mon.up:
        rtts = [r for r, _ in mon.window if r is not None]
        avg_rtt = sum(rtts) / len(rtts) if rtts else 0.0
        avg_loss = sum(l for _, l in mon.window) / len(mon.window)
        degraded = avg_rtt > s.lat_warn_ms or avg_loss > s.loss_warn_pct
        if degraded and not mon.degraded_alerted:
            mon.degraded_alerted = True
            msg = (f"Degradación en {mon.key} ({mon.address}): "
                   f"rtt prom {avg_rtt:.0f} ms / pérdida {avg_loss:.1f}% "
                   f"sostenido {s.degraded_minutes} min")
            await raise_alert(pool, "degraded", "warning", msg,
                              {"target": mon.key, "rtt": avg_rtt, "loss": avg_loss})
        elif not degraded:
            mon.degraded_alerted = False

    # ---- agregado por minuto --------------------------------------------------
    if mon.minute is None:
        mon.minute = minute
    if minute != mon.minute:
        await flush_minute(pool, mon)
        mon.minute = minute
    mon.sent += s.ping_count
    mon.lost += round(s.ping_count * loss / 100.0)
    if rtt is not None:
        mon.rtts.append(rtt)


async def flush_minute(pool: asyncpg.Pool, mon: TargetMonitor) -> None:
    if mon.minute is None or mon.sent == 0:
        return
    rtt_avg = sum(mon.rtts) / len(mon.rtts) if mon.rtts else None
    rtt_max = max(mon.rtts) if mon.rtts else None
    loss_pct = 100.0 * mon.lost / mon.sent
    await pool.execute(
        """INSERT INTO ping_min (ts, target, rtt_avg_ms, rtt_max_ms, loss_pct)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (ts, target) DO UPDATE SET
             rtt_avg_ms = EXCLUDED.rtt_avg_ms, rtt_max_ms = EXCLUDED.rtt_max_ms,
             loss_pct = EXCLUDED.loss_pct""",
        mon.minute, mon.key, rtt_avg, rtt_max, loss_pct,
    )
    mon.rtts, mon.sent, mon.lost = [], 0, 0


PROBE_ERROR_ALERT_AFTER = 3


async def probe_and_record(pool: asyncpg.Pool, mon: TargetMonitor) -> None:
    """Una ronda de un target. Si el ping mismo falla (permisos, socket, DNS) NO
    es pérdida de paquetes: la ronda queda sin dato (target_state envejece y la
    UI muestra 'Sin datos') y tras varias seguidas se avisa que el monitor no
    puede medir (auditoría H23)."""
    try:
        rtt, loss = await mon.probe()
    except Exception as exc:
        mon.probe_errors += 1
        log.error("probe %s falló (%d seguidas): %s", mon.key, mon.probe_errors, exc)
        if mon.probe_errors == PROBE_ERROR_ALERT_AFTER:
            await raise_alert(pool, "pinger", "warning",
                              f"El monitor de enlaces no puede medir {mon.key} ({mon.address}): "
                              f"{exc.__class__.__name__}: {exc}",
                              {"target": mon.key, "address": mon.address})
        return
    mon.probe_errors = 0
    await handle_round(pool, mon, rtt, loss)


async def detect_privileged_mode() -> bool:
    """False = modo no privilegiado disponible (preferido). True = usar raw socket."""
    try:
        await async_ping("127.0.0.1", count=1, timeout=1, privileged=False)
        return False
    except SocketPermissionError:
        log.info("modo no privilegiado no disponible, usando raw sockets (CAP_NET_RAW)")
        return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = get_settings()
    pool = await db.create_pool()
    privileged = await detect_privileged_mode()
    monitors = [TargetMonitor(k, addr, privileged) for k, addr in s.ping_targets()]
    log.info("pinger iniciado: %s", ", ".join(f"{m.key}={m.address}" for m in monitors))

    try:
        while True:
            started = asyncio.get_event_loop().time()

            await asyncio.gather(*(probe_and_record(pool, m) for m in monitors))
            elapsed = asyncio.get_event_loop().time() - started
            await asyncio.sleep(max(1.0, s.ping_interval - elapsed))
    finally:
        for mon in monitors:
            await flush_minute(pool, mon)
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
