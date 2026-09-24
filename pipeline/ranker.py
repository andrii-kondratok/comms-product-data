"""Ознаки реранкера — спільні для навчання і щоденного ранжування.

Ті самі ознаки, що в posts_db/eval_ranker.py, але з бази. Жодна не дивиться
в майбутнє: історія постів і дайджесту береться лише до дня кандидата.
"""

from __future__ import annotations

import re
from datetime import timedelta

import numpy as np

from . import embeddings

EVENT_SIM = 0.80          # ознака «розмір події» — так вона рахувалась при навчанні
# Дедуплікація топу. Виміряно на топі 21.09: та сама подія — 0.71–0.78 (вибори в Думу,
# саміт Трампа і Сі), різні сюжети близької теми — 0.60–0.68. 0.80 пропускало
# п'ять статей про ті самі вибори.
DEDUP_SIM = 0.70
# Між мовами схожість нижча навіть для однакового змісту. Виміряно 21.09: та сама
# подія англ./укр./рос. — 0.657–0.698 (заява Путіна про Європу, вибори, зустріч
# Зеленського й Трампа), різні події — 0.650 і нижче.
DEDUP_SIM_CROSS = 0.655
KNN = 10
COVERED_DAYS = 3
SRC_SMOOTH = 20           # джерело з кількома статтями не отримує крайніх значень


def _arr(rows, col="e"):
    if not rows:
        return np.zeros((0, 1024), dtype=np.float32)
    return np.array([np.fromstring(r[col].strip("[]"), sep=",") for r in rows], dtype=np.float32)


def embed_candidates(con, where_sql: str, params: tuple, limit: int = 20000) -> int:
    """Ембединги кандидатів, яких ще немає. Повертає, скільки пораховано."""
    rows = con.execute(f"""
        SELECT c.candidate_id, c.title, c.description FROM ops.candidate_pool c
        WHERE c.title IS NOT NULL AND length(c.title) >= 15 AND ({where_sql})
          AND NOT EXISTS (SELECT 1 FROM ml.candidate_embedding e
                          WHERE e.candidate_id = c.candidate_id)
        LIMIT %s""", (*params, limit)).fetchall()
    if not rows:
        return 0
    texts = [" — ".join(x for x in (r["title"].strip(), (r["description"] or "").strip()[:300]) if x)
             for r in rows]
    E = embeddings.encode(texts)
    for r, e in zip(rows, E):
        con.execute("""INSERT INTO ml.candidate_embedding (candidate_id, model, embedding)
                       VALUES (%s,%s,%s::vector) ON CONFLICT DO NOTHING""",
                    (r["candidate_id"], embeddings.MODEL_NAME, embeddings.vec(e)))
    return len(rows)


def embed_history(con, limit: int = 20000) -> int:
    """Ембединги статей дайджесту і статей, що дали пост, — довідник для knn і covered."""
    rows = con.execute("""
        SELECT a.article_id, a.title, coalesce(a.subtitle, a.description) AS descr
        FROM core.article a
        WHERE a.title IS NOT NULL AND length(a.title) >= 15
          AND (a.notion_page_id IS NOT NULL
               OR EXISTS (SELECT 1 FROM core.article_post_link l WHERE l.article_id = a.article_id))
          AND NOT EXISTS (SELECT 1 FROM core.article_embedding e WHERE e.article_id = a.article_id)
        LIMIT %s""", (limit,)).fetchall()
    if not rows:
        return 0
    texts = [" — ".join(x for x in (r["title"].strip(), (r["descr"] or "").strip()[:300]) if x)
             for r in rows]
    E = embeddings.encode(texts)
    for r, e in zip(rows, E):
        con.execute("""INSERT INTO core.article_embedding (article_id, model, embedding)
                       VALUES (%s,%s,%s::vector) ON CONFLICT (article_id) DO NOTHING""",
                    (r["article_id"], embeddings.MODEL_NAME, embeddings.vec(e)))
    return len(rows)


POST_CHARS = 700          # для порівняння беремо початок треду, не весь


def embed_posts(con, limit: int = 8000, chunk: int = 256) -> int:
    """Ембединги наших опублікованих постів — довідник для «ми про це вже писали».

    Партіями з комітом після кожної: одним викликом на тисячі текстів процес
    падає без повідомлення, а перший запуск на сервері саме такий.
    """
    rows = con.execute("""
        SELECT post_id, coalesce(body_raw, hook_raw) AS text FROM core.post
        WHERE length(coalesce(body_raw, hook_raw, '')) >= 80
          AND (posted_at IS NULL OR posted_at >= now() - interval '2 years')
          AND NOT EXISTS (SELECT 1 FROM core.post_embedding e WHERE e.post_id = core.post.post_id)
        ORDER BY posted_at DESC NULLS LAST LIMIT %s""", (limit,)).fetchall()
    done = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        E = embeddings.encode([r["text"][:POST_CHARS] for r in part])
        for r, e in zip(part, E):
            con.execute("""INSERT INTO core.post_embedding (post_id, model, embedding)
                           VALUES (%s,%s,%s::vector) ON CONFLICT (post_id) DO NOTHING""",
                        (r["post_id"], embeddings.MODEL_NAME, embeddings.vec(e)))
        con.commit()
        done += len(part)
    return done


