"""Підключення, міграції, водяні знаки, облік API."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import config

MIGRATIONS = config.ROOT / "db" / "migrations"


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(config.DATABASE_URL, autocommit=autocommit, row_factory=dict_row)


def migrate() -> list[str]:
    """Проганяє нові файли з db/migrations у порядку імен. Кожен — одна транзакція."""
    applied = []
    with connect(autocommit=True) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS public.schema_migrations (
                           name text PRIMARY KEY,
                           applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r["name"] for r in con.execute("SELECT name FROM public.schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            with con.transaction():
                con.execute(f.read_text(encoding="utf-8"))
                con.execute("INSERT INTO public.schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied


# ---------------------------------------------------------------- водяні знаки

def get_watermark(con, job: str, key: str, default: str | None = None) -> str | None:
    r = con.execute("SELECT value FROM ops.watermark WHERE job=%s AND key=%s",
                    (job, key)).fetchone()
    return r["value"] if r else default


def set_watermark(con, job: str, key: str, value: str) -> None:
    con.execute("""INSERT INTO ops.watermark (job, key, value) VALUES (%s,%s,%s)
                   ON CONFLICT (job, key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = now()""", (job, key, value))


# ---------------------------------------------------------------- облік API

def log_api_call(con, *, provider: str, endpoint: str, run_id, request: dict | None,
                 http_status, items_requested: int | None, items_returned: int | None,
                 latency_ms: int | None, error: str | None = None) -> None:
    con.execute("""INSERT INTO ops.api_call
                   (provider, endpoint, run_id, request, http_status,
                    items_requested, items_returned, latency_ms, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (provider, endpoint, run_id, json.dumps(request or {}), str(http_status),
                 items_requested, items_returned, latency_ms, error))


def api_calls_today(con, provider: str) -> int:
    r = con.execute("""SELECT count(*) AS n FROM ops.api_call
                       WHERE provider = %s
                         AND called_at >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s""",
                    (provider, config.TZ, config.TZ)).fetchone()
    return r["n"]
