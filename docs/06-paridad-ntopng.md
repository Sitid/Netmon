# 06 — Paridad funcional con ntopng: decisiones build vs. reuse

Criterio aplicado: **todo lo que ntopng ya calcula bien se consume por su REST
API; solo se implementa lo que ntopng no da** (persistencia larga, AD, reglas
de negocio, UI). Prioridad: mantenible por una sola persona.

| # | Función | Decisión | Implementación |
|---|---|---|---|
| 1 | Vista de hosts | Reusar ntopng + PG propio | Lista y detalle desde la API de ntopng (incluye `os` por fingerprint pasivo, flujos activos, throughput); histórico desde nuestras tablas. Países con **DB-IP Lite** (mmdb gratuito sin registro; GeoLite2 exige cuenta). Local/remoto por `NETMON_LOCAL_NETWORKS`. |
| 2 | Flujos activos | Proxy en vivo, sin persistir | `GET /api/flows` normaliza `get/flow/active.lua`. Persistir flujos multiplicaría la DB ×100 (ClickHouse quedó fuera de alcance a propósito). Filtros por host/app/puerto; badges LARGA (>5 min) y VOLUMEN (>50 MB) al renderizar. |
| 3 | Detección L7 | nDPI **vía ntopng**, no bindings | Los bindings de nDPI obligan a escribir captura+tracking de flujos en Python (GIL, mantenimiento propio). Embebido en ntopng, las firmas llegan por `apt upgrade`. Nuestro aporte: mapeo app→categoría con taxonomía Streaming/Social/Productividad/Sistema/P2P/Desconocido, editable en Configuración (tabla `category_map`). |
| 4 | Timeseries tipo RRD | Rollups en PostgreSQL, no RRDtool | Tiers: **memoria 2 s × 30 min** (realtime, sin disco) → **1 min × 48 h** → **5 min × 14 días** → **1 h × 90 días**. Por-app: `app_min`/`app_hour` a nivel red (por host+app explotaría). RRDtool descartado: segundo storage para backupear y API antigua. |
| 5 | Alertas | Propio (extendido) | Tabla `alert_rules` editable desde la UI: cuota GB/día, categoría prohibida (P2P dispara alerta), dispositivo nuevo, y reputación con **FireHOL level1** (semanal, sin registro). Dedupe: 1 alerta por clave por día. Notifica mail + **webhook JSON genérico** (`NETMON_WEBHOOK_URL`, apto para relay a Telegram/Teams). Campana con contador + histórico consultable. |
| 6 | Interfaz / top talkers | Mixto | Throughput y pps de `interface/data.lua`, muestreados cada 2 s a un ring buffer en el proceso API y empujados por WebSocket → gráfico realtime fluido en Estado de red. Top talkers/apps ya resueltos por el colector. |

## Fuera de alcance (según lo acordado)

SNMP polling, ML de comportamiento, ClickHouse/MySQL a gran escala,
multi-tenant y scripting Lua. Si alguno se vuelve necesario, la señal es
migrar a ntopng Enterprise en lugar de crecer este proyecto.

## Endpoints agregados en esta fase

```
GET  /api/hosts                      hosts activos (locales y remotos) enriquecidos
GET  /api/hosts/{ip}                 detalle: OS, apps, contactos, puertos, países
GET  /api/flows?host=&l7=&port=      flujos activos con filtros
GET  /api/apps?range=                top de aplicaciones + serie temporal
GET  /api/timeline?range=&ip=        serie por tier (1h/24h/7d/30d), total o por host
GET  /api/interface/realtime         buffer del gráfico realtime (2 s)
GET  /api/ping/summary?range=        uptime %, rtt, caídas por target
GET/POST /api/rules[...]             reglas de alerta (admin)
GET/POST/DELETE /api/category-map    mapeo app→categoría (admin)
GET  /api/reports/list, /file/{n}    reportes generados (admin)
GET  /api/config-info                estado de la config (admin)
GET  /kiosk                          modo pantalla fija (rotación 30 s por defecto)
```