def coverage_by_url(con, rows, *, post_days: int, digest_days: int) -> dict:
    """Точна перевірка «вже писали» — за посиланням, без схожості.

    Кандидат → стаття з тим самим url_canonical → чи є з неї наш пост
    (core.article_post_link) і чи вона вже в дайджесті (notion_page_id).
    Помилок тут не буває: це не схожість, а той самий матеріал.
    """
    ids = [r["candidate_id"] for r in rows]
    out = {i: {} for i in ids}
    for r in con.execute("""
            SELECT c.candidate_id, l.post_id, p.posted_at
            FROM ops.candidate_pool c
            JOIN core.article a ON a.url_canonical = c.url_canonical
            JOIN core.article_post_link l ON l.article_id = a.article_id
            JOIN core.post p ON p.post_id = l.post_id
            WHERE c.candidate_id = ANY(%s)
              AND (p.posted_at IS NULL OR p.posted_at >= now() - make_interval(days => %s))""",
            (ids, post_days)):
        out[r["candidate_id"]]["post"] = (1.0, r["post_id"])
    for r in con.execute("""
            SELECT c.candidate_id, a.article_id
            FROM ops.candidate_pool c
            JOIN core.article a ON a.url_canonical = c.url_canonical
            WHERE c.candidate_id = ANY(%s) AND a.notion_page_id IS NOT NULL
              AND coalesce(a.published_at, a.ingested_at) >= now() - make_interval(days => %s)""",
            (ids, digest_days)):
        out[r["candidate_id"]]["digest"] = (1.0, r["article_id"])
    return out


def coverage(con, rows, E, *, post_days: int, digest_days: int) -> dict:
    """Для кожного кандидата — найсхожіший наш пост і найсхожіша стаття дайджесту.

    Повертає {candidate_id: {"post": (score, post_id), "digest": (score, article_id)}}.
    """
    posts = con.execute("""
        SELECT p.post_id, e.embedding::text AS e FROM core.post p
        JOIN core.post_embedding e USING (post_id)
        WHERE p.posted_at >= now() - make_interval(days => %s)""", (post_days,)).fetchall()
    arts = con.execute("""
        SELECT a.article_id, e.embedding::text AS e FROM core.article a
        JOIN core.article_embedding e USING (article_id)
        WHERE a.notion_page_id IS NOT NULL
          AND coalesce(a.published_at, a.ingested_at) >= now() - make_interval(days => %s)""",
        (digest_days,)).fetchall()
    out = {r["candidate_id"]: {} for r in rows}
    for key, ref, id_col in (("post", posts, "post_id"), ("digest", arts, "article_id")):
        if not ref:
            continue
        M = _arr(ref)
        sims = E @ M.T
        best = sims.argmax(axis=1)
        for i, r in enumerate(rows):
            out[r["candidate_id"]][key] = (float(sims[i, best[i]]), ref[int(best[i])][id_col])
    return out


def load_reference(con):
    """Топіки, рутина та історія (дата, дав пост, вектор)."""
    model = embeddings.MODEL_NAME
    facets = con.execute("""SELECT f.topic_code, f.embedding::text AS e
                            FROM core.topic_facet f JOIN core.topic t USING (topic_code)
                            WHERE f.model=%s AND t.is_active AND t.from_news
                            ORDER BY f.topic_code, f.facet_id""", (model,)).fetchall()
    codes = sorted({f["topic_code"] for f in facets})
    owner = np.array([codes.index(f["topic_code"]) for f in facets])
    routine = con.execute("SELECT embedding::text AS e FROM core.routine_facet WHERE model=%s",
                          (model,)).fetchall()
    hist = con.execute("""
        SELECT (coalesce(a.published_at, a.ingested_at) AT TIME ZONE 'Europe/Kyiv')::date AS d,
               a.notion_page_id IS NOT NULL AS in_digest,
               EXISTS (SELECT 1 FROM core.article_post_link l WHERE l.article_id = a.article_id) AS has_post,
               e.embedding::text AS e
        FROM core.article a JOIN core.article_embedding e USING (article_id)
        WHERE a.notion_page_id IS NOT NULL
           OR EXISTS (SELECT 1 FROM core.article_post_link l WHERE l.article_id = a.article_id)""").fetchall()
    # Статті, які людина позначила як добрі, — такий самий взірець, як статті, що дали
    # пост. Датуються днем оцінки: на сам день не впливають, лише на наступні.
    good = con.execute("""
        SELECT DISTINCT ON (f.candidate_id) f.day AS d, e.embedding::text AS e
        FROM feedback.pick_feedback f JOIN ml.candidate_embedding e USING (candidate_id)
        WHERE f.verdict = 'good' ORDER BY f.candidate_id, f.created_at DESC""").fetchall()
    hist = list(hist) + [{"d": g["d"], "in_digest": False, "has_post": True, "e": g["e"]}
                         for g in good]
    return {
        "codes": codes, "owner": owner, "F": _arr(facets), "R": _arr(routine),
        "H": _arr(hist), "h_day": np.array([h["d"] for h in hist]),
        "h_digest": np.array([h["in_digest"] for h in hist], dtype=bool),
        "h_post": np.array([h["has_post"] for h in hist], dtype=bool),
        "n_good": len(good),
    }


