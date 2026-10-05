"""Integración con Active Directory / Windows (opcional).

Tres fuentes de enriquecimiento, todas de SOLO LECTURA:

1. PTR contra el DNS interno del dominio (NETMON_INTERNAL_DNS_IP):
   resuelve IP -> hostname para las IPs con tráfico reciente. Prio 20.

2. Leases del DHCP de Windows Server (NETMON_AD_DHCP_SERVER, vía WinRM):
   hostname autoritativo + MAC. Prio 10 (la mejor fuente de nombres).

3. Eventos Kerberos 4768 del DC (vía WinRM): "a la cuenta X se le emitió un
   TGT desde la IP Y" -> mapeo IP -> usuario AD con TTL. Requiere cuenta de
   servicio en el grupo "Event Log Readers" del dominio y WinRM habilitado
   (ver docs/04-integracion-ad.md). No requiere agentes en los DCs.

   Nota (AD híbrido con Azure): esto mapea equipos unidos al dominio on-prem.
   Dispositivos solo-AzureAD no generan 4768 en el DC y quedan sin usuario.

Si NETMON_AD_WINRM_HOST está vacío, solo corre la resolución PTR.
Correr con: python -m netmon.adsync
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import asyncpg
import dns.asyncresolver
import dns.reversename

from . import attribution, db
from .config import get_settings

log = logging.getLogger("netmon.adsync")

PTR_BATCH = 80          # IPs a resolver por ciclo
DHCP_EVERY_CYCLES = 3   # leases DHCP cada N ciclos (cambian poco)

MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)

# --- Scripts PowerShell ejecutados en el host WinRM (solo lectura) -----------

PS_KERBEROS_EVENTS = r"""
# EventLogReader directo (como wevtutil): alcanza con "Event Log Readers".
# Get-WinEvent consulta antes la configuración del log Security y eso exige
# más privilegios ("Attempted to perform an unauthorized operation").
$xp = "*[System[(EventID=4768) and TimeCreated[timediff(@SystemTime) <= {lookback_ms}]]]"
$q = New-Object System.Diagnostics.Eventing.Reader.EventLogQuery(
       'Security', [System.Diagnostics.Eventing.Reader.PathType]::LogName, $xp)
$r = New-Object System.Diagnostics.Eventing.Reader.EventLogReader($q)
$out = New-Object System.Collections.Generic.List[object]
while ($e = $r.ReadEvent()) {{
  $x = [xml]$e.ToXml()
  $d = @{{}}
  $x.Event.EventData.Data | ForEach-Object {{ $d[$_.Name] = $_.'#text' }}
  $out.Add([pscustomobject]@{{ u = $d['TargetUserName']; ip = $d['IpAddress'];
                               t = $e.TimeCreated.ToUniversalTime().ToString('o') }})
}}
@($out) | ConvertTo-Json -Compress
"""

PS_DHCP_LEASES = r"""
$leases = Get-DhcpServerv4Scope {cn_arg} |
  Get-DhcpServerv4Lease {cn_arg} -ErrorAction SilentlyContinue |
  Where-Object {{ $_.AddressState -like '*Active*' }} |
  ForEach-Object {{
    [pscustomobject]@{{ ip = $_.IPAddress.ToString(); h = $_.HostName;
                        mac = $_.ClientId }}
  }}
