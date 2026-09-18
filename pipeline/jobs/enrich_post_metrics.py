"""Обгортка над postgres/enrich_post_metrics.py — воркером, що пише в Notion.

Логіка лишається в одному місці (скрипт уже ганявся на 15 тис. рядків і має
регресійні кейси); тут лише запуск за розкладом і облік у ops.run.
"""

from __future__ import annotations

import re
import subprocess
import sys

from .. import config


def run(ctx) -> dict:
    p = subprocess.run(
        [sys.executable, str(config.ROOT / "postgres" / "enrich_post_metrics.py"), "--apply"],
        capture_output=True, text=True, encoding="utf-8", timeout=13 * 60,
        cwd=str(config.ROOT),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    out = p.stdout + p.stderr
    if p.returncode != 0:
        raise RuntimeError(out[-1500:])
    stats = {}
    for k in ("updated", "text_written", "source_written", "low_match",
              "ambiguous_start", "empty_chunk", "error_write", "error_fetch"):
        m = re.search(rf"^\s*{k}\s+([\d,]+)", out, re.M)
        if m:
            stats[k] = int(m.group(1).replace(",", ""))
    if "Порожніх полів немає" in out:
        stats["idle"] = True
    return stats
