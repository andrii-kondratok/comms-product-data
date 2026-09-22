"""Топ дня → рядки в 🧾 Articles зі статусом Linked (як додає редакція вручну).

Далі статтю підхоплює агент обробки редакції: витягує назву, розділ і повний текст.

    python posts_db/push_top_to_notion.py                         # показати, що буде додано
    python posts_db/push_top_to_notion.py --exclude 6,20 --apply  # без поганих номерів
    python posts_db/push_top_to_notion.py --extra urls.txt --apply  # плюс свої посилання

Номери з --exclude записуються в feedback.pick_feedback як 'bad', а статті з --extra,
що є в пулі, — як 'good': так кожне перенесення налаштовує відбір.
Посилання, що вже є в Articles, повторно не додаються.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import config, db, notion  # noqa: E402
from pipeline.util import canonical  # noqa: E402

ARTICLES = config.NOTION_DB["articles"]


def key(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (canonical(u) or u).lower())


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args
    exclude = {int(x) for x in args[args.index("--exclude") + 1].split(",")} if "--exclude" in args else set()
    extra = []
    if "--extra" in args:
        extra = [re.sub(r"(?<!:)//+", "/", u.strip())
                 for u in Path(args[args.index("--extra") + 1]).read_text(encoding="utf-8").split()
                 if u.strip()]

    with db.connect() as con:
        top = con.execute("""
            SELECT p.rank, p.candidate_id, p.day, p.model_version,
                   coalesce(c.url_original, c.url_canonical) AS url, c.title
            FROM ml.daily_pick p JOIN ops.candidate_pool c USING (candidate_id)
            WHERE p.day = (now() AT TIME ZONE 'Europe/Kyiv')::date
              AND p.computed_at = (SELECT max(computed_at) FROM ml.daily_pick
                                   WHERE day = (now() AT TIME ZONE 'Europe/Kyiv')::date)
            ORDER BY p.rank""").fetchall()
        if not top:
            raise SystemExit("Топу за сьогодні немає — спершу python -m pipeline run rank_daily")

        items = [(f"#{t['rank']}", t["url"], t["title"]) for t in top if t["rank"] not in exclude]
        items += [("своя", u, "") for u in extra]
        seen, uniq = set(), []
        for it in items:
            if key(it[1]) not in seen:
                seen.add(key(it[1]))
                uniq.append(it)

        existing = set()
        for row in notion.query_since(ARTICLES, None):
            p = row["properties"]
            for u in (notion.plain(p.get("Source URL")), notion.scalar(p.get("Working URL"))):
                if u:
                    existing.add(key(u))
        todo = [it for it in uniq if key(it[1]) not in existing]

        print(f"до перенесення {len(uniq)}, уже є в Articles {len(uniq) - len(todo)}, нових {len(todo)}")
        for src, u, t in uniq:
            print(f"  {'вже є' if key(u) in existing else '+':5} {src:5} {(t or u)[:90]}")
        if not apply:
            print("\nсухий прогін — додайте --apply")
            return

        for src, u, t in todo:
            notion._call("POST", "https://api.notion.com/v1/pages", {
                "parent": {"database_id": ARTICLES},
                "properties": {"Source URL": {"title": [{"text": {"content": u}}]},
                               "Status": {"status": {"name": "Linked"}}}})
            time.sleep(notion.DELAY)

        # оцінки: виключені номери — погано, свої посилання з пулу — добре
        for t in top:
            if t["rank"] in exclude:
                con.execute("""INSERT INTO feedback.pick_feedback
                               (day, candidate_id, verdict, reason, reviewer, rank_shown, model_version)
                               VALUES (%s,%s,'bad','виключено при перенесенні в Articles','editor',%s,%s)""",
                            (t["day"], t["candidate_id"], t["rank"], t["model_version"]))
        for u in extra:
            c = con.execute("SELECT candidate_id FROM ops.candidate_pool WHERE url_canonical=%s",
                            (canonical(u),)).fetchone()
            if c:
                con.execute("""INSERT INTO feedback.pick_feedback (day, candidate_id, verdict, reason, reviewer)
                               VALUES ((now() AT TIME ZONE 'Europe/Kyiv')::date, %s, 'good',
                                       'додано вручну при перенесенні в Articles', 'editor')""",
                            (c["candidate_id"],))
        con.commit()
        print(f"\nдодано в Articles: {len(todo)}; оцінок записано: {len(exclude)} bad, "
              f"{len(extra)} good (якщо є в пулі)")


if __name__ == "__main__":
    main()
