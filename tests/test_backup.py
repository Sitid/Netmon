"""Backup de la base (auditoría H13). Correr en netmon-srv como netmon o root:

    python -m pytest -q tests/test_backup.py
"""

import subprocess
import time
from pathlib import Path

BACKUP_DIR = Path("/var/backups/netmon")
MAX_AGE_H = 26          # diario + margen


def _dumps():
    return sorted(BACKUP_DIR.glob("netmon-*.dump"), key=lambda p: p.stat().st_mtime)


def test_timer_de_backup_habilitado():
    r = subprocess.run(["systemctl", "is-enabled", "netmon-backup.timer"],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "enabled", r.stdout + r.stderr


def test_hay_un_backup_reciente():
    dumps = _dumps() if BACKUP_DIR.exists() else []
    assert dumps, f"no hay backups en {BACKUP_DIR}"
    age_h = (time.time() - dumps[-1].stat().st_mtime) / 3600
    assert age_h < MAX_AGE_H, f"último backup de hace {age_h:.1f} h"


def test_el_backup_se_puede_restaurar():
    """pg_restore --list lee el catálogo completo: un dump truncado falla."""
    dumps = _dumps()
    assert dumps
    r = subprocess.run(["pg_restore", "--list", str(dumps[-1])], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]
    assert "TABLE DATA public traffic_hour" in r.stdout
    assert "TABLE DATA public devices" in r.stdout
