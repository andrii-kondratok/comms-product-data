"""Топ дня (DAILY_TOP, за замовчуванням 30): ранжування сьогоднішніх кандидатів і дедуплікація подій.

Два види моделі в ml.ranker_model (params.kind):
  transparent — ½ відповідності темі + ½ схожості на взірці (статті, що дали пост,
                і позначені людиною як добрі). Налаштовується оцінками, не вагами.

Перед ранжуванням відсікається те, про що ми вже писали:
  * за посиланням — точно: у статті з тим самим URL уже є наш пост або вона в дайджесті;
  * за схожістю — коли `covered_vector` увімкнено (пороги з posts_db/eval_coverage.py);
    поки вимкнено, бо поріг ще не відкалібровано.
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
    p = m["params"]
    topics = embeddings.sync_topics(con)
    today = con.execute("SELECT (now() AT TIME ZONE 'Europe/Kyiv')::date AS d").fetchone()["d"]
    # Вікно збору ширше за вікно свіжості: стаття може з'явитись у стрічці пізніше,
    # ніж вийшла. Що старше за fresh_hours — відсіється нижче.
    where = "c.first_seen_at >= now() - interval '36 hours'"
    n_emb = ranker.embed_candidates(con, where, ())
    # Взірці для схожості: статті дайджесту й ті, що дали пост. На чистому сервері
    # їх ще ніхто не рахував (раніше це робило лише навчання) — без них топ іде
    # за самою темою. Перший раз ~3 тис. заголовків (кілька хвилин), далі лише нові.
    n_hist = ranker.embed_history(con)
    n_posts = ranker.embed_posts(con) if p.get("covered_vector") else 0
    ctx.checkpoint()

    ref = ranker.load_reference(con)
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

    # «ми про це вже писали»: наші пости і статті, що вже в дайджесті
    days = dict(post_days=p.get("covered_post_days", 30),
                digest_days=p.get("covered_digest_days", 7))
    cov = ranker.coverage_by_url(con, rows, **days)
    if p.get("covered_vector"):
        for cid, hit in ranker.coverage(con, rows, E, **days).items():
            for kind, v in hit.items():
                if kind not in cov[cid]:          # точний збіг важливіший за схожість
                    cov[cid][kind] = v
    covered = np.zeros(len(rows), dtype=bool)
    limits = {"post": (p.get("covered_post_min", 0.80), "ref_post_id"),
              "digest": (p.get("covered_digest_min", 0.80), "ref_article_id")}
    for i, r in enumerate(rows):
        for kind, (lim, col) in limits.items():
            hit = cov[r["candidate_id"]].get(kind)
            if not hit:
                continue
            sc, ref_id = hit          # не ref: так зветься довідник тем
            con.execute(f"""INSERT INTO ml.candidate_coverage
                            (candidate_id, kind, model, score, {col})
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (candidate_id, kind) DO UPDATE
                            SET score = EXCLUDED.score, {col} = EXCLUDED.{col}, checked_at = now()""",
                        (r["candidate_id"], kind, embeddings.MODEL_NAME, sc, ref_id))
            if sc >= lim:
                covered[i] = True
    s = np.where(covered, -9.0, s)
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
            "history_embedded": n_hist,
            "routine_cut": int(routine.sum()), "stale": int(stale.sum()),
            "already_covered": int(covered.sum()), "posts_embedded": n_posts,
            "reference_good": ref["n_good"], "picked": len(picked), "model": m["version"]}
