"""Топ-20 дня: ранжування сьогоднішніх кандидатів і дедуплікація подій.

1. Ембединги кандидатів, що з'явились сьогодні.
2. Ознаки й бал активної моделі реранкера.
3. Рутина відсікається жорстко — тим самим правилом, що в тематичному відборі:
   рутина ≥ тема + запас. Модель і так штрафує рутину, але це вимога редакції,
   тож вона не залежить від ваг.
4. З кількох статей про одну подію лишається найкраща; решта йде в event_size.
5. Зріз пишеться в ml.daily_pick з часом розрахунку: видно, як список змінювався.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .. import embeddings, ranker
from .train_ranker import score

TOP = 20


def run(ctx) -> dict:
    con = ctx.con
    m = con.execute("SELECT * FROM ml.ranker_model WHERE is_active").fetchone()
    if not m:
        return {"note": "немає активної моделі — спершу train_ranker"}
    topics = embeddings.sync_topics(con)
    today = con.execute("SELECT (now() AT TIME ZONE 'Europe/Kyiv')::date AS d").fetchone()["d"]
    where = "(c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date = %s"
    n_emb = ranker.embed_candidates(con, where, (today,))
    ctx.checkpoint()

    ref = ranker.load_reference(con)
    if ranker.feature_names(ref) != list(m["features"]):
        return {"note": "теми змінились після навчання — потрібен train_ranker",
                "model": m["version"]}
    rows, E = ranker.load_candidates(con, where, (today,))
    if not rows:
        return {"candidates": 0}
    X, per_topic, event = ranker.features(con, rows, E, ref)
    s = score(X, m["params"])

    thr = con.execute("SELECT routine_margin FROM ml.topic_threshold WHERE model=%s",
                      (embeddings.MODEL_NAME,)).fetchone()
    tmax = per_topic.max(axis=1)
    rout = X[:, ranker.feature_names(ref).index("routine")]
    routine = rout >= tmax + (thr["routine_margin"] if thr else 0.08)
    s = np.where(routine, -1.0, s)

    order = [i for i in np.argsort(-s) if s[i] >= 0]
    picked = ranker.dedup_top(order, E, TOP)
    # скільки статей дня злилось у кожен пункт топу: «про це пишуть N видань»
    group = [int(((E @ E[i]) >= ranker.DEDUP_SIM).sum()) for i in picked]
    now = datetime.now(timezone.utc)
    for rank, (i, g) in enumerate(zip(picked, group), 1):
        con.execute("""INSERT INTO ml.daily_pick (day, candidate_id, rank, score, topic_code,
                           event_size, model_version, computed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (today, rows[i]["candidate_id"], rank, float(s[i]),
                     ref["codes"][int(per_topic[i].argmax())], g,
                     m["version"], now))
    return {"day": str(today), "candidates": len(rows), "embedded": n_emb,
            "routine_cut": int(routine.sum()), "picked": len(picked), "model": m["version"]}
