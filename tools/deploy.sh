#!/usr/bin/env bash
# Despliega el código del repo en /opt/netmon (ejecutar como root desde la raíz del repo).
#
#   sudo tools/deploy.sh [servicio ...]      p. ej.: sudo tools/deploy.sh netmon-api
#
# 1. Respaldo de lo instalado en /opt/netmon/backups/deploy-AAAAMMDD-HHMMSS/
# 2. Copia netmon/, frontend/ (sin vendor/), tools/ y schema.sql
# 3. Versión de caché del frontend (?v=) = commit actual, para que el navegador
#    no siga usando JS viejo
# 4. Compila el Python instalado y reinicia sólo los servicios indicados
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/netmon
[[ $EUID -eq 0 ]] || { echo "Ejecutar como root (sudo)"; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
BK="$DEST/backups/deploy-$STAMP"
mkdir -p "$BK"
cp -a "$DEST/netmon" "$DEST/tools" "$DEST/schema.sql" "$BK/"
mkdir -p "$BK/frontend" && cp -a "$DEST/frontend/index.html" "$DEST/frontend/style.css" "$DEST/frontend/js" "$BK/frontend/"
echo "respaldo: $BK"

install -o netmon -g netmon -m 644 "$REPO"/netmon/*.py "$DEST/netmon/"
install -o netmon -g netmon -m 644 "$REPO"/frontend/index.html "$REPO"/frontend/style.css "$DEST/frontend/"
install -o netmon -g netmon -m 644 "$REPO"/frontend/js/*.js "$DEST/frontend/js/"
for f in "$REPO"/tools/*.sh; do
    # backup_db.sh corre como postgres: va a un directorio de root, fuera de
    # /opt/netmon (que es de netmon y permitiría reemplazar el archivo)
    if [[ "$(basename "$f")" == backup_db.sh ]]; then
        install -o root -g root -m 755 "$f" /usr/local/sbin/netmon-backup-db
    else
        install -o netmon -g netmon -m 755 "$f" "$DEST/tools/"
    fi
done
install -o netmon -g netmon -m 644 "$REPO"/tools/*.py "$DEST/tools/"
install -o netmon -g netmon -m 644 "$REPO"/schema.sql "$DEST/schema.sql"

VER=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "$STAMP")
sed -i -E "s/\?v=[A-Za-z0-9]+/?v=$VER/g" "$DEST/frontend/index.html"

sudo -u netmon "$DEST/venv/bin/python" -m py_compile "$DEST"/netmon/*.py
echo "compila OK (versión $VER)"

for svc in "$@"; do
    systemctl restart "$svc"
    echo "reiniciado $svc"
done
