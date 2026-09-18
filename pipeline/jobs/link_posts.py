"""Пари стаття → пост із явних посилань, для постів, оновлених з останнього прогону.

Джерела зв'язку, у порядку довіри:
  source_field — `Source / Evidence (regex)` у Post Metrics;
  url_in_post  — посилання в самому тексті поста (t.co розгортаємо).
Статті, якої ще немає, створюється рядок у pending — fetch_articles її дотягне.
"""

from __future__ import annotations

import re
from urllib import error, request

from .. import articles, candidates, db
from ..util import canonical, domain_of, looks_like_article

URL = re.compile(r"https?://[^\s<>\"'\)\]…]+", re.I)
NOT_ARTICLE = ("facebook.", "x.com", "twitter.com", "t.me", "instagram.", "youtube.",
               "youtu.be", "tiktok.", "linkedin.", "threads.", "kse.ua", "kse.org.ua",
               "notion.", "google.com", "forms.gle", "prod-files-secure")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = request.build_opener(_NoRedirect)


def expand(url: str) -> str:
    """t.co → справжня адреса одним HEAD-запитом, без переходу за редиректом."""
    if "t.co/" not in url:
        return url
    try:
        _OPENER.open(request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"}), timeout=15)
    except error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location") or url
    except Exception:                                            # noqa: BLE001
        pass
    return url


def is_article(url: str) -> bool:
    d = domain_of(url)
    return bool(d) and not any(x in d for x in NOT_ARTICLE)


def run(ctx) -> dict:
    con = ctx.con
    sources = candidates.load_sources(con)
    since = db.get_watermark(con, ctx.job, "post.updated_at", "1970-01-01")
    posts = con.execute("""SELECT post_id, source_url, body_raw, hook_raw, updated_at
                           FROM core.post WHERE updated_at > %s
                           ORDER BY updated_at LIMIT 5000""", (since,)).fetchall()
    stats = {"posts": len(posts), "links": 0, "new_articles": 0}
    for p in posts:
        found = []
        m = URL.search(p["source_url"] or "")
        if m:
            found.append((m.group(0).rstrip(".,;:"), "source_field"))
        for u in URL.findall(p["body_raw"] or p["hook_raw"] or ""):
            found.append((expand(u.rstrip(".,;:!?»")), "url_in_post"))
        seen = set()
        for url, evidence in found:
            if not is_article(url) or not looks_like_article(url):
                continue
            cu = canonical(url)
            if not cu or cu in seen:
                continue
            seen.add(cu)
            existed = con.execute("SELECT 1 FROM core.article WHERE url_canonical=%s",
                                  (cu,)).fetchone()
            aid = articles.ensure(con, sources, url=url, run_id=ctx.run_id)
            stats["new_articles"] += not existed
            con.execute("""INSERT INTO core.article_post_link
                           (article_id, post_id, link_method, link_evidence, confidence)
                           VALUES (%s,%s,'explicit_url',%s,1.0)
                           ON CONFLICT (article_id, post_id) DO NOTHING""",
                        (aid, p["post_id"], evidence))
            stats["links"] += 1
    if posts:
        db.set_watermark(con, ctx.job, "post.updated_at", posts[-1]["updated_at"].isoformat())
    return stats
