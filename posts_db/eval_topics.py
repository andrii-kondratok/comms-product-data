"""Чи відрізняє тематичний відбір те, що редакція взяла, від того, що пропустила.

Позитиви — статті з 🧾 Articles (потрапили в дайджест).
Негативи — пул кандидатів зі стрічок, що в дайджест не потрапили (outcome = seen).
Порівнюємо за заголовком (+ підзаголовок, де є): у кандидатів тексту ще немає,
і в продакшні відбір теж працює до дотягування тексту.

Що міряємо:
  1. AUC: наскільки максимальна схожість із темою розділяє два класи.
  2. Поріг, за якого проходить 90% позитивів, і скільки при цьому відсіюється негативів.
  3. Розподіл позитивів за темами — які теми реально живлять дайджест.
  4. Позитиви нижче порогу — чого в описі тем бракує.
  5. Ціль: чи збігаються цілі з тем статті з Strategic Goal посту в Post Metrics.

Вихід: data/processed/topic_eval.md + кеш ембедингів.
Запуск: python posts_db/eval_topics.py
"""

from __future__ import annotations

import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
CACHE = PROC / "emb_cache"
MODEL = "BAAI/bge-m3"
csv.field_size_limit(10 ** 8)


def load_topics():
    d = json.load((ROOT / "pipeline" / "topics.json").open(encoding="utf-8"))
    return d["topics"], d["goals"]


def embed(model, texts, name):
    CACHE.mkdir(parents=True, exist_ok=True)
    # ключ кешу — хеш самих текстів: після правок фасетів старі вектори не підхопляться
    import hashlib
    h = hashlib.sha1("\n".join(texts).encode()).hexdigest()[:12]
    f = CACHE / f"{name}_{h}.npy"
    if f.exists():
        return np.load(f)
    legacy = CACHE / f"{name}.npy"          # кеш першого прогону, до хешів; для статей текст не мінявся
    if name != "facets" and legacy.exists() and len(np.load(legacy)) == len(texts):
        legacy.rename(f)
        return np.load(f)
    arr = model.encode(texts, batch_size=32, normalize_embeddings=True,
                       show_progress_bar=True, convert_to_numpy=True)
    np.save(f, arr)
    return arr


