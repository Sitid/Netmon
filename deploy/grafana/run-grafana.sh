#!/usr/bin/env bash
# Recrea el contenedor de Grafana con la configuración correcta.
#
# CLAVE: Grafana DEBE ir conectado a la red interna de Wazuh (single-node_default),
# porque el datasource apunta a https://wazuh.indexer:9200 (nombre que sólo resuelve
# dentro de esa red). Si se recrea con `docker run` SIN --network, los paneles
# muestran "No data" con error de DNS: "lookup wazuh.indexer ... no such host".
#
# La contraseña de admin NO se hardcodea: se toma de GF_SECURITY_ADMIN_PASSWORD.
# Uso:
#   GF_SECURITY_ADMIN_PASSWORD='tu-clave' ./run-grafana.sh
set -euo pipefail

: "${GF_SECURITY_ADMIN_PASSWORD:?Definí GF_SECURITY_ADMIN_PASSWORD antes de correr}"

NET=single-node_default          # red del stack de Wazuh (indexer/manager/dashboard)
HOST_IP=10.10.11.59

docker rm -f grafana 2>/dev/null || true

docker run -d --name grafana --restart unless-stopped \
  --network "$NET" \
  -p 3005:3000 \
  -e GF_SERVER_ROOT_URL="http://${HOST_IP}:3005" \
  -e GF_ANALYTICS_REPORTING_ENABLED=false \
  -e GF_ANALYTICS_CHECK_FOR_UPDATES=false \
  -e GF_INSTALL_PLUGINS=grafana-opensearch-datasource \
  -e GF_SECURITY_ADMIN_PASSWORD="$GF_SECURITY_ADMIN_PASSWORD" \
  -v /opt/grafana/provisioning:/etc/grafana/provisioning \
  -v grafana-storage:/var/lib/grafana \
  -v /opt/grafana/dashboards:/var/lib/grafana/dashboards \
  grafana/grafana-oss:11.2.0

echo "Grafana recreado en la red ${NET}."
echo "Verificar salud:   curl -s http://localhost:3005/api/health"
echo "Verificar red:     docker exec grafana getent hosts wazuh.indexer"
