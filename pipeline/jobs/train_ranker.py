"""Перенавчання реранкера на розмічених днях пулу.

Розмічений день — той, за який редакція вже взяла щось у дайджест, і він
закінчився (не сьогодні). Останній такий день відкладається для перевірки:
модель навчається на решті, метрики пишуться з відкладеного дня. Потім модель
перенавчається на всіх днях і стає активною — якщо на відкладеному дні вона
не гірша за бал теми.

Модель — логістична регресія: пояснювана, стабільна на малих даних і рахується
в продакшні одним скалярним добутком, без sklearn.

Мітки: вибір редакції — слабкий сигнал (вона бачить не всі новини). Пряма оцінка
топу з feedback.pick_feedback важить більше: good / bad — вага FEEDBACK_WEIGHT,
ok — слабкий позитив. duplicate у навчання не йде: це вада дедуплікації.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from .. import embeddings, ranker

MIN_DAYS = 2
FEEDBACK_WEIGHT = 5.0


def _fit(X, y, w=None):
    from sklearn.linear_model import LogisticRegression
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-6
    lr = LogisticRegression(C=0.3, class_weight="balanced", max_iter=3000)
    lr.fit((X - mu) / sd, y, sample_weight=w)
    return {"mean": mu.tolist(), "scale": sd.tolist(),
            "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])}


def score(X, p) -> np.ndarray:
    z = ((X - np.array(p["mean"])) / np.array(p["scale"])) @ np.array(p["coef"]) + p["intercept"]
    return 1 / (1 + np.exp(-z))


def _recall_at(y, s, k=20):
    order = np.argsort(-s)
    return float(y[order[:k]].sum() / max(y.sum(), 1)), int(y[order[:k]].sum())


def run(ctx) -> dict:
    from sklearn.metrics import roc_auc_score
    con = ctx.con
    topics = embeddings.sync_topics(con)
    days = [r["d"] for r in con.execute("""
        SELECT (first_seen_at AT TIME ZONE 'Europe/Kyiv')::date AS d
        FROM ops.candidate_pool
        GROUP BY 1
        HAVING (count(*) FILTER (WHERE outcome='ingested') > 0
                OR bool_or(EXISTS (SELECT 1 FROM feedback.pick_feedback f
                                   WHERE f.candidate_id = ops.candidate_pool.candidate_id)))
           AND (first_seen_at AT TIME ZONE 'Europe/Kyiv')::date < (now() AT TIME ZONE 'Europe/Kyiv')::date
        ORDER BY 1""").fetchall()]
    if len(days) < MIN_DAYS:
        return {"note": f"розмічених днів {len(days)} < {MIN_DAYS}", "days": [str(d) for d in days]}

    where = "(c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date = ANY(%s)"
    n_emb = ranker.embed_candidates(con, where, (days,))
    n_hist = ranker.embed_history(con)
    ctx.checkpoint()
    ref = ranker.load_reference(con)
    rows, E = ranker.load_candidates(con, where, (days,))
    X, per_topic, _ = ranker.features(con, rows, E, ref)
    y = np.array([r["outcome"] == "ingested" for r in rows])
    d = np.array([r["day"] for r in rows])

    # пряма оцінка топу переважує вибір редакції
    fb = {r["candidate_id"]: r["verdict"] for r in con.execute("""
        SELECT DISTINCT ON (candidate_id) candidate_id, verdict FROM feedback.pick_feedback
        WHERE verdict <> 'duplicate' ORDER BY candidate_id, created_at DESC""")}
    w = np.ones(len(rows))
    for i, r in enumerate(rows):
        v = fb.get(r["candidate_id"])
        if v in ("good", "bad"):
            y[i], w[i] = v == "good", FEEDBACK_WEIGHT
        elif v == "ok":
            y[i] = True

    hold = days[-1]
    tr, te = d != hold, d == hold
    p_hold = _fit(X[tr], y[tr], w[tr])
    s_model, s_topic = score(X[te], p_hold), per_topic[te].max(axis=1)
    rec_m, hits_m = _recall_at(y[te], s_model)
    rec_t, hits_t = _recall_at(y[te], s_topic)
    metrics = {"holdout_day": str(hold), "holdout_taken": int(y[te].sum()),
               "holdout_candidates": int(te.sum()),
               "auc": round(float(roc_auc_score(y[te], s_model)), 3),
               "auc_topic_only": round(float(roc_auc_score(y[te], s_topic)), 3),
               "top20_taken": hits_m, "top20_taken_topic_only": hits_t,
               "recall20": round(rec_m, 3), "recall20_topic_only": round(rec_t, 3)}

    params = _fit(X, y, w)                   # фінальна модель — на всіх днях
    version = f"lr-{datetime.now():%Y%m%d-%H%M}"
    better = metrics["auc"] >= metrics["auc_topic_only"]
    if better:
        con.execute("UPDATE ml.ranker_model SET is_active=false WHERE is_active")
    con.execute("""INSERT INTO ml.ranker_model (version, embed_model, taxonomy_version, features,
                       params, train_days, holdout_day, metrics, is_active)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (version, embeddings.MODEL_NAME, topics["taxonomy_version"],
                 ranker.feature_names(ref), __import__("json").dumps(params), days, hold,
                 __import__("json").dumps(metrics), better))
    return {"version": version, "active": better, "days": len(days), "candidates": len(rows),
            "feedback_labels": len(fb),
            "embedded": n_emb, "history_embedded": n_hist, **metrics}
