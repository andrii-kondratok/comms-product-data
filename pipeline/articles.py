"""core.article: створення, запис тексту, журнал спроб."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from .util import canonical, domain_of, match_source

GOOD_CHARS = 1500      # нижче — тизер
OVER_CHARS = 80_000    # вище — парсер зачепив навігацію, а не статтю
MAX_ATTEMPTS = 3
RETRY_HOURS = (1, 6, 24)


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text or "").strip().lower()
    return hashlib.sha256(norm.encode()).hexdigest()


def ensure(con, sources: dict, *, url: str, run_id=None, title=None, published_at=None,
           description=None) -> str | None:
    """Повертає article_id; створює рядок у статусі pending, якщо його ще немає."""
    cu = canonical(url)
    if not cu:
        return None
    src = match_source(domain_of(cu), sources)
    r = con.execute("""
        INSERT INTO core.article (url_canonical, url_original, source_id, title,
                                  published_at, description, first_seen_run)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (url_canonical) DO UPDATE SET
            title        = coalesce(core.article.title, EXCLUDED.title),
            published_at = coalesce(core.article.published_at, EXCLUDED.published_at),
            description  = coalesce(core.article.description, EXCLUDED.description),
            url_original = coalesce(core.article.url_original, EXCLUDED.url_original),
            source_id    = coalesce(core.article.source_id, EXCLUDED.source_id)
        RETURNING article_id""",
        (cu, url.split("#")[0], src["source_id"] if src else None, title, published_at,
         description, run_id)).fetchone()
    return r["article_id"]


def verdict_of(text: str) -> str:
    n = len(text or "")
    if n == 0:
        return "blocked_or_empty"
    if n > OVER_CHARS:
        return "over_extracted"
    if n < GOOD_CHARS:
        return "teaser"
    return "full"


def record(con, article_id, *, method: str, text: str | None, run_id=None,
           http_status=None, verified: bool | None = None, paywalled_source=False,
           error: str | None = None) -> str:
    """Записує спробу і, якщо текст повний, сам текст. Повертає вердикт."""
    verdict = "error" if error else verdict_of(text or "")
    con.execute("""INSERT INTO ops.fetch_attempt
                   (article_id, run_id, method, http_status, chars, verdict, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (article_id, run_id, method, str(http_status) if http_status else None,
                 len(text or ""), verdict, error))
    if verdict == "full":
        h = content_hash(text)
        # Той самий текст під іншою адресою (синдикація) — не дублюємо тіло
        dup = con.execute("SELECT article_id FROM core.article WHERE content_hash=%s "
                          "AND article_id<>%s", (h, article_id)).fetchone()
        con.execute("""UPDATE core.article SET body_text=%s, content_hash=%s,
                       retrieval_status='full_text', retrieval_method=%s,
                       text_verified=%s, last_error=NULL, next_retry_at=NULL,
                       retrieval_attempts=retrieval_attempts+1, updated_at=now()
                       WHERE article_id=%s""",
                    (text, None if dup else h, method, verified, article_id))
        return verdict
    # не вдалося: тизер/пейвол — остаточно, блок/помилка — повторимо пізніше
    row = con.execute("SELECT retrieval_attempts FROM core.article WHERE article_id=%s",
                      (article_id,)).fetchone()
    attempts = (row["retrieval_attempts"] if row else 0) + 1
    if verdict == "teaser":
        status, retry = ("paywall_stub" if paywalled_source else "too_short"), None
    else:
        status = "blocked" if verdict == "blocked_or_empty" else "error"
        retry = (datetime.now(timezone.utc) + timedelta(hours=RETRY_HOURS[min(attempts, 3) - 1])
                 if attempts < MAX_ATTEMPTS else None)
    con.execute("""UPDATE core.article SET retrieval_status =
                       CASE WHEN retrieval_status='full_text' THEN retrieval_status ELSE %s END,
                   retrieval_attempts=%s, next_retry_at=%s, last_error=%s, updated_at=now()
                   WHERE article_id=%s""",
                (status, attempts, retry, error or verdict, article_id))
    return verdict
