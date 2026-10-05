"""Resolución de fabricante por OUI (prefijo de MAC).

Usa el CSV oficial de IEEE (https://standards-oui.ieee.org/oui/oui.csv) que el
instalador descarga a /opt/netmon/data/oui.csv. Si el archivo no existe, todo
sigue funcionando y el fabricante queda vacío.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger("netmon.oui")

_db: dict[str, str] | None = None


def _load(path: str) -> dict[str, str]:
    table: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        log.warning("OUI CSV no encontrado en %s (el fabricante quedará vacío)", path)
        return table
    try:
        with p.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Formato IEEE: Registry,Assignment,Organization Name,Organization Address
                assignment = (row.get("Assignment") or "").strip().upper()
                org = (row.get("Organization Name") or "").strip()
                if len(assignment) == 6 and org:
                    table[assignment] = org
        log.info("OUI DB cargada: %d prefijos", len(table))
    except Exception:
        log.exception("No se pudo parsear el OUI CSV")
    return table


def vendor_for_mac(mac: str, oui_csv_path: str) -> str:
    """Devuelve el fabricante para una MAC ('aa:bb:cc:dd:ee:ff') o ''. Carga lazy."""
    global _db
    if _db is None:
        _db = _load(oui_csv_path)
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    return _db.get(prefix, "")
