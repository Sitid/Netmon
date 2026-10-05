#!/usr/bin/env bash
# Actualiza los feeds externos de netmon (lo dispara netmon-feeds.timer):
#   - FireHOL level1 (reputación de IPs) — semanal
#   - DB-IP Country Lite (GeoIP, sin registro) — la URL cambia por mes
# Descarga a archivo temporal y reemplaza atómico: si falla la descarga,
# queda la versión anterior. El colector/API recargan solos por mtime.
set -euo pipefail

DATA_DIR="${NETMON_DATA_DIR:-/opt/netmon/data}"
mkdir -p "$DATA_DIR"

echo "== FireHOL level1 =="
TMP=$(mktemp)
if curl -fsSL --max-time 120 \
    "https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_level1.netset" \
    -o "$TMP" && [ -s "$TMP" ]; then
    mv "$TMP" "$DATA_DIR/firehol_level1.netset"
    echo "   OK ($(grep -vc '^#' "$DATA_DIR/firehol_level1.netset") entradas)"
else
    rm -f "$TMP"
    echo "   FALLO: se conserva la lista anterior" >&2
fi

echo "== DB-IP Country Lite =="
YM=$(date +%Y-%m)
TMP=$(mktemp)
if curl -fsSL --max-time 300 \
    "https://download.db-ip.com/free/dbip-country-lite-${YM}.mmdb.gz" \
    -o "$TMP.gz" && gunzip -f "$TMP.gz" && [ -s "$TMP" ]; then
    mv "$TMP" "$DATA_DIR/dbip-country-lite.mmdb"
    echo "   OK (edición $YM)"
else
    rm -f "$TMP" "$TMP.gz" 2>/dev/null || true
    echo "   FALLO: se conserva la base anterior (o países desactivados)" >&2
fi

chown -R netmon:netmon "$DATA_DIR" 2>/dev/null || true
echo "Feeds actualizados."
