"""Розвідка BigQuery-проєкту x-analysis-498513.

Таблиця x_archive.tweets колись існувала (запит 2026-08-04 обробив 30 МБ),
зараз датасет порожній. Перевіряємо, чи таблиця відновлюється через time travel
(BigQuery тримає видалені дані 7 днів).
"""

from __future__ import annotations

import os

from google.cloud import bigquery

os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                      ".secrets/bigquery-x-analysis.json")

TABLE = "x-analysis-498513.x_archive.tweets"


def main() -> None:
    c = bigquery.Client()
    ds = c.get_dataset("x_archive")
    print(f"датасет x_archive · локація {ds.location}")
    print(f"  default_table_expiration_ms: {ds.default_table_expiration_ms}")
    print(f"  створено {ds.created} · змінено {ds.modified}")

    jobs = [j for j in c.list_jobs(max_results=3, all_users=True)
            if j.job_type == "query"]
    if jobs:
        j = jobs[0]
        print(f"\nнайсвіжіший запит: {str(j.created)[:19]} · стан {j.state}")
        print(f"  помилка: {j.error_result}")
        print(f"  байтів оброблено: {j.total_bytes_processed}")
        print("  SQL:", (j.query or "")[:300].replace("\n", " "))

    print("\nСпроба відновлення через time travel:")
    for hours in (1, 6, 24, 72, 160):
        sql = (f"SELECT COUNT(*) AS n FROM `{TABLE}` "
               f"FOR SYSTEM_TIME AS OF "
               f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR)")
        try:
            n = list(c.query(sql).result())[0].n
            print(f"  -{hours:>3} год: {n:,} рядків  ← ВІДНОВЛЮЄТЬСЯ")
            return
        except Exception as e:                            # noqa: BLE001
            print(f"  -{hours:>3} год: {str(e).splitlines()[0][:100]}")

    print("\nЖодна точка в межах вікна time travel не спрацювала.")


if __name__ == "__main__":
    main()
