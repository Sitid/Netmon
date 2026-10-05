# Grafana — SOC (netmon)

Grafana OSS 11.2 en Docker, puerto 3005. Datasource `Wazuh-OpenSearch`
(grafana-opensearch-datasource) sobre `wazuh-alerts-*`, campo de tiempo `@timestamp`.

## Tableros provisionados (carpeta "SOC")
`provisioning/dashboards/netmon.yml` -> `/etc/grafana/provisioning/dashboards/netmon.yml`
carga los `.json` de `dashboards/` (montados en `/var/lib/grafana/dashboards`).
Quedan de solo lectura en la UI (allowUiUpdates:false): se editan acá y se
recargan solos (30 s) o con `docker restart grafana`.

- `dashboards/soc-triage.json` — Triage de alertas: estado por nivel de Wazuh
  (12+ críticas, 8-11 altas, 5-7 medias), serie por nivel, últimas alertas,
  top reglas/grupos, top IP externas de origen y equipos internos de destino.

`reference/suricata-net.json` es copia del tablero de Suricata creado por API
(no provisionado, para versionarlo).

Niveles de Wazuh: 0-3 informativo, 4-7 bajo/medio, 8-11 alto, 12-15 crítico.

## Recrear el contenedor (IMPORTANTE: red)

Grafana **debe** correr conectado a la red interna de Wazuh
(`single-node_default`), porque el datasource apunta a `https://wazuh.indexer:9200`,
un nombre que sólo resuelve dentro de esa red. Si se recrea con `docker run` sin
`--network single-node_default`, todos los paneles muestran **"No data"** con el
error `lookup wazuh.indexer ... no such host` en los logs.

Usar el script versionado (no hardcodea la clave):
```bash
GF_SECURITY_ADMIN_PASSWORD='tu-clave' deploy/grafana/run-grafana.sh
```
Si el contenedor ya existe y quedó sin la red (p. ej. tras un `docker run` manual):
```bash
docker network connect single-node_default grafana
```
El estado (dashboards, datasource) vive en el volumen `grafana-storage` y en
`/opt/grafana`, así que recrear no pierde nada.
