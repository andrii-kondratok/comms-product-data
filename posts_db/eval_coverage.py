"""Поріг для перевірки «ми про це вже писали».

Позитиви — пари «стаття → пост», які в нас є: пост зроблено саме з цієї статті,
тобто це рівно той випадок, коли нову схожу статтю брати не треба.
Негативи — та сама стаття проти випадкових постів і проти постів того ж тижня
(складніший випадок: та сама тематика, інша подія).

Міряємо схожість заголовка статті з текстом поста і дивимось, який поріг ловить
дублікати, не чіпаючи різні сюжети.

Запуск: python posts_db/eval_coverage.py [--embed-limit 8000]
Вихід:  data/processed/coverage_eval.md
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import db, embeddings, ranker  # noqa: E402

PROC = Path(__file__).resolve().parent.parent / "data" / "processed"
POST_CHARS = 700          # пост часто довгий тред; для порівняння беремо початок


def embed_posts(con, limit: int) -> int:
    rows = con.execute("""
        SELECT post_id, coalesce(body_raw, hook_raw) AS text
        FROM core.post
        WHERE length(coalesce(body_raw, hook_raw, '')) >= 80
          AND (posted_at IS NULL OR posted_at >= '2024-01-01')
          AND NOT EXISTS (SELECT 1 FROM core.post_embedding e WHERE e.post_id = core.post.post_id)
        ORDER BY posted_at DESC NULLS LAST
        LIMIT %s""", (limit,)).fetchall()
    if not rows:
        return 0
    E = embeddings.encode([r["text"][:POST_CHARS] for r in rows])
    for r, e in zip(rows, E):
        con.execute("""INSERT INTO core.post_embedding (post_id, model, embedding)
                       VALUES (%s,%s,%s::vector) ON CONFLICT (post_id) DO NOTHING""",
                    (r["post_id"], embeddings.MODEL_NAME, embeddings.vec(e)))
    con.commit()
    return len(rows)


def main() -> None:
    limit = int(sys.argv[sys.argv.index("--embed-limit") + 1]) if "--embed-limit" in sys.argv else 8000
    with db.connect() as con:
        n = embed_posts(con, limit)
        print(f"пораховано ембедингів постів: {n}", flush=True)

        pairs = con.execute("""
            SELECT a.article_id, p.post_id, a.title,
                   (p.posted_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
                   ea.embedding::text AS ea, ep.embedding::text AS ep
            FROM core.article_post_link l
            JOIN core.article a USING (article_id)
            JOIN core.post p USING (post_id)
            JOIN core.article_embedding ea ON ea.article_id = a.article_id
            JOIN core.post_embedding ep ON ep.post_id = p.post_id
            WHERE l.link_method = 'explicit_url' AND a.title IS NOT NULL""").fetchall()
        posts = con.execute("""
            SELECT p.post_id, (p.posted_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
                   e.embedding::text AS e, left(coalesce(p.body_raw, p.hook_raw), 90) AS txt
            FROM core.post p JOIN core.post_embedding e USING (post_id)""").fetchall()

    if not pairs:
        raise SystemExit("немає пар з ембедингами обох сторін")
    A = ranker._arr(pairs, "ea")
    P = ranker._arr(pairs, "ep")
    true_sim = (A * P).sum(axis=1)

    allP = ranker._arr(posts, "e")
    pday = np.array([p["day"] for p in posts])
    random.seed(11)
    rnd, week = [], []
    for i, pr in enumerate(pairs):
        j = random.randrange(len(posts))
        rnd.append(float(A[i] @ allP[j]))
        if pr["day"] is not None:
            same = np.where((pday != None) & (abs(pday - pr["day"]) <= np.timedelta64(7, "D")))[0]  # noqa: E711
            same = [k for k in same if posts[k]["post_id"] != pr["post_id"]]
            if same:
                week.append(float((A[i] @ allP[same].T).max()))
    rnd, week = np.array(rnd), np.array(week)

    def q(x, p):
        return float(np.quantile(x, p))

    lines = ["# «Ми про це вже писали»: поріг\n",
             f"Пар «стаття → пост» з ембедингами обох сторін: {len(pairs):,}. "
             f"Постів у порівнянні: {len(posts):,}.\n",
             "Схожість заголовка статті з текстом поста (перші 700 знаків).\n",
             "| Розподіл | 10% | 25% | медіана | 75% | 90% |", "|---|---|---|---|---|---|",
             f"| Стаття vs **її** пост | {q(true_sim,.1):.2f} | {q(true_sim,.25):.2f} | "
             f"{q(true_sim,.5):.2f} | {q(true_sim,.75):.2f} | {q(true_sim,.9):.2f} |",
             f"| Стаття vs найсхожіший пост того ж тижня (інша подія) | {q(week,.1):.2f} | "
             f"{q(week,.25):.2f} | {q(week,.5):.2f} | {q(week,.75):.2f} | {q(week,.9):.2f} |",
             f"| Стаття vs випадковий пост | {q(rnd,.1):.2f} | {q(rnd,.25):.2f} | {q(rnd,.5):.2f} | "
             f"{q(rnd,.75):.2f} | {q(rnd,.9):.2f} |",
             "\n| Поріг | Ловить «вже писали» | Хибно на постах того ж тижня | Хибно на випадкових |",
             "|---|---|---|---|"]
    for t in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
        lines.append(f"| {t:.2f} | {(true_sim >= t).mean():.0%} | {(week >= t).mean():.0%} | "
                     f"{(rnd >= t).mean():.0%} |")
    lines += ["\n«Хибно на постах того ж тижня» — верхня оцінка: серед них трапляються й пости",
              "про ту саму подію з іншої статті, тобто справжні дублікати."]

    out = PROC / "coverage_eval.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[3:]))
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
