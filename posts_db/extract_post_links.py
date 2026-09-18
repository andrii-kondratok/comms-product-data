"""Витяг посилань із текстів постів і зіставлення зі статтями.

Мета — відновити пари «стаття → пост», яких немає в Notion: `Source Article 1`
заповнено у 2 рядках із 8 797. Але сам ТМ у більшості постів дає лінк на
джерело просто в тексті, тож зв'язок уже записаний, його треба лише дістати.

Крок 1 (цей скрипт): витягти URL, канонікалізувати, зіставити з core.article.
Крок 2 (за потреби): розкрити скорочені t.co, якщо їх багато.

Запуск:  python posts_db/extract_post_links.py
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).parent.parent
DB = Path(__file__).parent / "posts.duckdb"
OUT = ROOT / "data" / "processed"

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.I)
TRACKING = re.compile(
    r"[?&](utm_[^=&]+|fbclid|gclid|srnd|mod|ref|ref_src|ref_url|s|t|"
    r"mibextid|syn-[^=&]+|smid|smtyp)=[^&]*", re.I)

# Домени, які не є джерелами статей
SELF = {"x.com", "twitter.com", "facebook.com", "fb.com", "fb.me",
        "instagram.com", "threads.net", "threads.com", "youtube.com",
        "youtu.be", "t.me", "linkedin.com", "tiktok.com"}


def canonical(url: str) -> str:
    u = url.rstrip(".,;:!?»\")")
    u = TRACKING.sub("", u)
    u = re.sub(r"[?&]+$", "", u)
    u = re.sub(r"^https?://(?:www\.)?", "https://", u, flags=re.I)
    return u.rstrip("/")


def domain(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url, re.I)
    return m.group(1).lower().removeprefix("www.") if m else ""


def main() -> None:
    con = duckdb.connect(str(DB))
    posts = con.execute("""
        SELECT native_id, platform, posted_at, text_best, text_origin
        FROM post_master
        WHERE text_best IS NOT NULL AND platform IN ('X', 'FB')
    """).df()
    print(f"Постів із текстом: {len(posts):,}")

    rows = []
    for _, p in posts.iterrows():
        for raw in URL_RE.findall(p["text_best"]):
            u = canonical(raw)
            rows.append({"native_id": p["native_id"], "platform": p["platform"],
                         "posted_at": p["posted_at"], "url": u, "domain": domain(u),
                         "from": "published"})

    # Опублікований текст рідко несе лінк: на X він іде окремим твітом або
    # ховається за t.co. А ось у драфті Content Pulse редактор лишає джерело —
    # там посилання є у 78% записів. Для відновлення пар це краще джерело.
    drafts = con.execute("""
        SELECT c.pulse_page_id, c.body, m.native_id, m.platform, m.posted_at
        FROM content_pulse c
        LEFT JOIN post_master m ON m.pulse_page_id = c.pulse_page_id
        WHERE c.body IS NOT NULL AND c.body LIKE '%http%'
    """).df()
    print(f"Драфтів Content Pulse із лінком: {len(drafts):,} "
          f"(з них прив'язані до поста: {drafts['native_id'].notna().sum():,})")

    for _, d in drafts.iterrows():
        for raw in URL_RE.findall(d["body"]):
            u = canonical(raw)
            rows.append({"native_id": d["native_id"], "platform": d["platform"],
                         "posted_at": d["posted_at"], "url": u, "domain": domain(u),
                         "from": "draft", "pulse_page_id": d["pulse_page_id"]})

    links = pd.DataFrame(rows)
    print(f"Знайдено посилань: {len(links):,} "
          f"у {links['native_id'].nunique():,} постах "
          f"({links['native_id'].nunique() / len(posts):.1%} від усіх)")

    print("\nТоп доменів у посиланнях:")
    top = links["domain"].value_counts().head(15)
    for d, n in top.items():
        tag = "  (власне/соцмережа)" if d in SELF else ""
        tag = "  ← скорочувач, треба розкривати" if d == "t.co" else tag
        print(f"  {d:<28} {n:>6,}{tag}")

    links["is_external"] = ~links["domain"].isin(SELF) & (links["domain"] != "t.co")
    ext = links[links["is_external"]]
    print(f"\nЗовнішніх посилань (кандидати на статті): {len(ext):,} "
          f"у {ext['native_id'].nunique():,} постах")
    print(f"Скорочених t.co: {(links['domain'] == 't.co').sum():,} "
          f"у {links[links['domain'] == 't.co']['native_id'].nunique():,} постах")

    # ── Зіставлення зі статтями ──
    arts = con.execute("""
        SELECT url_canonical, title FROM read_csv_auto(?, header=true)
    """, [str(OUT / "pg_article.csv")]).df()
    arts["key"] = arts["url_canonical"].map(
        lambda u: canonical(u) if isinstance(u, str) else None)
    art_keys = dict(zip(arts["key"], arts["title"]))
    print(f"\nСтатей у базі: {len(arts):,}")

    ext = ext.copy()
    ext["matched"] = ext["url"].isin(art_keys)
    matched = ext[ext["matched"]]
    print(f"Прямих збігів URL зі статтями: {len(matched):,} "
          f"у {matched['native_id'].nunique():,} постах")

    # Часткове зіставлення: та сама стаття, але URL трохи інакший
    art_domains = Counter(domain(k) for k in art_keys if k)
    ext["domain_known"] = ext["domain"].isin(art_domains)
    print(f"Посилань на домени, які є серед джерел дайджесту: "
          f"{ext['domain_known'].sum():,}")

    links.to_csv(OUT / "post_links.csv", index=False)
    matched[["native_id", "platform", "posted_at", "url", "domain", "from"]].to_csv(
        OUT / "article_post_link.csv", index=False)
    print(f"\nЗаписано → post_links.csv ({len(links):,}) · "
          f"article_post_link.csv ({len(matched):,})")

    print("\nЗбіги за роками:")
    if len(matched):
        m = matched.copy()
        m["рік"] = pd.to_datetime(m["posted_at"], format="mixed",
                                  utc=True, errors="coerce").dt.year
        print(m.groupby("рік")["native_id"].nunique().to_string())
    con.close()


if __name__ == "__main__":
    main()
