"""Топ дня (DAILY_TOP, за замовчуванням 30): ранжування сьогоднішніх кандидатів і дедуплікація подій.

Два види моделі в ml.ranker_model (params.kind):
  transparent — ½ відповідності темі + ½ схожості на взірці (статті, що дали пост,
                і позначені людиною як добрі). Налаштовується оцінками, не вагами.
  logreg      — навчена на виборі редакції. 22.09 перевірка людиною показала, що на
                кількох днях вона вчить випадкові особливості (від'ємна вага теми,
                Reuters топиться за джерелом), тож активується лише вручну.

1. Ембединги кандидатів, що з'явились сьогодні.
2. Ознаки й бал активної моделі реранкера.
3. Рутина відсікається жорстко — тим самим правилом, що в тематичному відборі:
   рутина ≥ тема + запас. Модель і так штрафує рутину, але це вимога редакції,
   тож вона не залежить від ваг.
4. З кількох статей про одну подію лишається найкраща; решта йде в event_size.
5. Зріз пишеться в ml.daily_pick з часом розрахунку: видно, як список змінювався.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

import numpy as np

from .. import embeddings, ranker
from .train_ranker import score

# Розмір топу: 30 на етапі налаштування — ширше вікно, щоб бачити, що лежить за межею
TOP = int(os.environ.get("DAILY_TOP", "30"))


def run(ctx) -> dict:
    con = ctx.con
    m = con.execute("SELECT * FROM ml.ranker_model WHERE is_active").fetchone()
    if not m:
        return {"note": "немає активної моделі — спершу train_ranker"}
    topics = embeddings.sync_topics(con)
    today = con.execute("SELECT (now() AT TIME ZONE 'Europe/Kyiv')::date AS d").fetchone()["d"]
    # Вікно — 36 годин, а не календарний день: о 9-й ранку «сьогодні» майже порожнє,
    # а новини вчорашнього вечора ще актуальні.
    where = "c.first_seen_at >= now() - interval '36 hours'"
    n_emb = ranker.embed_candidates(con, where, ())
    ctx.checkpoint()

    ref = ranker.load_reference(con)
    p = m["params"]
    transparent = p.get("kind") == "transparent"
    if not transparent and ranker.feature_names(ref) != list(m["features"]):
        return {"note": "теми змінились після навчання — потрібен train_ranker",
                "model": m["version"]}
    rows, E = ranker.load_candidates(con, where, ())
    for r in rows:              # ознаки дня (історія «до дня») — як для сьогодні
        r["day"] = today
    if not rows:
        return {"candidates": 0}
    X, per_topic, event = ranker.features(con, rows, E, ref)
    names = ranker.feature_names(ref)
    if transparent:
        s = (p["w_topic"] * X[:, names.index("topic_max")]
             + p["w_knn"] * X[:, names.index("knn_post")])
    else:
        s = score(X, p)

    thr = con.execute("SELECT routine_margin FROM ml.topic_threshold WHERE model=%s",
                      (embeddings.MODEL_NAME,)).fetchone()
    tmax = per_topic.max(axis=1)
    rout = X[:, ranker.feature_names(ref).index("routine")]
    routine = rout >= tmax + (thr["routine_margin"] if thr else 0.08)
    s = np.where(routine, -9.0, s)
    # свіжість: sitemap приносить і статті кількаденної давнини
    fresh_h = p.get("fresh_hours", 36)
    ages = con.execute("""SELECT candidate_id,
                                 extract(epoch FROM now() - coalesce(published_at, first_seen_at)) / 3600 AS h
                          FROM ops.candidate_pool WHERE candidate_id = ANY(%s)""",
                       ([r["candidate_id"] for r in rows],)).fetchall()
    age = {a["candidate_id"]: float(a["h"]) for a in ages}
    stale = np.array([age.get(r["candidate_id"], 0.0) > fresh_h for r in rows])
    s = np.where(stale, -9.0, s)

    order = [i for i in np.argsort(-s) if s[i] > -9]
    langs = [ranker.lang_of(r["title"]) for r in rows]
    # не більше cap статей однієї теми: без цього топ заповнювали звіти про удари
    cap = p.get("topic_cap", 5)
    topic_of = [ref["codes"][int(per_topic[i].argmax())] for i in range(len(rows))]
    picked, cnt = [], Counter()
    for i in order:
        if cnt[topic_of[i]] >= cap or any(ranker.same_event(E, langs, i, j) for j in picked):
            continue
        picked.append(i)
        cnt[topic_of[i]] += 1
        if len(picked) == TOP:
            break
    # скільки статей дня злилось у кожен пункт топу: «про це пишуть N видань»
    group = [sum(ranker.same_event(E, langs, i, j) for j in range(len(rows))) for i in picked]
    now = datetime.now(timezone.utc)
    for rank, (i, g) in enumerate(zip(picked, group), 1):
        con.execute("""INSERT INTO ml.daily_pick (day, candidate_id, rank, score, topic_code,
                           event_size, model_version, computed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (today, rows[i]["candidate_id"], rank, float(s[i]),
                     ref["codes"][int(per_topic[i].argmax())], g,
                     m["version"], now))
    return {"day": str(today), "candidates": len(rows), "embedded": n_emb,
            "routine_cut": int(routine.sum()), "stale": int(stale.sum()),
            "reference_good": ref["n_good"], "picked": len(picked), "model": m["version"]}
