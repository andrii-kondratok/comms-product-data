"""CLI.

    python -m pipeline migrate            # нові міграції з db/migrations
    python -m pipeline backfill [--only sources,articles,posts,links,candidates]
    python -m pipeline run <job>          # одна задача зараз
    python -m pipeline scheduler          # постійний процес за розкладом з ops.job
    python -m pipeline status             # стан задач
"""

from __future__ import annotations

import logging
import sys

from . import backfill, db, runner


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = sys.argv[1:]
    cmd = args[0] if args else "status"

    if cmd == "migrate":
        applied = db.migrate()
        print("застосовано:", applied or "нічого нового")
    elif cmd == "backfill":
        only = args[args.index("--only") + 1].split(",") if "--only" in args else None
        backfill.main(only)
    elif cmd == "run":
        print(runner.run_job(args[1], trigger="manual"))
    elif cmd == "scheduler":
        db.migrate()
        runner.scheduler()
    elif cmd == "status":
        with db.connect() as con:
            for r in con.execute("SELECT * FROM marts.pipeline_health"):
                print(f"{r['job']:22} {str(r['last_status']):8} "
                      f"{str(r['last_finished_at'])[:19]:20} {r['schedule']:18} "
                      f"помилок поспіль: {r['consecutive_failures']}  {r['stats'] or ''}")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
