"""Щоденний pg_dump у том бекапів, зберігаємо 14 днів.

Це мінімум, щоб не втратити пул кандидатів: його не відновити заднім числом.
Копію за межі сервера (S3/інший хост) варто додати окремо.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

from .. import config

KEEP_DAYS = 14


def run(ctx) -> dict:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = config.BACKUP_DIR / f"comms_{datetime.now():%Y%m%d_%H%M}.dump"
    p = subprocess.run(["pg_dump", "--format=custom", "--no-owner",
                        f"--file={out}", config.DATABASE_URL],
                       capture_output=True, text=True, timeout=50 * 60)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-1000:])
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0
    for f in config.BACKUP_DIR.glob("comms_*.dump"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed += 1
    return {"file": out.name, "mb": round(out.stat().st_size / 1e6, 1), "removed": removed}
