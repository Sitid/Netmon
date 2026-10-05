#!/usr/bin/env bash
# Chequeos rápidos antes de desplegar (no necesitan base ni ntopng):
#   tools/check.sh [python]      (python con pytest y ruff; default: python3)
# Los tests contra una base temporal y contra producción se corren aparte:
#   sudo -u postgres PY -m pytest tests/test_db.py
#   sudo -u netmon bash -c 'set -a; . /etc/netmon/netmon.env; set +a; PY -m pytest tests/test_invariantes.py'
set -euo pipefail
PY=${1:-python3}
cd "$(dirname "$0")/.."
"$PY" -m ruff check netmon tools tests
"$PY" -m pytest tests/test_unit.py tests/test_build.py
echo "check OK"
