"""Скільки тексту віддає видання анонімному читачеві.

Бере ті самі 24 статті, що й приймальний тест провайдерів, завантажує
публічний HTML і пробує витягти текст. Нічого не обходить — показує рівно те,
що сайт віддає без підписки.

Порівнюється з `word_count` статті в нашій базі (там текст, який редакція
вже має), щоб побачити, де віддається вся стаття, а де лише анонс.

Запуск:  python posts_db/test_extractor.py
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import trafilatura

CASES = Path(__file__).parent / "provider_acceptance.csv"
ARTICLES = Path(__file__).parent.parent / "data" / "processed" / "pg_article.csv"
DELAY = 2.0          # ввічливо до чужих серверів


def main() -> None:
    cases = pd.read_csv(CASES)
    arts = pd.read_csv(ARTICLES)[["url_canonical", "body_text"]]
    arts["ours"] = arts["body_text"].str.len()
    ours = dict(zip(arts["url_canonical"].map(
        lambda u: str(u).rstrip("/").lower()), arts["ours"]))

    rows = []
    for i, c in cases.iterrows():
        html = trafilatura.fetch_url(c["url"])
        text = trafilatura.extract(html, include_comments=False,
                                   include_tables=False) if html else None
        got = len(text or "")
        have = ours.get(c["url"].rstrip("/").lower())
        rows.append({"domain": c["domain"], "access": c["access"],
                     "витягнуто": got, "у_нас": have})
        share = f"{got / have:.0%}" if have and have > 0 else "—"
        print(f"  {i + 1:>2}/24 {c['domain']:<22} {c['access']:<13} "
              f"{got:>6} знаків   у нас {str(have or '—'):>6}   {share:>5}")
        time.sleep(DELAY)

    df = pd.DataFrame(rows)
    print("\nЗа виданням (два кейси на кожне):")
    g = df.groupby(["domain", "access"]).agg(
        витягнуто=("витягнуто", "mean"), у_нас=("у_нас", "mean")).round(0)
    g["частка"] = (g["витягнуто"] / g["у_нас"]).round(2)
    print(g.sort_values("частка", ascending=False).to_string())

    print("\nПідсумок за типом доступу:")
    s = df.groupby("access").agg(
        кейсів=("витягнуто", "size"),
        сер_витягнуто=("витягнуто", "mean"),
        повних=("витягнуто", lambda x: (x > 1500).sum()),
        порожніх=("витягнуто", lambda x: (x < 300).sum())).round(0)
    print(s.to_string())


if __name__ == "__main__":
    main()
