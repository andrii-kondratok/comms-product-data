"""Переносить редакційну категорію контенту в post_master.

Джерел три, і вони не однакові:
  * scrapper_clean_threads.csv → post_format_category (розмітка від колеги)
  * Post Metrics → Category
  * Content Pulse → Post Type

Спершу міряємо, наскільки вони збігаються, і лише потім зливаємо —
інакше ризикуємо затерти робочу розмітку гіршою.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

SRC = Path(r"C:\Users\Admin\Downloads\scrapper_clean_threads.csv")
DB = Path(__file__).parent / "posts.duckdb"

# Назви в джерелах різні, значення — ті самі поняття
NORMALISE = {
    "News / Analytical article / Commentary": "News / Analytical article",
    "News / Analytical article / Column": "News / Analytical article",
    "News / Analytical article": "News / Analytical article",
    "TM column / interview / speech": "TM column / interview / speech",
    "TM column / interview / speech / quote": "TM column / interview / speech",
    "Speech / Interview / Q&A": "Speech / Interview / Q&A",
    "Human Story": "Human Story",
    "Research": "Research",
    "KSE post": "KSE post",
    "Personal Reflection": "Personal Reflection",
    "Fundraising": "Fundraising",
    "Person Profile": "Person Profile",
    "Investigation article": "Investigation article",
    "Investigation article / video": "Investigation article",
}


def norm(v):
    return NORMALISE.get(v, v) if isinstance(v, str) and v.strip() else None


def main() -> None:
    ext = pd.read_csv(SRC, dtype=str, keep_default_na=False, na_values=[""],
                      encoding="utf-8-sig")[["id", "post_format_category"]]
    ext = ext.rename(columns={"id": "native_id",
                              "post_format_category": "cat_scraper"})
    ext["cat_scraper"] = ext["cat_scraper"].map(norm)
    ext = ext.dropna(subset=["cat_scraper"]).drop_duplicates("native_id")
    print(f"Розмітка від колеги: {len(ext):,} твітів")

    con = duckdb.connect(str(DB))
    con.register("ext_df", ext)

    print("\nНаявне покриття в post_master:")
    print(con.execute("""
        SELECT platform, count(*) AS постів,
               count(category) AS з_category,
               count(post_type) AS з_post_type
        FROM post_master GROUP BY 1
    """).df().to_string(index=False))

    # Наскільки збігаються там, де є обидві
    both = con.execute("""
        SELECT m.category, e.cat_scraper
        FROM post_master m JOIN ext_df e USING (native_id)
        WHERE m.category IS NOT NULL
    """).df()
    both["category"] = both["category"].map(norm)
    if len(both):
        agree = (both["category"] == both["cat_scraper"]).mean()
        print(f"\nПерекриття з Post Metrics.Category: {len(both):,} постів, "
              f"збіг {agree:.1%}")
        if agree < 0.95:
            print("  найчастіші розбіжності:")
            d = both[both["category"] != both["cat_scraper"]]
            print(d.groupby(["category", "cat_scraper"]).size()
                  .sort_values(ascending=False).head(6).to_string())

    # Зливаємо: наявна розмітка з Notion має пріоритет як редакційна,
    # скрейперська заповнює порожнечі
    con.execute("ALTER TABLE post_master ADD COLUMN IF NOT EXISTS content_category TEXT")
    con.execute("ALTER TABLE post_master ADD COLUMN IF NOT EXISTS content_category_src TEXT")
    con.execute("""
        UPDATE post_master AS m
        SET content_category = COALESCE(m.category, m.post_type, e.cat_scraper),
            content_category_src = CASE
                WHEN m.category IS NOT NULL THEN 'notion_post_metrics'
                WHEN m.post_type IS NOT NULL THEN 'notion_content_pulse'
                WHEN e.cat_scraper IS NOT NULL THEN 'scraper'
            END
        FROM ext_df AS e
        WHERE m.native_id = e.native_id
    """)
    con.execute("""
        UPDATE post_master
        SET content_category = COALESCE(category, post_type),
            content_category_src = CASE
                WHEN category IS NOT NULL THEN 'notion_post_metrics'
                WHEN post_type IS NOT NULL THEN 'notion_content_pulse' END
        WHERE content_category IS NULL
    """)

    print("\nПокриття після злиття:")
    print(con.execute("""
        SELECT content_category_src, count(*) AS постів
        FROM post_master WHERE content_category IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False))

    total, covered = con.execute("""
        SELECT count(*), count(content_category) FROM post_master
    """).fetchone()
    print(f"\nУсього {total:,} постів · із категорією {covered:,} "
          f"({covered / total:.1%})")

    print("\nРозподіл категорій:")
    print(con.execute("""
        SELECT content_category, count(*) AS постів
        FROM post_master WHERE content_category IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """).df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
