"""Мапа витягуваності: що реально береться власним парсером, а що ні.

Замінює здогадки в колонці `access` реєстру виміром. Для кожного домену бере
до трьох наших статей і пробує витягти текст, порівнюючи з тим, що вже є в базі.

Важливо: ходимо з браузерним User-Agent. Без нього Reuters, AP, Axios і Politico
віддають 401/403 — і попередній замір через `trafilatura.fetch_url` показав по них
нуль, хоча сайти цілком відкриті. Антибот легко переплутати з пейволом.

Результат: data/processed/extractability.csv

Запуск:  python posts_db/map_extractability.py [--limit N]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib import error, request

import pandas as pd
import trafilatura

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "processed"
REG = Path(__file__).parent / "sources.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
}
DELAY = 1.5
TRIES = 3
GOOD_CHARS = 1500      # нижче цього — тизер, а не стаття
GOOD_RATIO = 0.60      # частка від того, що має редакція
# Верхня межа правдоподібності: zona.media віддав 71 тис. знаків проти наших 6 тис. —
# парсер зачепив навігацію цілої сторінки. Такий «успіх» отруює корпус гірше,
# ніж чесний нуль, тому позначаємо окремо.
OVER_RATIO = 3.0
OVER_CHARS = 80_000


def fetch(url: str) -> tuple[int | str, str]:
    try:
        with request.urlopen(request.Request(url, headers=HEADERS), timeout=30) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        return e.code, ""
    except Exception as e:                                   # noqa: BLE001
        return type(e).__name__, ""


def classify(chars: int, ours: float | None) -> str:
    if chars == 0:
        return "blocked_or_empty"
    ratio = chars / ours if ours else 0
    # Наша копія теж буває обрізана — тоді велика частка означає, що погана вона,
    # а не витяг. Тому надлишок ловимо лише коли є з чим порівнювати.
    if chars > OVER_CHARS or (ours and ours >= GOOD_CHARS and ratio > OVER_RATIO):
        return "over_extracted"
    if chars >= GOOD_CHARS and ratio >= GOOD_RATIO:
        return "full"
    if chars >= 300:
        return "partial"
    return "teaser"


def access_from_measurement(verdict: str, status) -> str:
    """Вердикт витягу → клас доступу. Антибот і пейвол — різні речі:
    у першому випадку сервер не пускає взагалі, у другому віддає сторінку
    без тексту. Плутати їх не можна, бо лікуються вони по-різному."""
    if verdict in ("full", "over_extracted"):
        return "open"
    if verdict in ("teaser", "partial"):
        return "hard_paywall" if status == 200 else "antibot"
    if verdict == "blocked_or_empty":
        return "antibot" if status != 200 else "hard_paywall"
    return "unknown"


def main() -> None:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    reg = pd.read_csv(REG)
    arts = pd.read_csv(OUT / "pg_article.csv")
    arts["dom"] = arts["url_canonical"].str.extract(
        r"https?://(?:www\.)?([^/?#]+)", expand=False).str.lower()
    arts = arts[arts["body_text"].notna()].sort_values("created_at", ascending=False)

    domains = reg["domain"].tolist()
    if limit:
        domains = domains[:limit]

    rows = []
    for i, d in enumerate(domains, 1):
        sub = arts[arts["dom"] == d].head(TRIES)
        if sub.empty:
            rows.append({"domain": d, "tried": 0, "status": "no_articles",
                         "chars": 0, "ours": None, "verdict": "untested"})
            print(f"  {i:>2}/{len(domains)} {d:<26} немає статей у базі")
            continue

        best = {"chars": 0, "ours": None, "status": None}
        for _, r in sub.iterrows():
            status, html = fetch(r["url_canonical"])
            text = trafilatura.extract(html, include_comments=False,
                                       include_tables=False) if html else None
            n = len(text or "")
            if n > best["chars"]:
                best = {"chars": n, "ours": len(r["body_text"]), "status": status}
            time.sleep(DELAY)
            if best["chars"] >= GOOD_CHARS and best["ours"] \
                    and best["chars"] / best["ours"] >= GOOD_RATIO:
                break                                   # досить, домен працює

        verdict = classify(best["chars"], best["ours"])
        ratio = best["chars"] / best["ours"] if best["ours"] else 0
        rows.append({"domain": d, "tried": len(sub), "status": best["status"],
                     "chars": best["chars"], "ours": best["ours"],
                     "ratio": round(ratio, 2), "verdict": verdict})
        print(f"  {i:>2}/{len(domains)} {d:<26} {str(best['status']):<6} "
              f"{verdict:<17} {best['chars']:>7,} / {best['ours'] or 0:>7,.0f}  {ratio:>5.0%}")

    df = pd.DataFrame(rows)
    df = df.merge(reg[["domain", "access", "category", "articles_n"]], on="domain", how="left")
    df.to_csv(OUT / "extractability.csv", index=False)

    print(f"\n{'─' * 70}\nПідсумок за вердиктом")
    s = df.groupby("verdict").agg(доменів=("domain", "size"),
                                  статей=("articles_n", "sum")).sort_values("статей", ascending=False)
    print(s.to_string())

    print(f"\n{'─' * 70}\nРозбіжності з реєстром — там, де `access` бреше")
    mism = df[((df["access"] == "hard_paywall") & (df["verdict"] == "full"))
              | (df["access"].isin(["open", "metered"]) & (df["verdict"].isin(["teaser", "blocked_or_empty"])))]
    if len(mism):
        print(mism[["domain", "access", "verdict", "chars", "ratio", "articles_n"]]
              .sort_values("articles_n", ascending=False).to_string(index=False))
    else:
        print("  немає")

    print(f"\nЗаписано → {OUT / 'extractability.csv'}")


if __name__ == "__main__":
    main()
