"""Запис у пул кандидатів: один рядок на статтю, повторні бачення — лічильником."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .util import canonical, domain_of, match_source

UPSERT = """
INSERT INTO ops.candidate_pool
    (run_id, url_canonical, url_original, source_id, title, published_at, provider,
     providers, topics, lang, description, provider_cluster_id, cluster_size,
     first_seen_at, last_seen_at, seen_at)
VALUES (%(run_id)s, %(url)s, %(orig)s, %(source_id)s, %(title)s, %(published_at)s, %(provider)s,
        ARRAY[%(provider)s], %(topics)s, %(lang)s, %(description)s, %(cluster_id)s, %(cluster_size)s,
        now(), now(), now())
ON CONFLICT (url_canonical) DO UPDATE SET
    last_seen_at  = now(),
    seen_count    = ops.candidate_pool.seen_count + 1,
    providers     = ARRAY(SELECT DISTINCT unnest(ops.candidate_pool.providers || EXCLUDED.providers)),
    topics        = ARRAY(SELECT DISTINCT unnest(ops.candidate_pool.topics || EXCLUDED.topics)),
    title         = coalesce(ops.candidate_pool.title, EXCLUDED.title),
    published_at  = coalesce(ops.candidate_pool.published_at, EXCLUDED.published_at),
    description   = coalesce(ops.candidate_pool.description, EXCLUDED.description),
    lang          = coalesce(ops.candidate_pool.lang, EXCLUDED.lang),
    url_original  = coalesce(ops.candidate_pool.url_original, EXCLUDED.url_original),
    source_id     = coalesce(ops.candidate_pool.source_id, EXCLUDED.source_id),
    provider_cluster_id = coalesce(EXCLUDED.provider_cluster_id, ops.candidate_pool.provider_cluster_id),
    cluster_size  = greatest(ops.candidate_pool.cluster_size, EXCLUDED.cluster_size)
RETURNING (xmax = 0) AS inserted
"""


def parse_date(raw) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    for fn in (lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
               parsedate_to_datetime):
        try:
            d = fn(s)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:                                        # noqa: BLE001
            continue
    return None


def load_sources(con) -> dict:
    rows = con.execute("SELECT * FROM core.source WHERE is_active").fetchall()
    return {r["domain"]: r for r in rows}


def upsert(con, run_id, sources: dict, *, url: str, provider: str, title=None,
           published=None, topics=None, lang=None, description=None,
           cluster_id=None, cluster_size=None) -> bool | None:
    """True — новий кандидат, False — уже бачили, None — посилання непридатне."""
    cu = canonical(url)
    if not cu:
        return None
    src = match_source(domain_of(cu), sources)
    r = con.execute(UPSERT, {
        "run_id": run_id, "url": cu, "orig": url.split("#")[0],
        "source_id": src["source_id"] if src else None,
        "title": title, "published_at": parse_date(published), "provider": provider,
        "topics": list(topics or []), "lang": lang, "description": description,
        "cluster_id": cluster_id, "cluster_size": cluster_size,
    }).fetchone()
    return r["inserted"]