def load_candidates(con, where_sql: str, params: tuple):
    rows = con.execute(f"""
        SELECT c.candidate_id, c.title, c.outcome, c.source_id,
               (c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
               e.embedding::text AS e
        FROM ops.candidate_pool c JOIN ml.candidate_embedding e USING (candidate_id)
        WHERE {where_sql}""", params).fetchall()
    return rows, _arr(rows)


def feature_names(ref) -> list:
    return ([f"topic:{c}" for c in ref["codes"]] +
            ["topic_max", "routine", "topic_minus_routine", "knn_post", "covered_3d",
             "log_event_size", "lang_uk", "lang_ru", "lang_en", "src_rate"])


def lang_of(t: str) -> str:
    """uk / ru / en за заголовком. Кирилиця без і/ї/є/ґ — російська: заголовки rbc.ua
    на кшталт «Европа может не отразить…» не мають ы/э/ъ/ё і раніше йшли як українські."""
    t = t or ""
    cyr = sum(("а" <= c.lower() <= "я") or c.lower() in "іїєґёыэъ" for c in t)
    lat = sum("a" <= c.lower() <= "z" for c in t)
    if lat >= cyr:
        return "en"
    return "uk" if re.search(r"[іїєґ]", t.lower()) else "ru"


def _lang(t: str):
    l = lang_of(t)
    return [float(l == "uk"), float(l == "ru"), float(l == "en")]


def source_rates(con, before_day) -> dict:
    """Частка взятих по джерелу на розмічених днях до before_day."""
    rows = con.execute("""
        SELECT source_id, count(*) AS n, count(*) FILTER (WHERE outcome='ingested') AS k
        FROM ops.candidate_pool
        WHERE (first_seen_at AT TIME ZONE 'Europe/Kyiv')::date < %s
        GROUP BY source_id""", (before_day,)).fetchall()
    total_n = sum(r["n"] for r in rows) or 1
    prior = sum(r["k"] for r in rows) / total_n
    rates = {r["source_id"]: (r["k"] + SRC_SMOOTH * prior) / (r["n"] + SRC_SMOOTH) for r in rows}
    return rates, prior


def features(con, rows, E, ref):
    """Матриця ознак для кандидатів (рядки можуть бути з різних днів)."""
    s = E @ ref["F"].T
    per_topic = np.stack([s[:, ref["owner"] == j].max(axis=1)
                          for j in range(len(ref["codes"]))], axis=1)
    tmax = per_topic.max(axis=1)
    rout = (E @ ref["R"].T).max(axis=1) if len(ref["R"]) else np.full(len(E), -1.0)
    knn = np.zeros(len(rows))
    covered = np.zeros(len(rows))
    event = np.zeros(len(rows))
    src = np.zeros(len(rows))
    days = np.array([r["day"] for r in rows])
    for D in sorted(set(days)):
        idx = np.where(days == D)[0]
        post_ref = ref["H"][(ref["h_day"] < D) & ref["h_post"]]
        recent = ref["H"][(ref["h_day"] < D) & (ref["h_day"] >= D - timedelta(days=COVERED_DAYS))
                          & ref["h_digest"]]
        if len(post_ref):
            sims = E[idx] @ post_ref.T
            k = min(KNN, sims.shape[1])
            knn[idx] = np.sort(sims, axis=1)[:, -k:].mean(axis=1)
        if len(recent):
            covered[idx] = (E[idx] @ recent.T).max(axis=1)
        same = E[idx] @ E[idx].T
        event[idx] = (same > EVENT_SIM).sum(axis=1) - 1
        rates, prior = source_rates(con, D)
        src[idx] = [rates.get(rows[i]["source_id"], prior) for i in idx]
    lang = np.array([_lang(r["title"] or "") for r in rows])
    X = np.column_stack([per_topic, tmax, rout, tmax - rout, knn, covered,
                         np.log1p(event), lang, src])
    return X, per_topic, event


def same_event(E, langs, i, j) -> bool:
    lim = DEDUP_SIM if langs[i] == langs[j] else DEDUP_SIM_CROSS
    return float(E[i] @ E[j]) >= lim


def dedup_top(order, E, langs, k=20):
    """Жадібно: найкращий кандидат, далі — лише ті, що не про вже взяту подію."""
    picked = []
    for i in order:
        if not any(same_event(E, langs, i, j) for j in picked):
            picked.append(i)
            if len(picked) == k:
                break
    return picked
