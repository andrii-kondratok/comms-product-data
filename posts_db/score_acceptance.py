"""Оцінка провайдера статей на реальних URL з нашої бази.

Ідея: провайдера оцінюємо не за «140k+ джерел», а за тим, чи віддає він
конкретні статті, які редакція справді брала — і зважуємо за тим, скільки
статей це джерело дає в реальному потоці.

Використання:
    1. Прогнати URL із provider_acceptance.csv через API провайдера.
    2. Скласти results.json:  {"<url>": {"found": true, "body": "<текст>"}, ...}
    3. python posts_db/score_acceptance.py results.json [назва_провайдера]

Критерій «повний текст», а не «знайдено»:
    * body довший за MIN_BODY знаків
    * і не схожий на заглушку підписки (див. PAYWALL_MARKERS)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
CASES = HERE / "provider_acceptance.csv"

MIN_BODY = 1200          # менше — це анонс або заглушка, не стаття
PAYWALL_MARKERS = (
    "subscribe to continue", "subscription required", "sign in to read",
    "become a subscriber", "this article is for subscribers",
    "register to continue", "already a subscriber",
)


def classify(entry: dict | None) -> str:
    if not entry or not entry.get("found"):
        return "not_found"
    body = (entry.get("body") or "").strip()
    low = body.lower()
    if any(m in low for m in PAYWALL_MARKERS):
        return "paywall_stub"
    if len(body) < MIN_BODY:
        return "too_short"
    return "full_text"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    results = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    provider = sys.argv[2] if len(sys.argv) > 2 else Path(sys.argv[1]).stem

    cases = pd.read_csv(CASES)
    cases["verdict"] = cases["url"].map(lambda u: classify(results.get(u)))
    cases["body_chars"] = cases["url"].map(
        lambda u: len((results.get(u) or {}).get("body") or "")
    )

    print(f"\nПровайдер: {provider}\n{'=' * 70}")

    per_domain = (cases.groupby(["domain", "access", "articles_n"])
                  .agg(кейсів=("verdict", "size"),
                       повний_текст=("verdict", lambda s: (s == "full_text").sum()),
                       сер_знаків=("body_chars", "mean"))
                  .reset_index()
                  .sort_values("articles_n", ascending=False))
    per_domain["сер_знаків"] = per_domain["сер_знаків"].round(0).astype(int)
    per_domain["ok"] = per_domain["повний_текст"] == per_domain["кейсів"]
    print(per_domain.to_string(index=False))

    print(f"\n{'─' * 70}\nРозподіл вердиктів")
    print(cases["verdict"].value_counts().to_string())

    # Головна цифра: покриття, зважене на реальний обсяг статей у дайджесті
    ok_domains = per_domain[per_domain["ok"]]
    total_volume = per_domain["articles_n"].sum()
    covered = ok_domains["articles_n"].sum()

    print(f"\n{'─' * 70}")
    print(f"Зважене покриття: {covered:,} із {total_volume:,} статей "
          f"({covered / total_volume:.1%})")
    print("  (домен зараховується, тільки якщо повний текст в УСІХ його кейсах)")

    failed = per_domain[~per_domain["ok"]]
    if len(failed):
        print(f"\nНе дає повного тексту — {failed['articles_n'].sum():,} статей потоку:")
        for _, r in failed.iterrows():
            print(f"  {r['domain']:<24} {r['access']:<14} {r['articles_n']:>4} статей")


if __name__ == "__main__":
    main()
