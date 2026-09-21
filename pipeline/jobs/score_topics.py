"""Свіжі статті → ембединг (pgvector) → найближчі теми редакції.

Текст для ембедингу — заголовок і опис: так відбір працює ще до дотягування
тексту, і так само він калібрувався (posts_db/eval_topics.py).
Схожість із темою = максимум по її фасетах; пишемо три найближчі теми.
Чи «цікаво» — вирішує поріг з ml.topic_threshold, а не код.
"""

from __future__ import annotations

import numpy as np

from .. import embeddings

MAX_PER_RUN = 2000
TOP_K = 3


def run(ctx) -> dict:
    con = ctx.con
    stats = embeddings.sync_topics(con)
    model = embeddings.MODEL_NAME

    facets = con.execute("""SELECT f.topic_code, f.embedding::text AS e
                            FROM core.topic_facet f JOIN core.topic t USING (topic_code)
                            WHERE f.model=%s AND t.is_active AND t.from_news""",
                         (model,)).fetchall()
    if not facets:
        return {**stats, "note": "немає фасетів"}
    codes = sorted({f["topic_code"] for f in facets})
    F = np.array([np.fromstring(f["e"].strip("[]"), sep=",") for f in facets], dtype=np.float32)
    owner = np.array([codes.index(f["topic_code"]) for f in facets])

    todo = con.execute("""
        SELECT a.article_id, a.title, coalesce(a.description, a.subtitle) AS descr
        FROM core.article a
        WHERE a.title IS NOT NULL AND length(a.title) >= 15
          AND coalesce(a.published_at, a.ingested_at) >= now() - interval '3 days'
          AND NOT EXISTS (SELECT 1 FROM ml.article_topic t
                          WHERE t.article_id = a.article_id AND t.model = %s)
        ORDER BY coalesce(a.published_at, a.ingested_at) DESC
        LIMIT %s""", (model, MAX_PER_RUN)).fetchall()
    stats["articles"] = len(todo)
    if not todo:
        return stats

    texts = [" — ".join(x for x in (r["title"].strip(), (r["descr"] or "").strip()[:300]) if x)
             for r in todo]
    E = embeddings.encode(texts)
    sims = E @ F.T
    per_topic = np.stack([sims[:, owner == j].max(axis=1) for j in range(len(codes))], axis=1)

    thr = con.execute("SELECT threshold FROM ml.topic_threshold WHERE model=%s",
                      (model,)).fetchone()
    above = 0
    for i, r in enumerate(todo):
        con.execute("""INSERT INTO core.article_embedding (article_id, model, embedding)
                       VALUES (%s,%s,%s::vector) ON CONFLICT (article_id) DO UPDATE
                       SET model=EXCLUDED.model, embedding=EXCLUDED.embedding, created_at=now()""",
                    (r["article_id"], model, embeddings.vec(E[i])))
        order = np.argsort(-per_topic[i])[:TOP_K]
        for rank, j in enumerate(order, 1):
            con.execute("""INSERT INTO ml.article_topic (article_id, topic_code, model, score, rank)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (r["article_id"], codes[j], model, float(per_topic[i, j]), rank))
        if thr and per_topic[i, order[0]] >= thr["threshold"]:
            above += 1
    stats["above_threshold"] = above
    return stats