@($leases) | ConvertTo-Json -Compress
"""


def _run_ps(script: str) -> list[dict]:
    """Ejecuta PowerShell remoto (PSRP) y parsea el JSON. Corre en thread (pypsrp es sync).

    Se usa el endpoint Microsoft.PowerShell porque los miembros de "Remote
    Management Users" pueden abrirlo sin ser admin; el shell cmd (winrs) que
    abre pywinrm les da "Acceso denegado".
    """
    from pypsrp.client import Client  # import lazy: solo si el módulo AD está activo

    s = get_settings()
    https = s.ad_winrm_scheme == "https"
    client = Client(
        s.ad_winrm_host, username=s.ad_winrm_user, password=s.ad_winrm_pass,
        ssl=https, cert_validation=not https, auth="ntlm",
    )
    out, streams, had_errors = client.execute_ps(script)
    if had_errors:
        errors = "; ".join(str(e) for e in streams.error)[:400]
        if not out.strip():
            raise RuntimeError(f"PowerShell remoto: {errors}")
        log.warning("PowerShell remoto con errores parciales: %s", errors)
    raw = out.strip()
    if not raw or raw == "null":
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


# ---------------------------------------------------------------------------
# 1) PTR contra el DNS del dominio
# ---------------------------------------------------------------------------

async def ptr_pass(pool: asyncpg.Pool) -> None:
    s = get_settings()
    if not s.internal_dns_ip:
        return
    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = [s.internal_dns_ip]
    resolver.timeout = 2.0
    resolver.lifetime = 3.0

    # IPs con tráfico en la última hora sin hostname bueno y fresco
    rows = await pool.fetch(
        """SELECT DISTINCT t.ip FROM traffic_min t
           LEFT JOIN hostnames h ON h.ip = t.ip
           WHERE t.ts > now() - interval '1 hour'
             AND (h.ip IS NULL OR h.prio > 20
                  OR h.resolved_at < now() - interval '24 hours')
           LIMIT $1""",
        PTR_BATCH,
    )
    resolved = 0
    for row in rows:
        ip = str(row["ip"])
        try:
            answer = await resolver.resolve(dns.reversename.from_address(ip), "PTR")
            hostname = str(answer[0]).rstrip(".")
        except Exception:
            continue  # sin PTR: normal si la zona inversa no está completa
        await pool.execute(
            """INSERT INTO hostnames (ip, hostname, source, prio)
               VALUES ($1, $2, 'ptr', 20)
               ON CONFLICT (ip) DO UPDATE SET hostname = EXCLUDED.hostname,
                 source = 'ptr', prio = 20, resolved_at = now()
               WHERE hostnames.prio >= 20""",
            ip, hostname,
        )
        resolved += 1
    if rows:
        log.info("PTR: %d/%d resueltos", resolved, len(rows))


# ---------------------------------------------------------------------------
# 2) Leases DHCP de Windows Server
# ---------------------------------------------------------------------------

async def dhcp_pass(pool: asyncpg.Pool) -> None:
    s = get_settings()
    # DHCP en el mismo host WinRM: sin -ComputerName (evita un segundo salto de red)
    cn_arg = f"-ComputerName '{s.ad_dhcp_server.replace(chr(39), '')}'" if s.ad_dhcp_server else ""
    script = PS_DHCP_LEASES.format(cn_arg=cn_arg)
    leases = await asyncio.to_thread(_run_ps, script)
    count = 0
    for lease in leases:
        ip, host, mac = lease.get("ip"), (lease.get("h") or "").strip(), lease.get("mac") or ""
        if not ip or not host:
            continue
        await pool.execute(
            """INSERT INTO hostnames (ip, hostname, source, prio)
               VALUES ($1, $2, 'dhcp', 10)
               ON CONFLICT (ip) DO UPDATE SET hostname = EXCLUDED.hostname,
                 source = 'dhcp', prio = 10, resolved_at = now()""",
            ip, host,
        )
        # ClientId viene como 'aa-bb-cc-dd-ee-ff'
        mac_norm = mac.replace("-", ":").lower()
        if MAC_RE.match(mac_norm):
            await pool.execute(
                "UPDATE devices SET hostname = $2 WHERE mac = $1", mac_norm, host
            )
        count += 1
    log.info("DHCP: %d leases activos sincronizados", count)


# ---------------------------------------------------------------------------
# 3) Eventos Kerberos 4768 -> IP -> usuario
# ---------------------------------------------------------------------------

def parse_event_time(raw) -> datetime | None:
    """Hora de un evento del DC (ISO 8601). None si no se puede leer: usar la
    hora de procesamiento atribuiría el login a otro momento (auditoría H26)."""
    try:
        ts = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def kerberos_pass(pool: asyncpg.Pool, lookback_s: int) -> None:
    s = get_settings()
    script = PS_KERBEROS_EVENTS.format(lookback_ms=(lookback_s + 60) * 1000)
    events = await asyncio.to_thread(_run_ps, script)
    mapped = 0
    for ev in events:
        user = (ev.get("u") or "").strip()
        ip = (ev.get("ip") or "").strip().replace("::ffff:", "")
        ts_raw = ev.get("t")
        # descartar cuentas de máquina (PC$), krbtgt y direcciones no locales
        if not user or user.endswith("$") or user.lower() == "krbtgt":
            continue
        if not s.is_local_ip(ip):
            continue
        seen_at = parse_event_time(ts_raw)
        if seen_at is None:
            # sin la hora real del login no se puede atribuir: se descarta (H26)
            log.warning("evento 4768 de %s en %s sin hora legible (%r): descartado", user, ip, ts_raw)
            continue
        # historial + último usuario de la IP + usuario sólo del equipo que tenía
        # la IP en ese momento (antes lo heredaba toda MAC que tuvo la IP: H07)
        await attribution.record_logon(pool, ip, user, seen_at)
        mapped += 1
    log.info("Kerberos: %d eventos de logon mapeados", mapped)


# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("pypsrp").setLevel(logging.WARNING)  # en INFO vuelca cada script
    s = get_settings()
    pool = await db.create_pool()
    winrm_enabled = bool(s.ad_winrm_host and s.ad_winrm_user)
    log.info("adsync iniciado (WinRM %s, DNS interno %s)",
             "activo -> " + s.ad_winrm_host if winrm_enabled else "desactivado",
             s.internal_dns_ip or "no configurado")
    cycle = 0
    try:
        while True:
            try:
                await ptr_pass(pool)
            except Exception:
                log.exception("PTR pass falló")
            if winrm_enabled:
                try:
                    await kerberos_pass(pool, s.ad_poll_seconds)
                except Exception:
                    log.exception("lectura de eventos Kerberos falló (¿WinRM/permisos?)")
                if cycle % DHCP_EVERY_CYCLES == 0:
                    try:
                        await dhcp_pass(pool)
                    except Exception as exc:  # sin traceback: se reintenta cada 3 ciclos
                        log.warning("lectura de leases DHCP falló: %s", str(exc)[:300])
            cycle += 1
            await asyncio.sleep(s.ad_poll_seconds)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
