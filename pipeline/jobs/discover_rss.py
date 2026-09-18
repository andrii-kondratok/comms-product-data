"""RSS і news-sitemap усіх джерел → ops.candidate_pool.

Санкціонований видавцем канал: стрічку й новинний sitemap публікують навмисно.
Звідси повний перелік того, що вийшло; різниця з дайджестом — негативи для
моделі релевантності. Втрачене тут не відновлюється, тому задача щогодинна.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from .. import candidates
from ..util import BROWSER_HEADERS, http_get, looks_like_article

FEED_HEADERS = {**BROWSER_HEADERS, "Accept": "application/rss+xml,application/xml,text/xml,*/*"}
TAGS = re.compile(r"<[^>]+>")
# Загальний sitemap містить увесь сайт і архів за роки. Кандидат — те, що редактор
# міг узяти сьогодні, тож із sitemap беремо лише свіже.
SITEMAP_FRESH_DAYS = 3
DELAY = 1.0


def _text(el) -> str | None:
    if el is None or not el.text:
        return None
    return TAGS.sub("", el.text).strip() or None


def parse_feed(body: bytes) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter():
        if not (item.tag.endswith("item") or item.tag.endswith("entry")):
            continue
        link = title = pub = desc = None
        for ch in item:
            tag = ch.tag.split("}")[-1].lower()
            if tag == "link":
                link = link or (ch.text or "").strip() or ch.attrib.get("href")
            elif tag == "title":
                title = title or _text(ch)
            elif tag in ("pubdate", "published", "updated", "date"):
                pub = pub or (ch.text or "").strip()
            elif tag in ("description", "summary"):
                desc = desc or _text(ch)
        if link:
            out.append({"url": link, "title": title, "published": pub, "description": desc})
    return out


def parse_sitemap(body: bytes, depth: int = 0) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    kids = [e for e in root.iter() if e.tag.endswith("sitemap")]
    if kids and depth == 0:
        out = []
        for c in kids[:2]:                  # індекс: перші два — найсвіжіші
            loc = next((x.text for x in c if x.tag.endswith("loc") and x.text), None)
            if loc:
                st, b = http_get(loc, FEED_HEADERS, timeout=25)
                if st == 200:
                    out += parse_sitemap(b, depth + 1)
                time.sleep(DELAY)
        return out
    out = []
    for u in root.iter():
        if not u.tag.endswith("url"):
            continue
        loc = title = pub = None
        for ch in u.iter():
            tag = ch.tag.split("}")[-1].lower()
            if tag == "loc" and not loc:
                loc = (ch.text or "").strip()
            elif tag == "title" and not title:
                title = _text(ch)
            elif tag in ("publication_date", "lastmod") and not pub:
                pub = (ch.text or "").strip()
        if loc:
            out.append({"url": loc, "title": title, "published": pub, "description": None})
    return out


def run(ctx) -> dict:
    con = ctx.con
    sources = candidates.load_sources(con)
    fresh_after = datetime.now(timezone.utc) - timedelta(days=SITEMAP_FRESH_DAYS)
    stats = {"sources": 0, "feeds_failed": 0, "items": 0, "new": 0}

    for dom, s in sources.items():
        channels = []
        if s.get("rss_url"):
            channels.append(("rss", s["rss_url"]))
        if s.get("sitemap_url"):
            channels.append(("sitemap", s["sitemap_url"]))
        if not channels:
            continue
        stats["sources"] += 1
        for kind, url in channels:
            st, body = http_get(url, FEED_HEADERS, timeout=25)
            if st != 200 or not body:
                stats["feeds_failed"] += 1
                ctx.log.warning("%s %s: HTTP %s", dom, kind, st)
                continue
            items = parse_feed(body) if kind == "rss" else parse_sitemap(body)
            for it in items:
                if not looks_like_article(it["url"]):
                    continue
                if kind == "sitemap":
                    pub = candidates.parse_date(it["published"])
                    if pub and pub < fresh_after:
                        continue
                new = candidates.upsert(con, ctx.run_id, sources, url=it["url"], provider=kind,
                                        title=it["title"], published=it["published"],
                                        description=it["description"])
                stats["items"] += 1
                stats["new"] += bool(new)
            time.sleep(DELAY)
    return stats
