"""Вибір порогу і фільтра рутини.

Три групи статей:
  post      — стаття дала пост (є пара стаття → пост): те, що справді цікаво;
  digest    — потрапила в дайджест, але поста не дала;
  not_taken — пул кандидатів, що в дайджест не потрапив.

Правило відбору: тема ≥ τ, і (з фільтром) тема − рутина ≥ δ,
де «рутина» — схожість з антитемами з topics.json (обстріли з кількістю жертв,
тривоги, ДТП). Для кожного τ і δ показуємо, скільки кожної групи проходить.

Запуск: python posts_db/eval_threshold.py
Вихід:  data/processed/threshold_eval.md
"""

from __future__ import annotations

import csv
import json
import re

import numpy as np
from sentence_transformers import SentenceTransformer

from eval_topics import CACHE, MODEL, PROC, ROOT, embed

csv.field_size_limit(10 ** 8)


def norm(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").lower().split("?")[0]).rstrip("/")


def main() -> None:
    d = json.load((ROOT / "pipeline" / "topics.json").open(encoding="utf-8"))
    news = [t for t in d["topics"] if t["from_news"]]
    routine = d["routine"]

    paired = {norm(r["article_url"]) for r in
              csv.DictReader((PROC / "training_pairs.csv").open(encoding="utf-8"))}
    pos, pos_is_post = [], []
    for r in csv.DictReader((PROC / "pg_article.csv").open(encoding="utf-8")):
        title = (r["title"] or "").strip()
        if len(title) >= 20:
            pos.append(" — ".join(x for x in (title, (r["subtitle"] or "").strip()) if x))
            pos_is_post.append(norm(r["url_canonical"]) in paired)
    pos_urls = {norm(r["url_canonical"]) for r in
                csv.DictReader((PROC / "pg_article.csv").open(encoding="utf-8"))}
    neg = [r["title"].strip() for r in
           csv.DictReader((PROC / "candidate_pool_export.csv").open(encoding="utf-8"))
           if r["outcome"] == "seen" and len((r["title"] or "").strip()) >= 20
           and norm(r["url_canonical"]) not in pos_urls]

    model = SentenceTransformer(MODEL, device="cpu")
    facets, owner = [], []
    for i, t in enumerate(news):
        for f in t["facets"]:
            facets.append(f)
            owner.append(i)
    owner = np.array(owner)
    F = embed(model, facets, "facets")
    R = embed(model, routine, "routine")
    P = embed(model, pos, "pos")
    N = embed(model, neg, "neg")

    def scores(X):
        s = X @ F.T
        topic = np.stack([s[:, owner == j].max(axis=1) for j in range(len(news))], 1).max(1)
        return topic, (X @ R.T).max(axis=1)

    tp, rp = scores(P)
    tn, rn = scores(N)
    is_post = np.array(pos_is_post)
    groups = {"post": (tp[is_post], rp[is_post]), "digest": (tp[~is_post], rp[~is_post]),
              "not_taken": (tn, rn)}

    lines = ["# Поріг і фільтр рутини\n",
             f"Статей, що дали пост: {is_post.sum()}, лише дайджест: {(~is_post).sum()}, "
             f"не взяті: {len(tn)}.\n",
             "| τ (тема) | δ (тема − рутина) | Пости проходять | Дайджест | Не взяті | Скорочення потоку |",
             "|---|---|---|---|---|---|"]
    for tau in (0.443, 0.47, 0.49, 0.51, 0.53):
        for delta in (None, -0.12, -0.10, -0.08, 0.0):
            row = []
            for g in ("post", "digest", "not_taken"):
                t, r = groups[g]
                ok = t >= tau
                if delta is not None:
                    ok &= (t - r) >= delta
                row.append(ok.mean())
            lines.append(f"| {tau:.3f} | {'—' if delta is None else f'{delta:+.2f}'} | "
                         f"{row[0]:.0%} | {row[1]:.0%} | {row[2]:.0%} | ×{1 / max(row[2], 1e-9):.1f} |")

    # що саме фільтр рутини відкидає серед постів і серед не взятих
    def cut(texts, t, r, delta=-0.10, n=12):
        idx = [i for i in range(len(t)) if t[i] >= 0.47 and t[i] - r[i] < delta]
        idx.sort(key=lambda i: -(r[i] - t[i]))
        return [f"- `тема {t[i]:.2f} / рутина {r[i]:.2f}` {texts[i][:120]}" for i in idx[:n]], len(idx)

    post_texts = [pos[i] for i in range(len(pos)) if pos_is_post[i]]
    a, na = cut(post_texts, *groups["post"])
    b, nb = cut(neg, *groups["not_taken"])
    lines += [f"\n## Фільтр рутини (рутина ≥ тема + 0.10) відкидає серед статей, що дали пост: {na}\n"] + a
    lines += [f"\n## …і серед не взятих: {nb}\n"] + b

    out = PROC / "threshold_eval.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
