"""Build reproducible: el lockfile debe instalar exactamente lo que corre en producción."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pkgs(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            out[name.strip().lower().replace("_", "-")] = ver.strip()
    return out


def test_existe_lockfile_con_versiones_fijas():
    lock = ROOT / "requirements.lock"
    assert lock.exists(), "falta requirements.lock"
    pk = _pkgs(lock.read_text())
    assert pk, "el lockfile no fija ninguna versión"


def test_dependencias_directas_estan_declaradas_y_fijadas():
    """Cada paquete que el código importa tiene que estar en requirements.txt y en el lock."""
    req = (ROOT / "requirements.txt").read_text().lower()
    lock = _pkgs((ROOT / "requirements.lock").read_text())
    for pkg in ("fastapi", "uvicorn", "asyncpg", "httpx", "icmplib", "pydantic-settings",
                "itsdangerous", "dnspython", "reportlab", "maxminddb", "pypsrp"):
        assert pkg in req, f"{pkg} no está en requirements.txt"
        assert pkg in lock, f"{pkg} no está fijado en requirements.lock"
