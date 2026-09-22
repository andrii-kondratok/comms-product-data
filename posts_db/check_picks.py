"""Де в рейтингу дня стоять статті, які людина відібрала руками.

Незалежна перевірка: людина переглядала новини сама, без нашого топу.
Для кожної її статті: чи є вона в пулі кандидатів, яке місце в повному
рейтингу дня і в топі після дедуплікації (або з якою статтею топу злилась).

Запуск: python posts_db/check_picks.py data/processed/user_picks_2026-09-22.txt [--record]
  --record  записати статті як 'good' у feedback.pick_feedback
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import db, embeddings, ranker  # noqa: E402
from pipeline.jobs.train_ranker import score  # noqa: E402
from pipeline.util import canonical  # noqa: E402

TOP = int(os.environ.get("DAILY_TOP", "30"))


def main() -> None:
    urls = [u.strip() for u in Path(sys.argv[1]).read_text(encoding="utf-8").split() if u.strip()]
    record = "--record" in sys.argv
    with db.connect() as con:
        m = con.execute("SELECT * FROM ml.ranker_model WHERE is_active").fetchone()
        today = con.execute("SELECT (now() AT TIME ZONE 'Europe/Kyiv')::date AS d").fetchone()["d"]
        # кандидати за останні два дні: відібране вранці часто вийшло напередодні
        where = "(c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date >= %s"
        since = today - __import__("datetime").timedelta(days=1)
        ranker.embed_candidates(con, where, (since,))
        ref = ranker.load_reference(con)
        rows, E = ranker.load_candidates(con, where, (since,))
        # ранжуємо як один день: ознаки дня — сьогоднішні
        for r in rows:
            r["day"] = today
        X, per_topic, _ = ranker.features(con, rows, E, ref)
        p = m["params"]
        names = ranker.feature_names(ref)
        if p.get("kind") == "transparent":
            s = p["w_topic"] * X[:, names.index("topic_max")] + p["w_knn"] * X[:, names.index("knn_post")]
        else:
            s = score(X, p)
        thr = con.execute("SELECT routine_margin FROM ml.topic_threshold WHERE model=%s",
                          (embeddings.MODEL_NAME,)).fetchone()
        rout = X[:, ranker.feature_names(ref).index("routine")]
        routine = rout >= per_topic.max(axis=1) + thr["routine_margin"]
        s = np.where(routine, -1.0, s)
        order = [i for i in np.argsort(-s) if s[i] >= 0]
        full_rank = {i: k + 1 for k, i in enumerate(order)}
        langs = [ranker.lang_of(r["title"]) for r in rows]
        top = ranker.dedup_top(order, E, langs, TOP)
        top_rank = {i: k + 1 for k, i in enumerate(top)}

        by_url = {}
        for i, r in enumerate(rows):
            c = con.execute("SELECT url_canonical FROM ops.candidate_pool WHERE candidate_id=%s",
                            (r["candidate_id"],)).fetchone()
            by_url[c["url_canonical"]] = i

        hit = merged = 0
        print(f"Кандидатів: {len(rows)}, модель {m['version']}, топ-{TOP}\n")
        for u in urls:
            i = by_url.get(canonical(u))
            if i is None:
                print(f"  — немає в пулі        {u[:110]}")
                continue
            title = rows[i]["title"][:80]
            if routine[i]:
                print(f"  рутина (відсічено)   {title}")
            elif i in top_rank:
                hit += 1
                print(f"  ТОП #{top_rank[i]:<3} (рейтинг {full_rank[i]:>4})  {title}")
            else:
                twin = next((j for j in top if ranker.same_event(E, langs, i, j)), None)
                if twin is not None:
                    merged += 1
                    print(f"  злилась із ТОП #{top_rank[twin]:<3}          {title}")
                else:
                    print(f"  поза топом (рейтинг {full_rank.get(i, '—'):>4})  {title}")
            if record:
                con.execute("""INSERT INTO feedback.pick_feedback
                               (day, candidate_id, verdict, reason, reviewer, model_version)
                               VALUES (%s,%s,'good','відібрано вручну як непогане','Andrii',%s)""",
                            (today, rows[i]["candidate_id"], m["version"]))
        n = len(urls)
        print(f"\nУ топі: {hit}/{n} · подія в топі через іншу статтю: {merged}/{n}")
        if record:
            con.commit()
            print("записано як good")


if __name__ == "__main__":
    main()