def auc(pos, neg):
    """Імовірність, що випадковий позитив має вищий бал за випадковий негатив."""
    s = np.concatenate([pos, neg])
    ranks = s.argsort().argsort() + 1
    rp = ranks[:len(pos)].sum()
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def main() -> None:
    topics, goals = load_topics()
    news = [t for t in topics if t["from_news"]]

    # --- позитиви: дайджест
    pos = []
    for r in csv.DictReader((PROC / "pg_article.csv").open(encoding="utf-8")):
        title = (r["title"] or "").strip()
        if len(title) >= 20:
            pos.append({"url": r["url_canonical"], "text": " — ".join(
                x for x in (title, (r["subtitle"] or "").strip()) if x)})
    pos_urls = {p["url"].lower().rstrip("/") for p in pos}

    # --- негативи: пул, що не потрапив у дайджест
    neg = []
    for r in csv.DictReader((PROC / "candidate_pool_export.csv").open(encoding="utf-8")):
        title = (r["title"] or "").strip()
        if r["outcome"] == "seen" and len(title) >= 20 \
                and r["url_canonical"].lower().rstrip("/") not in pos_urls:
            neg.append({"url": r["url_canonical"], "text": title})

    print(f"позитивів {len(pos):,}, негативів {len(neg):,}")
    model = SentenceTransformer(MODEL, device="cpu")

    facets, owner = [], []
    for i, t in enumerate(news):
        for f in t["facets"]:
            facets.append(f)
            owner.append(i)
    owner = np.array(owner)
    F = embed(model, facets, "facets")
    P = embed(model, [p["text"] for p in pos], "pos")
    N = embed(model, [n["text"] for n in neg], "neg")

    def topic_scores(X):
        sims = X @ F.T                                   # вектори нормовані → косинус
        out = np.full((len(X), len(news)), -1.0)
        for j in range(len(news)):
            out[:, j] = sims[:, owner == j].max(axis=1)
        return out

    SP, SN = topic_scores(P), topic_scores(N)
    mp, mn = SP.max(axis=1), SN.max(axis=1)
    a = auc(mp, mn)

    lines = [f"# Тематичний відбір: перевірка на даних\n",
             f"Модель `{MODEL}`, {len(news)} новинних тем, {len(facets)} фасетів. "
             f"Позитиви — дайджест ({len(pos):,}), негативи — пул кандидатів, що не потрапив у дайджест ({len(neg):,}).\n",
             f"## Розділення\n\n**AUC = {a:.3f}** (0.5 — випадково, 1.0 — ідеально).\n",
             "| Поріг | Позитивів проходить | Негативів проходить | Скорочення потоку |",
             "|---|---|---|---|"]
    chosen = None
    for q in (0.95, 0.90, 0.80, 0.70):
        thr = float(np.quantile(mp, 1 - q))
        passn = float((mn >= thr).mean())
        lines.append(f"| {thr:.3f} | {q:.0%} | {passn:.0%} | ×{1 / max(passn, 1e-9):.1f} |")
        if q == 0.90:
            chosen = thr
    lines.append(f"\nРобочий поріг — за 90% позитивів: **{chosen:.3f}**.\n")

    # --- теми дайджесту
    top_p = Counter(news[j]["code"] for j in SP.argmax(axis=1))
    top_n = Counter(news[j]["code"] for j in SN.argmax(axis=1))
    lines += ["## Які теми живлять дайджест\n",
              "| Тема | Дайджест | Пул | Частка взятих серед кандидатів цієї теми |", "|---|---|---|---|"]
    for t in news:
        c, cp, cn = t["code"], top_p[t["code"]], top_n[t["code"]]
        share = cp / (cp + cn) if cp + cn else 0
        lines.append(f"| {t['id']}. {t['name_uk']} | {cp} ({cp / len(pos):.0%}) | {cn} | {share:.0%} |")

    # --- позитиви нижче порогу: чого бракує в описі
    low = [(mp[i], pos[i]["text"]) for i in range(len(pos)) if mp[i] < chosen]
    random.seed(7)
    lines += [f"\n## Позитиви нижче порогу ({len(low)}): що тематика не ловить\n"]
    for s, txt in sorted(random.sample(low, min(25, len(low)))):
        lines.append(f"- `{s:.3f}` {txt[:140]}")

    # --- негативи з найвищим балом: що відбір помилково вважатиме цікавим
    hi = sorted(((mn[i], neg[i]["text"]) for i in range(len(neg))), reverse=True)[:15]
    lines += ["\n## Негативи з найвищим балом\n"]
    for s, txt in hi:
        lines.append(f"- `{s:.3f}` {txt[:140]}")

    # --- цілі: порівняння з Strategic Goal у Post Metrics
    pm = json.load((ROOT / "data" / "raw" / "notion" / "_pm_live.json").open(encoding="utf-8"))
    pm_goal = {}
    label2goal = {lab: g for g, v in goals.items() for lab in v["pm_label"]}
    for r in pm:
        labs = [x["name"] for x in (r["properties"].get("Strategic Goal") or {}).get("multi_select") or []]
        gs = {label2goal[l] for l in labs if l in label2goal}
        if gs:
            pm_goal[r["id"]] = gs
    pairs = [r for r in csv.DictReader((PROC / "training_pairs.csv").open(encoding="utf-8"))
             if r["pm_page_id"] in pm_goal]
    if pairs:
        texts = [(r["article_title"] or r["article_text"][:300]).strip() for r in pairs]
        G = topic_scores(embed(model, texts, "goal_pairs"))
        hit = 0
        conf = defaultdict(Counter)
        for i, r in enumerate(pairs):
            best = news[int(G[i].argmax())]
            pred = set(best["goals"])
            true = pm_goal[r["pm_page_id"]]
            hit += bool(pred & true)
            for g in true:
                conf[g][best["code"]] += 1
        lines += [f"\n## Цілі: збіг із розміткою Post Metrics\n",
                  f"Пар стаття → пост, де пост має Strategic Goal: {len(pairs)}. "
                  f"Найближча тема статті працює на ту саму ціль, що й пост: **{hit / len(pairs):.0%}**.\n"]
        for g, cnt in conf.items():
            lines.append(f"- {g}: " + ", ".join(f"{k} {v}" for k, v in cnt.most_common(4)))

    out = PROC / "topic_eval.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"AUC {a:.3f}, поріг {chosen:.3f} → {out}")


if __name__ == "__main__":
    main()
