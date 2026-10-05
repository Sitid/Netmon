# 01 — Arquitectura del sistema

## Topología (ajustada a tu red)

```
                         INTERNET
                            │
                    ┌───────┴────────┐
                    │ WatchGuard      │  ← gateway, rutea las VLANs
                    │ Firebox         │    (router-on-a-stick)
                    └───────┬────────┘
                            │ TRUNK 802.1Q (todas las VLANs)
              puerto X ─────┤
        ┌───────────────────┴────────────────────┐
        │        Switch core HPE/Aruba           │
        │                                        │
        │  mirror: puerto X (trunk al Firebox)   │
        │          └──────────► puerto Y (SPAN)  │
        └───┬––––––––┬–––––––––––––┬─────────┬───┘
            │        │             │         │ puerto Y (destino SPAN)
        VLAN 10   VLAN 20      Switch(es)    │
        usuarios  servidores   Ruckus acceso │
            │        │                       ▼
            │   ┌────┴─────┐    ┌────────────────────────────┐
            │   │ DC / DNS │    │  SERVIDOR NETMON (Debian)  │
            │   │ DHCP AD  │    │                            │
            │   └──────────┘    │  eth0: gestión (con IP)    │
            │                   │  eth1: captura (SPAN, sin  │
         100 PCs                │        IP, promiscuo)      │
                                └────────────────────────────┘
```

**Por qué se espeja el puerto del Firebox:** como el ruteo inter-VLAN lo hace
el firewall, *todo* el tráfico que interesa (internet + inter-VLAN) atraviesa
ese trunk. Un solo mirror del puerto X captura la red completa, con los MACs
reales de cada equipo (por eso el inventario por OUI funciona).

## Flujo de datos

```
   SPAN (eth1, promiscuo)
        │  paquetes crudos
        ▼
 ┌─────────────┐   nDPI clasifica por SNI/DNS/QUIC        ┌──────────┐
 │   ntopng    │──────────── REST API (loopback) ────────►│ netmon-  │
 │ (motor de   │   hosts activos, contadores, desglose    │ collector│
 │  captura)   │   por protocolo de aplicación            └────┬─────┘
 └─────────────┘                                               │ deltas/min
                                                               ▼
 ┌──────────────┐  ping gw/8.8.8.8/DNS   ┌──────────────────────────────┐
 │ netmon-pinger│───────────────────────►│         PostgreSQL           │
 └──────────────┘  rtt/pérdida/estado    │ traffic_min (48 h, 1 min)    │
                                         │ traffic_hour (90 días)       │
 ┌──────────────┐  WinRM solo lectura    │ devices / hostnames / alerts │
 │ netmon-adsync│───────────────────────►│ ip_user / ping_min           │
 └──────────────┘  DC: eventos 4768      └──────────────┬───────────────┘
   PTR al DNS AD      DHCP: leases                      │
                                                        ▼
                        ┌───────────────────────────────────────────┐
                        │      netmon-api (FastAPI, puerto 8080)    │
                        │  REST agregados + WebSocket en vivo (5 s) │
                        │  reportes CSV/PDF + auth admin/kiosco     │
                        └───────────────────┬───────────────────────┘
                                            ▼
                              Dashboard web (pantalla de Sistemas)
```

## Decisiones de diseño y justificación

| Decisión | Alternativa descartada | Motivo |
|---|---|---|
| **ntopng CE + SPAN** como motor de captura | Captura propia con scapy | scapy en Python no sostiene cientos de Mbps ni trae 400+ firmas de aplicación. nDPI clasifica por SNI/DNS sin abrir contenido, que es exactamente el requisito. |
| | NetFlow del WatchGuard (Fireware ≥12.9) | NetFlow da volúmenes por flujo pero **no transporta SNI**: no puede distinguir YouTube de Netflix dentro de HTTPS. Queda documentado como fuente complementaria opcional. |
| **Dashboard custom** sobre la REST API de ntopng | Usar la web de ntopng directa | La GUI de ntopng no hace: mapeo a usuario AD, alertas de dispositivo nuevo con confirmación, reportes gerenciales en castellano, ni modo kiosco. Además CE no retiene agregados consultables a 30 días como los necesitamos. |
| **PostgreSQL** | SQLite | Escriben 3 procesos en paralelo (collector, pinger, adsync) mientras la API lee; con retención y rollups, Postgres evita los dolores de lock de SQLite y ya viene empaquetado. |
| Rollup min→hora en el colector | TimescaleDB / particiones | A esta escala (~100 hosts ≈ 4–6 M filas/mes en `traffic_min` con retención 48 h) sobra; menos piezas que operar. |
| ICMP no privilegiado + CAP_NET_RAW de respaldo | Correr como root | Mínimo privilegio; todos los servicios corren como usuario `netmon` sin shell. |
| ntopng solo en loopback (`--disable-login`) | Exponer ntopng con login | Una sola cara pública (netmon-api) con su propia auth; ntopng queda inaccesible desde la red. |

## Dimensionamiento (100 puestos)

- **Servidor**: 4 vCPU / 8 GB RAM / 60 GB disco sobra. ntopng usa ~1–2 GB con
  cientos de hosts; PostgreSQL con esta retención ocupa < 5 GB.
- **SPAN 1 Gbps**: un puerto espejo de un enlace full-duplex 1 Gbps puede
  oversuscribirse en picos (1 Gbps de bajada + 1 Gbps de subida > 1 Gbps del
  puerto destino). Para estadísticas es aceptable perder paquetes en picos;
  si el switch core lo permite, usar un puerto destino de 2.5/10 G elimina el
  problema.

## Limitaciones conocidas (honestas, para no llevarse sorpresas)

1. **Tráfico inter-VLAN se cuenta dos veces**: un paquete VLAN10→VLAN20 cruza
   el trunk espejado dos veces (ida al firewall y vuelta). El tráfico a
   internet —el que consume el enlace— se cuenta una sola vez y es exacto.
2. **Tráfico local dentro de una misma VLAN** (PC a PC del mismo switch de
   acceso) no pasa por el trunk y no se ve. Para consumo de internet no
   importa; para inventario casi siempre alcanza porque todos los equipos
   hablan tarde o temprano con el gateway/DC.
3. **ESNI/ECH**: si un navegador usa Encrypted Client Hello, el SNI viaja
   cifrado y nDPI cae a clasificar por DNS/IP (algo menos preciso). Hoy es
   minoritario.
4. **Dispositivos solo-Azure AD** (no unidos al dominio on-prem) no generan
   eventos Kerberos en el DC: aparecen con hostname pero sin usuario.
