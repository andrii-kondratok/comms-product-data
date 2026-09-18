"""Кандидат, що потрапив у дайджест (є сторінка в 🧾 Articles), → outcome = ingested.

Це і розмічає пул: ingested — позитив, seen старше доби — кандидат у негативи.
"""

from __future__ import annotations


def run(ctx) -> dict:
    r = ctx.con.execute("""
        UPDATE ops.candidate_pool c
        SET outcome = 'ingested', article_id = coalesce(c.article_id, a.article_id)
        FROM core.article a
        WHERE a.url_canonical = c.url_canonical
          AND a.notion_page_id IS NOT NULL
          AND c.outcome = 'seen'""")
    return {"ingested": r.rowcount}
