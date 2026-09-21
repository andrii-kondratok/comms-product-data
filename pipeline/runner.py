"""Запуск задачі: блокування, ops.run, ops.job, розклад.

Гарантії:
  * одна задача не біжить двічі одночасно — pg_try_advisory_lock на її імʼя;
  * кожен запуск лишає рядок в ops.run зі статистикою або помилкою;
  * водяні знаки задача оновлює сама всередині своєї транзакції, тож впалий
    прогін нічого не зсуває і наступний підбере те саме вікно.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import traceback
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db

log = logging.getLogger("pipeline")


class Ctx:
    """Те, що бачить задача: з'єднання, id прогону, лог і дедлайн.

    Задачі йдуть по черзі, тож задача, що затягнулась, блокує всі інші.
    Дедлайн — із ops.job.timeout_minutes; довгі задачі перевіряють його самі
    й зупиняються акуратно, закомітивши зроблене.
    """

    def __init__(self, con, run_id, job: str, timeout_minutes: int = 30):
        self.con, self.run_id, self.job = con, run_id, job
        self.log = logging.getLogger(f"pipeline.{job}")
        self.deadline = time.time() + timeout_minutes * 60

    def time_left(self) -> float:
        return self.deadline - time.time()

    def checkpoint(self) -> None:
        """Закомітити зроблене: прогрес видно в базі й не губиться при обриві."""
        self.con.commit()


def _lock_key(job: str) -> int:
    return zlib.crc32(job.encode()) & 0x7FFFFFFF


def run_job(job: str, trigger: str = "schedule") -> dict:
    module = importlib.import_module(f"pipeline.jobs.{job}")
    with db.connect(autocommit=True) as lock_con:
        got = lock_con.execute("SELECT pg_try_advisory_lock(%s) AS ok",
                               (_lock_key(job),)).fetchone()["ok"]
        if not got:
            log.warning("%s: попередній запуск ще триває — пропускаю", job)
            return {"skipped": "locked"}
        try:
            return _run_locked(job, module, trigger)
        finally:
            lock_con.execute("SELECT pg_advisory_unlock(%s)", (_lock_key(job),))


def _run_locked(job: str, module, trigger: str) -> dict:
    with db.connect(autocommit=True) as con:
        # Ми тримаємо блокування задачі, тож будь-який її «running» — мертвий прогін
        con.execute("""UPDATE ops.run SET status='failed', finished_at=now(),
                       error='перервано: процес зупинився, не завершивши прогін'
                       WHERE job=%s AND status='running'""", (job,))
        timeout = con.execute("SELECT timeout_minutes FROM ops.job WHERE job=%s",
                              (job,)).fetchone()
        run_id = con.execute(
            """INSERT INTO ops.run (trigger, triggered_by, pipeline_version, job)
               VALUES (%s, %s, %s, %s) RETURNING run_id""",
            (trigger, "cron" if trigger == "schedule" else "cli",
             config.PIPELINE_VERSION, job)).fetchone()["run_id"]
        con.execute("""UPDATE ops.job SET last_run_id=%s, last_started_at=now(),
                       last_status='running' WHERE job=%s""", (run_id, job))

    started = time.time()
    try:
        with db.connect() as con:            # транзакція задачі
            ctx = Ctx(con, run_id, job, timeout["timeout_minutes"] if timeout else 30)
            stats = module.run(ctx) or {}
            con.commit()
        status, error = "done", None
    except Exception as e:                                       # noqa: BLE001
        stats, status = {}, "failed"
        error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        log.error("%s впав: %s", job, e)

    stats["seconds"] = round(time.time() - started, 1)
    with db.connect(autocommit=True) as con:
        con.execute("""UPDATE ops.run SET status=%s, error=%s, stats=%s, finished_at=now()
                       WHERE run_id=%s""", (status, error, json.dumps(stats), run_id))
        con.execute("""UPDATE ops.job SET last_status=%s, last_finished_at=now(),
                       consecutive_failures = CASE WHEN %s='done' THEN 0
                                                   ELSE consecutive_failures + 1 END
                       WHERE job=%s""", (status, status, job))
    log.info("%s: %s %s", job, status, stats)
    return {"status": status, **stats}


# ---------------------------------------------------------------- розклад

def _field_match(field: str, value: int) -> bool:
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            lo, hi = 0, 59
        elif "-" in part:
            lo, hi = map(int, part.split("-"))
        else:
            lo = hi = int(part)
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


def cron_due(expr: str, now: datetime) -> bool:
    """Мінімальний cron: хвилина, година, день, місяць, день тижня (0 = неділя)."""
    m, h, dom, mon, dow = expr.split()
    return (_field_match(m, now.minute) and _field_match(h, now.hour)
            and _field_match(dom, now.day) and _field_match(mon, now.month)
            and _field_match(dow, (now.isoweekday() % 7)))


def scheduler() -> None:
    """Один процес, раз на хвилину дивиться в ops.job. Задачі біжать по черзі.

    Розклад живе в базі: змінити частоту чи вимкнути задачу можна UPDATE-ом,
    без перезапуску контейнера.
    """
    tz = ZoneInfo(config.TZ)
    log.info("планувальник запущено, TZ=%s", config.TZ)
    last_tick = None
    while True:
        now = datetime.now(tz).replace(second=0, microsecond=0)
        if now != last_tick:
            last_tick = now
            try:
                with db.connect() as con:
                    jobs = con.execute("SELECT job, schedule FROM ops.job WHERE enabled "
                                       "ORDER BY job").fetchall()
                for j in jobs:
                    if cron_due(j["schedule"], now):
                        run_job(j["job"])
            except Exception as e:                               # noqa: BLE001
                log.error("планувальник: %s", e)
        time.sleep(5)
