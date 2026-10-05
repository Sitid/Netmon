# netmon — Monitoreo de red para PyME (~100 puestos, AD)

Sistema de monitoreo de tráfico sobre **port mirroring (SPAN)** pensado para
una red con Active Directory, switches HPE/Aruba + Ruckus y firewall
WatchGuard. Captura con **ntopng/nDPI** (clasificación por SNI/DNS, sin
inspección de contenido) y construye encima un dashboard propio en tiempo
real con histórico en PostgreSQL, integración AD y reportes gerenciales.

## Funcionalidades

- **SPA estilo NOC**: sidebar con 6 secciones (Resumen, Consumo por IP,
  Dispositivos, Estado de red, Reportes, Configuración), atajos de teclado
  1-6, tema oscuro/claro, un solo acento cian (verde/amarillo/rojo reservados
  para estados), cifras en fuente monoespaciada tabular.
- **Consumo por equipo**: tabla ordenable con búsqueda y paginación — IP /
  hostname / usuario AD / Mbps en vivo / bytes, vistas 5 min / 1 h / 24 h.
  Clic en una fila → **detalle de host**: histórico con zoom de rango
  (1 h/24 h/7 d/30 d), apps nDPI, contactos internos/externos, puertos,
  países de destino (GeoIP DB-IP) y OS estimado (fingerprint pasivo de ntopng).
- **Flujos activos** (pestaña en Consumo): origen/destino/puerto/L4/app
  L7/duración/bytes/Mbps con filtros y badges de larga duración y alto volumen.
- **Clasificación L7 por categoría** (Streaming, Social, Productividad,
  Sistema, P2P, Desconocido) vía nDPI (SNI de TLS + DNS + QUIC), **editable
  desde Configuración** por aplicación.
- **Inventario de dispositivos**: MAC, fabricante (OUI IEEE), hostname,
  primera/última vez visto, alerta y confirmación de dispositivos nuevos.
- **Estado de red**: uptime % por target, latencia/pérdida (gateway WatchGuard,
  internet, DNS interno), gráfico realtime de la interfaz (Mbps + pps cada
  2 s), historial de caídas.
- **Alertas configurables desde la UI**: cuota GB/día por host, categoría
  prohibida (ej. P2P), dispositivo nuevo, conexión a IP de mala reputación
  (FireHOL level1). Severidades info/warning/critical, campana con contador,
  mail + webhook JSON (para Telegram/Teams vía relay).
- **Histórico tipo RRD**: 1 min × 48 h → 5 min × 14 días → 1 h × 90 días
  (rollups y retención automáticos) + buffer realtime en memoria.
- **Reportes** CSV/PDF de top consumidores por día/semana (vista Reportes +
  timer semanal automático con listado de generados).
- **Modo kiosco** (`/kiosk?token=...`): sin navegación, tipografía ampliada,
  rotación Resumen ↔ Estado de red cada 30 s (desactivable con `?rotar=0`),
  banner de desconexión con "último dato: hace X s".

## Estructura del repo

```
netmon/
├── install.sh                ← instalador Debian/Ubuntu (root)
├── schema.sql                ← esquema PostgreSQL
├── requirements.txt
├── .env.example              ← plantilla de configuración
├── netmon/                   ← paquete Python
│   ├── config.py             ← settings (env NETMON_*)
│   ├── db.py                 ← pool asyncpg + consultas compartidas
│   ├── ntopng_client.py      ← cliente REST API v2 de ntopng
│   ├── collector.py          ← servicio: tráfico/categorías/inventario/rollups
│   ├── pinger.py             ← servicio: latencia, pérdida, estado de enlaces
│   ├── adsync.py             ← servicio: PTR + DHCP leases + logons Kerberos
│   ├── api.py                ← servicio: FastAPI REST + WebSocket + frontend
│   ├── categories.py         ← mapeo nDPI → categorías (defaults; overrides en DB)
│   ├── alerting.py           ← alertas: DB + mail + webhook + dedupe diario
│   ├── blocklist.py / geo.py ← reputación FireHOL / GeoIP DB-IP
│   ├── reports.py            ← generación CSV/PDF
│   ├── oui.py / notify.py    ← fabricante por MAC / mail y webhook
├── frontend/                 ← SPA (index + style.css + js/core.js + js/views.js)
├── systemd/                  ← units de servicios + timers (reportes, feeds)
├── tools/                    ← make_report.py (CLI) + update_feeds.sh
└── docs/
    ├── 01-arquitectura.md    ← diagrama y decisiones de diseño
    ├── 02-port-mirroring.md  ← comandos SPAN Aruba/Ruckus + NetFlow WatchGuard
    ├── 03-instalacion.md     ← paso a paso del servidor
    ├── 04-integracion-ad.md  ← cuenta de servicio, WinRM, DHCP, PTR
    ├── 05-privacidad-legal.md← qué se monitorea + plantilla de política interna
    ├── 06-paridad-ntopng.md  ← decisiones build vs. reuse por función
    └── 07-lab-vmware.md      ← laboratorio en VMware Workstation + export a ESXi
```

## Puesta en marcha (resumen)

1. **Switch**: espejar el puerto del WatchGuard hacia el puerto del servidor
   (comandos exactos en [docs/02](docs/02-port-mirroring.md)).
2. **Servidor**: `sudo NETMON_CAPTURE_IFACE=eth1 ./install.sh`, después editar
   `/etc/netmon/netmon.env` (VLANs, gateway, DNS) — [docs/03](docs/03-instalacion.md).
3. **AD** (opcional): cuenta `svc-netmon` + WinRM → `systemctl enable --now
   netmon-adsync` — [docs/04](docs/04-integracion-ad.md).
4. **RRHH/Legal**: comunicar la política de uso aceptable **antes** de usar
   reportes nominales — [docs/05](docs/05-privacidad-legal.md).

Dashboard: `http://servidor:8080` · Kiosco: `http://servidor:8080/kiosk?token=...`

## Servicios

| Unit | Función |
|---|---|
| `netmon-capture-iface` | prepara la NIC SPAN (promiscuo, sin offloads, MTU 1600) |
| `ntopng` | captura + nDPI (solo loopback) |
| `netmon-collector` | ntopng → PostgreSQL, inventario, reglas de alerta, rollups |
| `netmon-pinger` | latencia/pérdida/estado + alertas |
| `netmon-adsync` | hostname y usuario AD (PTR, DHCP, Kerberos 4768) |
| `netmon-api` | REST + WebSocket + SPA (puerto 8080) |
| `netmon-report.timer` | CSV+PDF semanal en `/opt/netmon/reports` |
| `netmon-feeds.timer` | actualiza blocklist FireHOL (semanal) y GeoIP DB-IP (mensual) |
