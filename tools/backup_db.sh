#!/usr/bin/env bash
# Backup diario de la base netmon (lo corre netmon-backup.timer como postgres).
# Formato custom de pg_dump (comprimido, restaurable tabla por tabla):
#   pg_restore -d netmon_restaurada /var/backups/netmon/netmon-AAAAMMDD-HHMM.dump
# Guarda los últimos KEEP backups.
set -euo pipefail
DIR=/var/backups/netmon
KEEP=${NETMON_BACKUP_KEEP:-14}
OUT="$DIR/netmon-$(date +%Y%m%d-%H%M).dump"

pg_dump -Fc -d netmon -f "$OUT.tmp"
pg_restore --list "$OUT.tmp" > /dev/null          # verifica que el dump es legible
mv "$OUT.tmp" "$OUT"
chmod 640 "$OUT"
ls -1t "$DIR"/netmon-*.dump | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup OK: $OUT ($(du -h "$OUT" | cut -f1))"
