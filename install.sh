#!/usr/bin/env bash
# =============================================================================
# netmon - instalador para Debian 12 / Ubuntu 22.04+ (ejecutar como root)
#
#   sudo NETMON_CAPTURE_IFACE=eth1 ./install.sh
#
# Qué hace:
#   1. Instala dependencias: ntopng, redis, PostgreSQL, Python venv, ethtool.
#   2. Crea usuario de sistema 'netmon', /opt/netmon y el virtualenv.
#   3. Crea la base 'netmon' con clave aleatoria y aplica schema.sql.
#   4. Configura ntopng para escuchar SOLO en loopback sobre la interfaz SPAN.
#   5. Descarga la base OUI de IEEE y Chart.js (si hay internet).
#   6. Instala y habilita las units de systemd.
#
# Después de correr: editar /etc/netmon/netmon.env (redes locales, gateway,
# claves) y reiniciar los servicios. Ver docs/03-instalacion.md.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR=/opt/netmon
ENV_FILE=/etc/netmon/netmon.env
CAPTURE_IFACE="${NETMON_CAPTURE_IFACE:-eth1}"

[[ $EUID -eq 0 ]] || { echo "Ejecutar como root (sudo)"; exit 1; }

echo "== [1/7] Paquetes del sistema =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    redis-server postgresql postgresql-client \
    python3 python3-venv python3-pip ethtool curl ca-certificates openssl

# ntopng: en Debian 12 está en los repos oficiales; en Debian 13 (trixie) ya
# no viene en la distro y se usa el repo oficial de ntop (packages.ntop.org),
# que además trae firmas nDPI más frescas.
if ! apt-get install -y --no-install-recommends ntopng; then
    echo "   ntopng no está en los repos de la distro; agrego packages.ntop.org…"
    CODENAME=$(. /etc/os-release; echo "$VERSION_CODENAME")
    curl -fsSL --max-time 60 -o /tmp/apt-ntop.deb \
        "https://packages.ntop.org/apt/${CODENAME}/all/apt-ntop.deb"
    apt-get install -y /tmp/apt-ntop.deb
    apt-get update
    apt-get install -y --no-install-recommends ntopng
fi

echo "== [2/7] Usuario y árbol de archivos =="
id -u netmon &>/dev/null || useradd --system --home-dir $INSTALL_DIR --shell /usr/sbin/nologin netmon
mkdir -p $INSTALL_DIR/{data,reports} /etc/netmon
cp -r "$REPO_DIR/netmon" "$REPO_DIR/frontend" "$REPO_DIR/tools" "$REPO_DIR/schema.sql" $INSTALL_DIR/

echo "== [3/7] Virtualenv Python =="
python3 -m venv $INSTALL_DIR/venv
$INSTALL_DIR/venv/bin/pip install --quiet --upgrade pip
$INSTALL_DIR/venv/bin/pip install --quiet -r "$REPO_DIR/requirements.lock"

echo "== [4/7] PostgreSQL =="
DB_PASS=$(openssl rand -hex 16)
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='netmon'" | grep -q 1; then
    sudo -u postgres psql -c "CREATE ROLE netmon LOGIN PASSWORD '$DB_PASS'"
    sudo -u postgres psql -c "CREATE DATABASE netmon OWNER netmon"
else
    echo "   rol netmon ya existe: conservo la clave actual (no piso netmon.env)"
    DB_PASS=""
fi
sudo -u postgres psql -d netmon -f "$REPO_DIR/schema.sql"
sudo -u postgres psql -d netmon -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO netmon; \
    GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO netmon"

echo "== [5/7] Configuración =="
if [[ ! -f $ENV_FILE ]]; then
    cp "$REPO_DIR/.env.example" $ENV_FILE
    ADMIN_PASS=$(openssl rand -base64 12 | tr -d '/+=')
    KIOSK_TOK=$(openssl rand -hex 24)
    SECRET=$(openssl rand -hex 32)
    sed -i \
      -e "s|^NETMON_DB_DSN=.*|NETMON_DB_DSN=postgresql://netmon:${DB_PASS}@127.0.0.1:5432/netmon|" \
      -e "s|^NETMON_ADMIN_PASSWORD=.*|NETMON_ADMIN_PASSWORD=${ADMIN_PASS}|" \
      -e "s|^NETMON_KIOSK_TOKEN=.*|NETMON_KIOSK_TOKEN=${KIOSK_TOK}|" \
      -e "s|^NETMON_SECRET_KEY=.*|NETMON_SECRET_KEY=${SECRET}|" \
      -e "s|^NETMON_CAPTURE_IFACE=.*|NETMON_CAPTURE_IFACE=${CAPTURE_IFACE}|" \
      $ENV_FILE
    chmod 640 $ENV_FILE && chgrp netmon $ENV_FILE
    echo "   -> Clave admin dashboard : $ADMIN_PASS"
    echo "   -> Token kiosco          : $KIOSK_TOK"
    echo "   (quedan guardados en $ENV_FILE)"
