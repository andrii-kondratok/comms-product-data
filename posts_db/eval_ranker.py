"""Реранкер: чи краще за бал теми він ставить у топ-20 те, що редакція взяла.

Дані — пул кандидатів із тестової бази за дні, для яких відомий вибір редакції
(outcome = ingested). Перевірка «залиш день»: навчаємо на двох днях, міряємо
на третьому, якого модель не бачила. Жодна ознака не дивиться в майбутнє:
історія постів і дайджесту береться лише до дня, що оцінюється.

Ознаки на статтю:
  topic_*        схожість із кожною новинною темою (max по фасетах)
  topic_max, routine, topic_minus_routine
  knn_post       середня схожість із 10 найближчими статтями, що дали пост (до дня D)
  covered_3d     максимальна схожість зі статтями дайджесту за 3 дні до D: подія вже покрита?
  event_size     скільки кандидатів того ж дня про ту саму подію (схожість > 0.8)
  src_rate       частка взятих із цього джерела на днях навчання
  lang_*         кирилиця / латиниця, російська за ознаками ы/э/ъ

Моделі: логістична регресія (пояснювана) і градієнтний бустинг.
Базові лінії: лише бал теми; бал теми − рутина.
Метрики: AUC, recall@20 і recall@50 (частка взятих у топі дня), те саме після
дедуплікації подій у топі.

Запуск: python posts_db/eval_ranker.py
Вихід:  data/processed/ranker_eval.md
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

import numpy as np
import psycopg
from psycopg.rows import dict_row
from sentence_transformers import SentenceTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from eval_topics import MODEL, PROC, ROOT, embed

DB = "postgresql://comms:test@127.0.0.1:55432/comms"
csv.field_size_limit(10 ** 8)
EVENT_SIM = 0.80
TOP = 20


def norm(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").lower().split("?")[0]).rstrip("/")


def lang_feats(t: str):
    cyr = sum("а" <= c.lower() <= "я" or c in "іїєґ" for c in t)
    lat = sum("a" <= c.lower() <= "z" for c in t)
    ru = bool(re.search(r"[ыэъё]", t.lower()))
    return [cyr > lat and not ru, ru, lat >= cyr]


def dedup_top(order, E, k=TOP, sim=EVENT_SIM):
    """Жадібно: беремо найкращий, пропускаємо все, що схоже на вже взяте."""
    picked = []
    for i in order:
        if all(float(E[i] @ E[j]) < sim for j in picked):
            picked.append(i)
            if len(picked) == k:
                break
    return picked


def main() -> None:
    d = json.load((ROOT / "pipeline" / "topics.json").open(encoding="utf-8"))
    news = [t for t in d["topics"] if t["from_news"]]
    model = SentenceTransformer(MODEL, device="cpu")

    facets, owner = [], []
    for i, t in enumerate(news):
        for f in t["facets"]:
            facets.append(f)
            owner.append(i)
    owner = np.array(owner)
    F = embed(model, facets, "facets")
    R = embed(model, d["routine"], "routine")

    # --- пул з розміткою
    with psycopg.connect(DB, row_factory=dict_row) as con:
        pool = con.execute("""
            SELECT c.url_canonical, c.title, c.description, c.outcome,
                   (c.first_seen_at AT TIME ZONE 'Europe/Kyiv')::date AS day,
                   coalesce(s.domain, split_part(split_part(c.url_canonical,'://',2),'/',1)) AS domain
            FROM ops.candidate_pool c LEFT JOIN core.source s USING (source_id)
            WHERE c.title IS NOT NULL AND length(c.title) >= 20""").fetchall()
    days = sorted({p["day"] for p in pool})
    texts = [" — ".join(x for x in (p["title"].strip(), (p["description"] or "").strip()[:300]) if x)
             for p in pool]
    E = embed(model, texts, "pool")
    y = np.array([p["outcome"] == "ingested" for p in pool])
    day = np.array([p["day"] for p in pool])
    print("днів", days, "кандидатів", len(pool), "взято", int(y.sum()))

    # --- історія: дайджест і статті, що дали пост, з датами
    paired = {norm(r["article_url"]) for r in
              csv.DictReader((PROC / "training_pairs.csv").open(encoding="utf-8"))}
    hist_text, hist_day, hist_post = [], [], []
    for r in csv.DictReader((PROC / "pg_article.csv").open(encoding="utf-8")):
        t = (r["title"] or "").strip()
        if len(t) < 20 or not r["created_at"]:
            continue
        hist_text.append(" — ".join(x for x in (t, (r["subtitle"] or "").strip()) if x))
        hist_day.append(date.fromisoformat(r["created_at"][:10]))
        hist_post.append(norm(r["url_canonical"]) in paired)
    H = embed(model, hist_text, "pos")          # той самий текст, що в eval_topics → кеш
    hist_day, hist_post = np.array(hist_day), np.array(hist_post)

    # --- ознаки
    s = E @ F.T
    per_topic = np.stack([s[:, owner == j].max(axis=1) for j in range(len(news))], axis=1)
    tmax = per_topic.max(axis=1)
    rout = (E @ R.T).max(axis=1)

    knn_post = np.zeros(len(pool))
    covered = np.zeros(len(pool))
    event = np.zeros(len(pool))
    for D in days:
        idx = np.where(day == D)[0]
        ref_post = H[(hist_day < D) & hist_post]
        ref_recent = H[(hist_day < D) & (hist_day >= D - timedelta(days=3))]
        if len(ref_post):
            sims = E[idx] @ ref_post.T
            knn_post[idx] = np.sort(sims, axis=1)[:, -10:].mean(axis=1)
        if len(ref_recent):
            covered[idx] = (E[idx] @ ref_recent.T).max(axis=1)
        same = E[idx] @ E[idx].T
        event[idx] = (same > EVENT_SIM).sum(axis=1) - 1
    lang = np.array([lang_feats(t) for t in texts], dtype=float)

    base = np.column_stack([per_topic, tmax, rout, tmax - rout, knn_post, covered,
                            np.log1p(event), lang])
    names = ([f"topic:{t['code']}" for t in news] +
             ["topic_max", "routine", "topic_minus_routine", "knn_post", "covered_3d",
              "log_event_size", "lang_uk", "lang_ru", "lang_en"])

    def src_rate(train_mask):
        taken, seen = Counter(), Counter()
        for i in np.where(train_mask)[0]:
            seen[pool[i]["domain"]] += 1
            taken[pool[i]["domain"]] += y[i]
        prior = y[train_mask].mean()
        # згладжування: джерело з кількома статтями не отримує крайніх значень
        return np.array([(taken[p["domain"]] + 20 * prior) / (seen[p["domain"]] + 20) for p in pool])

    def evaluate(scores, test):
        idx = np.where(test)[0]
        order = idx[np.argsort(-scores[idx])]
        pos = y[idx].sum()
        r20 = y[order[:TOP]].sum() / pos
        r50 = y[order[:50]].sum() / pos
        dd = dedup_top(order, E, TOP)
        # після дедуплікації: подію вважаємо влученою, якщо в топі є будь-яка стаття,
        # схожа (> 0.8) на взяту редакцією
        taken = [i for i in idx if y[i]]
        hit = sum(any(float(E[i] @ E[j]) > EVENT_SIM for j in dd) for i in taken) / len(taken)
        return roc_auc_score(y[idx], scores[idx]), r20, r50, hit, int(pos), len(idx)

    rows = defaultdict(list)
    coefs = []
    for D in days:
        test = day == D
        train = ~test
        X = np.column_stack([base, src_rate(train)])
        lr = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, class_weight="balanced",
                                                                 max_iter=2000))
        lr.fit(X[train], y[train])
        gb = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=300,
                                            class_weight="balanced", random_state=0)
        gb.fit(X[train], y[train])
        coefs.append(lr[-1].coef_[0])
        cand = {
            "Лише бал теми (зараз)": tmax,
            "Тема − рутина": tmax - np.maximum(0, rout - tmax),
            "Реранкер: логістична регресія": lr.predict_proba(X)[:, 1],
            "Реранкер: бустинг": gb.predict_proba(X)[:, 1],
        }
        for name, sc in cand.items():
            rows[name].append((D, *evaluate(sc, test)))

    lines = ["# Реранкер: перевірка на днях, яких модель не бачила\n",
             f"Пул: {len(pool):,} кандидатів за {len(days)} дні, взято редакцією {int(y.sum())}. "
             "Навчання на двох днях, перевірка на третьому.\n",
             "| Спосіб | День | Взято / кандидатів | AUC | Взятих у топ-20 | у топ-50 | Подій у топ-20 після дедуплікації |",
             "|---|---|---|---|---|---|---|"]
    for name, rs in rows.items():
        for D, auc, r20, r50, hit, pos, n in rs:
            lines.append(f"| {name} | {D:%d.%m} | {pos} / {n} | {auc:.3f} | {r20:.0%} | {r50:.0%} | {hit:.0%} |")
        m = np.mean([r[1] for r in rs]), np.mean([r[2] for r in rs]), \
            np.mean([r[3] for r in rs]), np.mean([r[4] for r in rs])
        lines.append(f"| **{name}** | **середнє** | | **{m[0]:.3f}** | **{m[1]:.0%}** | **{m[2]:.0%}** | **{m[3]:.0%}** |")

    c = np.mean(coefs, axis=0)
    names_all = names + ["src_rate"]
    lines += ["\n## Що важить у логістичній регресії (стандартизовані коефіцієнти, середнє по фолдах)\n"]
    for i in np.argsort(-np.abs(c))[:14]:
        lines.append(f"- `{names_all[i]}`: {c[i]:+.2f}")

    out = PROC / "ranker_eval.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
