"""Дві задачі: перевірити гіпотезу про повноту текстів FB і застосувати
надійно зібрані треди X до post_master.

Гіпотеза про FB: дописи Facebook не мають тредів, тож `hook_raw` із локального
експорту має бути повним текстом, а не обрізком. Перевіряємо не на віру, а
порівнянням на перетині: для постів, які є і в локальному експорті, і в Notion
із полем «Post text», довжини мають збігатися.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent.parent / "postgres"))
from load_posts import clean, numbering_counts, numbering_style  # noqa: E402

DB = Path(__file__).parent / "posts.duckdb"
ASM = Path(__file__).parent.parent / "data" / "processed" / "assembled_threads.csv"

TRUNC_HINTS = re.compile(r"(?:\.\.\.|…|See more|Показати більше|Ще)\s*$", re.I)


def verify_facebook(con: duckdb.DuckDBPyConnection) -> None:
    print("=" * 74)
    print("ГІПОТЕЗА: тексти FB із локального експорту повні, а не обрізані")
    print("=" * 74)

    # Перетин: пости FB, де є і локальний текст, і текст із Notion
    overlap = con.execute("""
        SELECT p.native_id,
               length(p.hook_raw)  AS local_len,
               length(n.metrics_text) AS notion_len,
               p.hook_raw, n.metrics_text
        FROM post p
        JOIN (
            SELECT native_id, text_best AS metrics_text
            FROM post_master
            WHERE platform = 'FB' AND text_origin = 'notion_post_metrics'
        ) n USING (native_id)
        WHERE p.platform = 'FB' AND p.hook_raw IS NOT NULL
    """).df()

    if len(overlap) == 0:
        print("Перетину немає — порівняти напряму не вдається.")
    else:
        same = (overlap["local_len"] == overlap["notion_len"]).sum()
        longer = (overlap["local_len"] > overlap["notion_len"]).sum()
        shorter = (overlap["local_len"] < overlap["notion_len"]).sum()
        print(f"Постів FB у перетині двох джерел: {len(overlap):,}")
        print(f"  однакова довжина:        {same:,} ({same / len(overlap):.1%})")
        print(f"  локальний ДОВШИЙ:        {longer:,}")
        print(f"  локальний КОРОТШИЙ:      {shorter:,}  ← лише це означало б обрізання")
        if shorter:
            d = overlap[overlap["local_len"] < overlap["notion_len"]]
            print(f"     медіана нестачі: {(d['notion_len'] - d['local_len']).median():.0f} знаків")

    # Непрямі ознаки обрізання на всьому масиві FB
    stats = con.execute("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE length(text_best) = 200) AS at_200,
               count(*) FILTER (WHERE length(text_best) = 500) AS at_500,
               count(*) FILTER (WHERE length(text_best) = 1000) AS at_1000,
               CAST(median(length(text_best)) AS INT) AS med,
               max(length(text_best)) AS mx
        FROM post_master
        WHERE platform = 'FB' AND text_origin = 'local_export'
    """).df().iloc[0]
    print(f"\nУсі FB із локального експорту: {stats['n']:,}")
    print(f"  медіана {stats['med']:,} знаків, максимум {stats['mx']:,}")
    print(f"  рівно 200 / 500 / 1000 знаків: "
          f"{stats['at_200']} / {stats['at_500']} / {stats['at_1000']}"
          f"   ← скупчення вказувало б на ліміт")

    tail = con.execute("""
        SELECT text_best AS hook_raw FROM post_master
        WHERE platform = 'FB' AND text_origin = 'local_export' AND text_best IS NOT NULL
    """).df()["hook_raw"]
    hint = tail.str.contains(TRUNC_HINTS, regex=True, na=False).sum()
    print(f"  закінчуються на «…» / «See more» / «Показати більше»: {hint:,}")

    verdict = ("ПІДТВЕРДЖЕНО" if len(overlap) and shorter / max(len(overlap), 1) < 0.05
               else "ПОТРЕБУЄ УВАГИ")
    print(f"\nВисновок: {verdict}")


def apply_threads(con: duckdb.DuckDBPyConnection) -> None:
    print("\n" + "=" * 74)
    print("ЗАСТОСУВАННЯ НАДІЙНО ЗІБРАНИХ ТРЕДІВ")
    print("=" * 74)

    con.execute(f"""
        CREATE OR REPLACE TABLE asm AS
        SELECT * FROM read_csv_auto('{ASM.as_posix()}', header=true)
    """)

    target = con.execute("""
        SELECT p.native_id, a.full_text, a.tweet_count
        FROM post_master p
        JOIN asm a ON CAST(a.root_tweet_id AS VARCHAR) = p.native_id
        WHERE p.platform = 'X'
          AND p.text_origin = 'local_export'
          AND a.tweet_count > 1
          AND a.starts_at_one
          AND a.missing_tweets = 0
    """).df()
    print(f"Постів до оновлення: {len(target):,}")

    target["text_clean"] = target["full_text"].map(clean)
    target["numbering_style"] = target["full_text"].map(numbering_style)
    counts = target["full_text"].map(numbering_counts)
    target["numbering_lead_n"] = counts.map(lambda c: c[0])
    target["numbering_trail_n"] = counts.map(lambda c: c[1])
    target["chars"] = target["full_text"].str.len()

    con.register("upd", target)
    con.execute("""
        UPDATE post_master AS p
        SET text_best = u.full_text,
            text_clean = u.text_clean,
            text_origin = 'assembled_archive',
            text_chars = u.chars,
            numbering_style = u.numbering_style,
            numbering_lead_n = u.numbering_lead_n,
            numbering_trail_n = u.numbering_trail_n,
            is_truncated = false
        FROM upd AS u
        WHERE p.native_id = u.native_id AND p.platform = 'X'
    """)

    print(f"  середній приріст тексту: "
          f"{target['chars'].mean():,.0f} знаків проти ~260 у хука")

    print("\nСтан після оновлення:")
    print(con.execute("""
        SELECT text_origin, count(*) AS постів,
               CAST(median(text_chars) AS INT) AS медіана
        FROM post_master GROUP BY 1 ORDER BY 2 DESC
    """).df().to_string(index=False))

    print("\nПовні треди X за роками:")
    print(con.execute("""
        SELECT substr(posted_at,1,4) AS рік,
               count(*) FILTER (WHERE text_origin IN
                   ('notion_post_metrics','notion_content_pulse','assembled_archive')) AS повних,
               count(*) AS усього
        FROM post_master WHERE platform='X' AND posted_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))


def main() -> None:
    con = duckdb.connect(str(DB))
    verify_facebook(con)
    apply_threads(con)
    con.close()


if __name__ == "__main__":
    main()
