"""GeoIP por país usando la base DB-IP Country Lite (mmdb, gratuita y sin
registro; se actualiza mensualmente vía netmon-feeds.timer).

Si la base no está, todo devuelve "" y la UI simplemente no muestra países.
La base se recarga sola si el archivo cambia (actualización mensual) o si
aparece después del arranque (auditoría H25).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("netmon.geo")

_reader = None
_path: str | None = None
_mtime: float | None = None


def _reset() -> None:
    global _reader, _path, _mtime
    if _reader is not None:
        try:
            _reader.close()
        except Exception:
            pass
    _reader, _path, _mtime = None, None, None


def _ensure(mmdb_path: str) -> None:
    """Abre (o reabre) la base si cambió el archivo; sin archivo, sin base."""
    global _reader, _path, _mtime
    try:
        mtime = os.stat(mmdb_path).st_mtime
    except OSError:
        if _reader is not None:
            log.warning("GeoIP: %s desapareció; países desactivados", mmdb_path)
        _reset()
        return
    if _reader is not None and _path == mmdb_path and _mtime == mtime:
        return
    _reset()
    try:
        import maxminddb
        _reader = maxminddb.open_database(mmdb_path)
        _path, _mtime = mmdb_path, mtime
        log.info("GeoIP cargado desde %s", mmdb_path)
    except Exception as exc:
        log.warning("GeoIP no disponible (%s): %s", mmdb_path, exc)
        _path, _mtime = mmdb_path, mtime    # no reintentar hasta que cambie el archivo


def country(ip: str, mmdb_path: str) -> str:
    """Código ISO de país ('AR', 'US', ...) o '' si no se puede resolver."""
    _ensure(mmdb_path)
    if _reader is None:
        return ""
    try:
        rec = _reader.get(ip)
        return (rec or {}).get("country", {}).get("iso_code", "") or ""
    except (ValueError, AttributeError):
        return ""
