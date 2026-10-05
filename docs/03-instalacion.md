# 03 — Instalación del servidor

## Requisitos

- Debian 12 o Ubuntu 22.04/24.04 con dos NICs:
  - `eth0` (o como se llame): **gestión**, con IP fija en la VLAN de servidores.
  - `eth1`: **captura**, cableada al puerto SPAN, **sin dirección IP**.
- 4 vCPU / 8 GB RAM / 60 GB de disco (ver dimensionamiento en doc 01).
- Salida a internet solo para instalar paquetes (después puede vivir aislado).

Identificá el nombre real de la NIC de captura con `ip -br link`
(suele ser `enp2s0`, `ens19`, etc.).

## Instalación

```bash
# como root, desde la carpeta del repo netmon/
chmod +x install.sh
NETMON_CAPTURE_IFACE=enp2s0 ./install.sh
```

El instalador imprime al final la **clave admin** y el **token kiosco**
generados (quedan en `/etc/netmon/netmon.env`).

## Configuración post-instalación (obligatoria)

1. Editar `/etc/netmon/netmon.env`:
   - `NETMON_LOCAL_NETWORKS` → tus VLANs reales (ej.: `192.168.10.0/24,192.168.20.0/24`).
     **De esto depende qué hosts se contabilizan.**
   - `NETMON_GATEWAY_IP` → IP interna del WatchGuard.
   - `NETMON_INTERNAL_DNS_IP` → IP del DC/DNS del dominio.
2. Reflejar las mismas redes en `/etc/ntopng/ntopng.conf` (línea `--local-networks=`).
3. Reiniciar:
   ```bash
   systemctl restart ntopng netmon-collector netmon-pinger netmon-api
   ```
4. (Opcional) Integración AD: completar `NETMON_AD_*` según doc 04 y luego
   `systemctl enable --now netmon-adsync`.

## Verificación

```bash
systemctl status netmon-api netmon-collector netmon-pinger --no-pager
journalctl -u netmon-collector -n 30 --no-pager   # debe loguear "ciclo ok: N hosts..."

# ntopng ve tráfico:
curl -s 'http://127.0.0.1:3000/lua/rest/v2/get/interface/data.lua?ifid=0' | head -c 400

# la API responde:
curl -s "http://127.0.0.1:8080/api/summary?token=$(grep -oP 'NETMON_KIOSK_TOKEN=\K.*' /etc/netmon/netmon.env)"
```

Dashboard: `http://IP-del-servidor:8080` → botón **Ingresar** con la clave admin.

## Pantalla kiosco en Sistemas

URL para la TV/monitor (solo lectura, sin login):

```
http://IP-del-servidor:8080/kiosk?token=EL_TOKEN_KIOSCO
```

Por defecto **rota entre Resumen y Estado de red cada 30 segundos**; se ajusta
con `&rotar=60` (segundos) o se desactiva con `&rotar=0`.

En una mini-PC con Chromium:

```bash
chromium --kiosk --noerrdialogs --disable-session-crashed-bubble \
         "http://IP-del-servidor:8080/kiosk?token=TOKEN"
```

El dashboard se auto-actualiza (WebSocket: realtime cada 2 s, estado cada 5 s,
agregados cada 30 s) y reconecta solo. Si pierde conexión muestra un banner
con "último dato: hace X s" y atenúa los paneles — nunca datos viejos que
parezcan actuales.

## Reportes

- Desde el dashboard (sesión admin): botón **Reportes** → día/semana, CSV o PDF.
- Automático: `netmon-report.timer` deja cada lunes 07:00 el CSV+PDF de la
  semana anterior en `/opt/netmon/reports/`.
- Manual/cron:
  ```bash
  sudo -u netmon /opt/netmon/venv/bin/python /opt/netmon/tools/make_report.py \
       --range week --format both
  ```

## Operación

| Tarea | Comando |
|---|---|
| Logs en vivo | `journalctl -u netmon-collector -f` |
| Backup de la DB | `sudo -u postgres pg_dump netmon | gzip > netmon_$(date +%F).sql.gz` |
| Cambiar clave admin | editar `netmon.env` → `systemctl restart netmon-api` |
| Actualizar código | copiar repo nuevo, re-ejecutar `install.sh` (no pisa `netmon.env`) |
| Actualizar firmas nDPI | `apt upgrade ntopng` (o migrar al repo packages.ntop.org) |

## Seguridad del propio servidor

- El dashboard va por HTTP en la LAN de servidores. Si se quiere HTTPS,
  poner un nginx/caddy adelante con certificado interno (10 líneas de config).
- Restringí el puerto 8080 a la VLAN de Sistemas con el propio WatchGuard o
  con nftables en el servidor.
- La NIC de captura no tiene IP: no es alcanzable desde la red espejada.
