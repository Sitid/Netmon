#!/usr/bin/env python3
"""Genera reportes de top consumidores desde la línea de comandos / cron.

Ejemplos:
    make_report.py --range day  --format pdf              # ayer, PDF
    make_report.py --range week --format csv --ref 2026-06-29
    make_report.py --range week --format both --outdir /opt/netmon/reports
    make_report.py --ip 10.10.10.75 --range day --ref 2026-09-25   # un equipo (auditoría)

La unit netmon-report.timer lo ejecuta cada lunes a las 07:00 con la semana
anterior y deja los archivos en /opt/netmon/reports/.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

# permitir ejecutarlo directo desde el repo sin instalar el paquete
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netmon import db, reports  # noqa: E402
from netmon.config import get_settings  # noqa: E402


async def run(args: argparse.Namespace) -> None:
    if args.ref:
        ref = date.fromisoformat(args.ref)
    elif args.range == "week":
        ref = get_settings().today() - timedelta(days=7)   # semana pasada
    else:
        ref = get_settings().today() - timedelta(days=1)   # ayer

    pool = await db.create_pool()
    try:
        if args.ip:
            start, end, label = reports.period_bounds(args.range, ref)
            data = await reports.gather_host_report(pool, args.ip, start, end, label)
        else:
            data = await reports.gather_report_data(pool, args.range, ref, args.limit)
    finally:
        await pool.close()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = (f"netmon_equipo_{args.ip}_{args.range}_{ref.isoformat()}" if args.ip
            else f"netmon_top_{args.range}_{ref.isoformat()}")

    formats = ["csv", "pdf"] if args.format == "both" else [args.format]
    for fmt in formats:
        if args.ip:
            payload = reports.build_host_pdf(data) if fmt == "pdf" else reports.build_host_csv(data)
        else:
            payload = reports.build_pdf(data) if fmt == "pdf" else reports.build_csv(data)
        path = outdir / f"{stem}.{fmt}"
        path.write_bytes(payload)
        print(f"OK {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--range", choices=["day", "week"], default="day")
    parser.add_argument("--ref", default="", help="fecha AAAA-MM-DD dentro del período")
    parser.add_argument("--format", choices=["csv", "pdf", "both"], default="both")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--ip", default="", help="reporte de un solo equipo (auditoría)")
    parser.add_argument("--outdir", default="/opt/netmon/reports")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