else
    echo "   $ENV_FILE ya existe, no lo toco."
fi

# ntopng: interfaz SPAN, web solo en loopback (la API custom es la cara pública)
if [[ ! -f /etc/ntopng/ntopng.conf.netmon-bak ]] && [[ -f /etc/ntopng/ntopng.conf ]]; then
    cp /etc/ntopng/ntopng.conf /etc/ntopng/ntopng.conf.netmon-bak
fi
LOCAL_NETS=$(grep -oP '^NETMON_LOCAL_NETWORKS=\K.*' $ENV_FILE || echo "192.168.0.0/16")
mkdir -p /etc/ntopng
cat > /etc/ntopng/ntopng.conf <<EOF
# Generado por netmon install.sh
-i=${CAPTURE_IFACE}
-w=127.0.0.1:3000
--local-networks=${LOCAL_NETS}
# Sin login: escucha SOLO en loopback; el acceso externo pasa por netmon-api.
--disable-login=1
--community
EOF
touch /etc/ntopng/ntopng.start 2>/dev/null || true

# ICMP sin privilegios para el pinger
cat > /etc/sysctl.d/90-netmon.conf <<'EOF'
net.ipv4.ping_group_range = 0 2147483647
EOF
sysctl --system >/dev/null

echo "== [6/7] Datos auxiliares (OUI + Chart.js + fuentes + feeds) =="
curl -fsSL --max-time 60 -o $INSTALL_DIR/data/oui.csv \
    https://standards-oui.ieee.org/oui/oui.csv \
    || echo "   AVISO: no pude bajar oui.csv (sin internet). Bajalo a mano después."
mkdir -p $INSTALL_DIR/frontend/vendor/fonts
curl -fsSL --max-time 60 -o $INSTALL_DIR/frontend/vendor/chart.umd.min.js \
    https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js \
    || echo "   AVISO: no pude bajar Chart.js. El dashboard mostrará tablas sin gráficos."

# Fuentes locales (sin CDN en runtime): Inter para UI, IBM Plex Mono para cifras
FONTS_BASE="https://cdn.jsdelivr.net/npm"
declare -A FONTS=(
  ["inter-400.woff2"]="@fontsource/inter@5.0.18/files/inter-latin-400-normal.woff2"
  ["inter-600.woff2"]="@fontsource/inter@5.0.18/files/inter-latin-600-normal.woff2"
  ["inter-800.woff2"]="@fontsource/inter@5.0.18/files/inter-latin-800-normal.woff2"
  ["plexmono-400.woff2"]="@fontsource/ibm-plex-mono@5.0.13/files/ibm-plex-mono-latin-400-normal.woff2"
  ["plexmono-600.woff2"]="@fontsource/ibm-plex-mono@5.0.13/files/ibm-plex-mono-latin-600-normal.woff2"
)
for f in "${!FONTS[@]}"; do
    curl -fsSL --max-time 60 -o "$INSTALL_DIR/frontend/vendor/fonts/$f" \
        "$FONTS_BASE/${FONTS[$f]}" \
        || echo "   AVISO: no pude bajar $f (se usa la fuente del sistema)."
done

# Feeds: blocklist FireHOL + GeoIP DB-IP (los mantiene netmon-feeds.timer)
bash "$REPO_DIR/tools/update_feeds.sh" || echo "   AVISO: feeds no descargados (reintenta el timer)."

chown -R netmon:netmon $INSTALL_DIR

echo "== [7/7] Servicios systemd =="
cp "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now netmon-capture-iface.service
systemctl enable --now redis-server postgresql ntopng
systemctl enable --now netmon-collector netmon-pinger netmon-api
systemctl enable --now netmon-report.timer netmon-feeds.timer
# netmon-adsync se habilita a mano tras configurar NETMON_AD_* :
#   systemctl enable --now netmon-adsync

echo
echo "=============================================================="
echo " Instalación completa."
echo "  1. Editá $ENV_FILE:"
echo "     - NETMON_LOCAL_NETWORKS (tus VLANs reales)"
echo "     - NETMON_GATEWAY_IP / NETMON_INTERNAL_DNS_IP"
echo "  2. Reflejá las redes en /etc/ntopng/ntopng.conf (--local-networks)"
echo "  3. systemctl restart ntopng netmon-collector netmon-pinger netmon-api"
echo "  4. Dashboard:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
echo "     Kiosco:     http://.../kiosk?token=<NETMON_KIOSK_TOKEN>"
echo "  5. Verificá que ntopng ve tráfico del SPAN:"
echo "     curl -s 'http://127.0.0.1:3000/lua/rest/v2/get/interface/data.lua?ifid=0' | head"
echo "=============================================================="
