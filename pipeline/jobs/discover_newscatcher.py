"""Тематичні запити NewsCatcher за 24 год → пул кандидатів (+ текст, де він повний).

Запити — з плейбука NewsCatcher під наші чотири теми. Кластеризація дає
cluster_size: скільки видань написали про ту саму подію — сигнал пріоритету.
Той самий URL із RSS і звідси — один кандидат із двома провайдерами.

Бюджет: NEWSCATCHER_MAX_CALLS_PER_DAY. Задача зупиняється, коли його вичерпано.
"""

from __future__ import annotations

import json

from .. import articles, candidates, newscatcher
from ..util import canonical, domain_of

QUERIES = {
    "economy": [
        '(Ukraine OR Ukrainian) AND (GDP OR inflation OR hryvnia OR "National Bank of Ukraine" '
        'OR NBU OR "state budget" OR IMF OR "World Bank" OR reconstruction)',
        '(Russia OR Russian) AND (ruble OR rouble OR "budget deficit" OR "oil revenues" '
        'OR "National Wealth Fund" OR "key rate" OR "Bank of Russia" OR Urals)',
    ],
    "war": [
        '(Ukraine OR Ukrainian) AND (offensive OR counteroffensive OR frontline OR '
        '"missile strike" OR "drone attack" OR Shahed OR Iskander OR "air defense")',
        'Ukraine AND ("military aid" OR "aid package" OR Patriot OR "F-16" OR ATACMS '
        'OR Taurus OR "long-range" OR "defense industry")',
        '(Ukraine OR Russia) AND (ceasefire OR "peace talks" OR negotiations OR "peace plan" '
        'OR "security guarantees" OR "prisoner exchange")',
    ],
    "sanctions": [
        '(Russia OR Russian) AND (sanction* OR "price cap" OR "shadow fleet" OR "frozen assets" '
        'OR "secondary sanctions" OR "export controls" OR "sanctions package")',
    ],
    "security": [
        '(NATO OR "European Union" OR Pentagon OR Bundeswehr) AND (Ukraine OR Russia) AND '
        '(deterrence OR "defense spending" OR "Article 5" OR "eastern flank" OR "security guarantees")',
        '(Russia OR Russian OR Kremlin) AND (Moldova OR Georgia OR Belarus OR Baltic* OR '
        'Kaliningrad OR "Black Sea" OR Arctic) AND (hybrid OR sabotage OR espionage OR '
        '"drone incursion" OR "airspace violation")',
    ],
}


def run(ctx) -> dict:
    con = ctx.con
    sources = candidates.load_sources(con)
    domains = [d for d, s in sources.items() if "newscatcher" in (s.get("discovery") or [])] \
        or [d for d, s in sources.items() if s.get("declared_in_digest")]
    stats = {"calls": 0, "articles": 0, "new": 0, "full_text": 0, "budget_stop": False}
    if not domains:
        return {**stats, "note": "немає джерел для newscatcher у реєстрі"}

    for topic, qs in QUERIES.items():
        for q in qs:
            if newscatcher.budget_left(ctx) <= 0:
                stats["budget_stop"] = True
                return stats
            body = {
                "q": q, "search_in": "title_content,title_content_translated",
                "sources": ",".join(domains), "from_": "24h", "sort_by": "date",
                "page_size": 500, "exclude_duplicates": True,
                "clustering_enabled": True, "clustering_threshold": 0.7,
                "include_translation_fields": True,
            }
            found = newscatcher.search(ctx, body)
            stats["calls"] += 1
            for a in found:
                link = a.get("link")
                if not link:
                    continue
                stats["articles"] += 1
                new = candidates.upsert(
                    con, ctx.run_id, sources, url=link, provider="newscatcher",
                    title=a.get("title"), published=a.get("published_date"),
                    topics=[topic], lang=a.get("language"), description=a.get("description"),
                    cluster_id=a.get("_cluster_id"), cluster_size=a.get("_cluster_size"))
                stats["new"] += bool(new)

                # Сира відповідь — окремо від розібраного
                con.execute("""INSERT INTO raw.article_payload
                               (provider, provider_id, requested_url, http_status, payload, run_id)
                               VALUES (%s,%s,%s,200,%s,%s)""",
                            (newscatcher.PROVIDER, a.get("id"), link,
                             json.dumps({k: v for k, v in a.items() if k != "content"}),
                             ctx.run_id))

                text = newscatcher.usable_text(a, domain_of(canonical(link) or ""))
                if text:
                    aid = articles.ensure(con, sources, url=link, run_id=ctx.run_id,
                                          title=a.get("title"),
                                          published_at=candidates.parse_date(a.get("published_date")),
                                          description=a.get("description"))
                    cur = con.execute("SELECT retrieval_status FROM core.article WHERE article_id=%s",
                                      (aid,)).fetchone()
                    if cur["retrieval_status"] != "full_text":
                        articles.record(con, aid, method="newscatcher_v3", text=text,
                                        run_id=ctx.run_id, http_status=200, verified=True)
                        stats["full_text"] += 1
                    con.execute("UPDATE ops.candidate_pool SET article_id=%s "
                                "WHERE url_canonical=%s AND article_id IS NULL",
                                (aid, canonical(link)))
    return stats
