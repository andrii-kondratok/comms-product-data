"""Текст статей: свіжі кандидати + черга повторів.

Спосіб дотягування задається в реєстрі (core.source.fetch_cascade), а не в коді:
  {own_extractor}                 — відкриті видання, наш trafilatura
  {newscatcher_v3, own_extractor} — антибот (Reuters, CNN, AP, Axios, Politico)
  {}                              — жорсткий пейвол: не пробуємо, лише редакція

Каскад іде позиція за позицією: спершу перший спосіб для всіх, хто його має
(NewsCatcher — пачкою по 100), далі другий для тих, кому не вдалося.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .. import articles, candidates, newscatcher
from ..util import http_get

FRESH_DAYS = 3        # кандидатів старших за це не дотягуємо — редактор їх уже не візьме
MAX_PER_RUN = 300     # ~6 хв власного витягу; решта — наступним запуском
DELAY = 1.2


def extract(url: str) -> tuple:
    import trafilatura                     # важкий імпорт — лише тут
    st, body = http_get(url)
    if st != 200 or not body:
        return st, ""
    text = trafilatura.extract(body.decode("utf-8", "replace"), include_comments=False,
                               include_tables=False, favor_precision=True) or ""
    return st, text


def run(ctx) -> dict:
    con = ctx.con
    sources = candidates.load_sources(con)
    since = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
    stats = {"new_articles": 0, "queued": 0, "full": 0, "teaser": 0, "blocked": 0,
             "skipped_no_cascade": 0}

    # 1. свіжі кандидати без статті → статті в статусі pending
    fresh = con.execute("""
        SELECT candidate_id, coalesce(url_original, url_canonical) AS url, title,
               published_at, description
        FROM ops.candidate_pool
        WHERE article_id IS NULL AND source_id IS NOT NULL
          AND coalesce(published_at, first_seen_at) >= %s
        ORDER BY coalesce(published_at, first_seen_at) DESC
        LIMIT %s""", (since, MAX_PER_RUN)).fetchall()
    for c in fresh:
        aid = articles.ensure(con, sources, url=c["url"], run_id=ctx.run_id, title=c["title"],
                              published_at=c["published_at"], description=c["description"])
        con.execute("UPDATE ops.candidate_pool SET article_id=%s WHERE candidate_id=%s",
                    (aid, c["candidate_id"]))
        stats["new_articles"] += 1

    # 2. черга: pending + повтори, чий час настав
    queue = con.execute("""
        SELECT a.article_id, coalesce(a.url_original, a.url_canonical) AS url,
               s.fetch_cascade, s.access_class
        FROM core.article a
        JOIN core.source s USING (source_id)
        WHERE a.retrieval_status IN ('pending','blocked','error')
          AND (a.next_retry_at IS NULL OR a.next_retry_at <= now())
          AND a.retrieval_attempts < %s
          AND coalesce(a.published_at, a.ingested_at) >= now() - interval '30 days'
        ORDER BY a.retrieval_attempts, coalesce(a.published_at, a.ingested_at) DESC
        LIMIT %s""", (articles.MAX_ATTEMPTS, MAX_PER_RUN)).fetchall()
    stats["queued"] = len(queue)

    pending = [q for q in queue if q["fetch_cascade"]]
    stats["skipped_no_cascade"] = len(queue) - len(pending)
    for pos in range(3):
        step = [q for q in pending if len(q["fetch_cascade"]) > pos]
        if not step:
            break
        done = set()
        nc = [q for q in step if q["fetch_cascade"][pos] == "newscatcher_v3"]
        if nc and newscatcher.budget_left(ctx) > 0:
            found = newscatcher.by_links(ctx, [q["url"] for q in nc])
            for q in nc:
                a = found.get(q["url"])
                dom = q["url"].split("/")[2].removeprefix("www.")
                text = newscatcher.usable_text(a, dom) if a else None
                v = articles.record(con, q["article_id"], method="newscatcher_v3", text=text,
                                    run_id=ctx.run_id, http_status=200 if a else 404,
                                    verified=True,
                                    paywalled_source=q["access_class"] == "hard_paywall")
                stats[{"full": "full", "teaser": "teaser"}.get(v, "blocked")] += 1
                if v == "full":
                    done.add(q["article_id"])
        for q in step:
            if q["fetch_cascade"][pos] != "own_extractor" or q["article_id"] in done:
                continue
            st, text = extract(q["url"])
            v = articles.record(con, q["article_id"], method="own_extractor", text=text,
                                run_id=ctx.run_id, http_status=st, verified=True,
                                paywalled_source=q["access_class"] == "hard_paywall")
            stats[{"full": "full", "teaser": "teaser"}.get(v, "blocked")] += 1
            if v == "full":
                done.add(q["article_id"])
            time.sleep(DELAY)
        pending = [q for q in pending if q["article_id"] not in done]
    return stats
