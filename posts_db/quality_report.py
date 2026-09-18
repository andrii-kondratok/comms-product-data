"""Звіт якості бази постів. Нічого не змінює, тільки показує стан."""

from __future__ import annotations

from pathlib import Path

import duckdb

DB = Path(__file__).parent / "posts.duckdb"


def show(con, title: str, sql: str) -> None:
    print(f"\n{'─' * 76}\n{title}\n{'─' * 76}")
    df = con.execute(sql).df()
    print(df.to_string(index=False) if len(df) else "  (порожньо)")


def main() -> None:
    con = duckdb.connect(str(DB), read_only=True)

    show(con, "1. Покриття: постів за платформою і роком", """
        SELECT year(posted_at) AS рік,
               count(*) FILTER (WHERE platform='X')  AS X,
               count(*) FILTER (WHERE platform='FB') AS FB,
               count(*) AS усього
        FROM post WHERE posted_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)

    show(con, "2. Нумерація треду за роками — вимір замість оцінки", """
        SELECT year(posted_at) AS рік,
               count(*) AS постів,
               count(*) FILTER (WHERE numbering_style='leading')  AS на_початку,
               count(*) FILTER (WHERE numbering_style='trailing') AS у_кінці,
               round(100.0 * count(*) FILTER (WHERE numbering_style='trailing') / count(*), 1) AS у_кінці_pct
        FROM post WHERE platform='X' AND posted_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)

    show(con, "3. Підозра на обрізання (рівно 200 знаків)", """
        SELECT platform, count(*) AS постів,
               count(*) FILTER (WHERE is_truncated_suspect) AS обрізаних,
               round(avg(hook_chars), 0) AS сер_довжина,
               max(hook_chars) AS макс
        FROM post GROUP BY 1 ORDER BY 1
    """)

    show(con, "4. Заповненість метрик", """
        SELECT m.metric, count(*) AS значень,
               round(median(m.value), 0) AS медіана,
               max(m.value) AS максимум
        FROM post_metric m GROUP BY 1 ORDER BY 2 DESC
    """)

    show(con, "5. Проблеми завантаження", """
        SELECT s.kind, i.issue, count(*) AS n
        FROM load_issue i JOIN source_file s USING (source_file_id)
        GROUP BY 1,2 ORDER BY 3 DESC
    """)

    show(con, "6. Внесок кожного джерела після дедуплікації", """
        SELECT s.kind, s.rows_read AS прочитано,
               count(p.post_id) AS лишилось_у_базі
        FROM source_file s LEFT JOIN post p USING (source_file_id)
        GROUP BY 1,2 ORDER BY 3 DESC
    """)

    show(con, "7. Придатність для fine-tune: X-пости з непорожнім чистим текстом", """
        SELECT year(posted_at) AS рік,
               count(*) FILTER (WHERE length(hook_clean) >= 80) AS придатних,
               count(*) AS усього
        FROM post WHERE platform='X' AND posted_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)

    show(con, "8. Порожнечі в календарі — дні без жодного поста (топ-10 розривів)", """
        WITH d AS (
            SELECT DISTINCT posted_date FROM post
            WHERE platform='X' AND posted_date IS NOT NULL
        ), g AS (
            SELECT posted_date,
                   lag(posted_date) OVER (ORDER BY posted_date) AS prev
            FROM d
        )
        SELECT prev AS від, posted_date AS до,
               (posted_date - prev) AS днів_розриву
        FROM g WHERE prev IS NOT NULL AND (posted_date - prev) > 7
        ORDER BY 3 DESC LIMIT 10
    """)

    con.close()


if __name__ == "__main__":
    main()
