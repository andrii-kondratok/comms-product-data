"""Чи віддають заблоковані видання текст через власний RSS.

RSS — канал, який видання публікує саме для автоматичного зчитування, тож
тут немає ані обходу захисту, ані сірої зони. Питання лише в тому, що вони
в нього кладуть: повний текст у <content:encoded> чи самий анонс.

Перевіряємо домени, які прямий запит блокує (401/403/таймаут).

Запуск:  python posts_db/test_rss.py
"""

from __future__ import annotations

import re
import time
from urllib import error, request
from xml.etree import ElementTree as ET

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/rss+xml,application/xml,text/xml,*/*",
}

# Відомі стрічки заблокованих доменів. Де офіційної не знайшов — типові шляхи.
FEEDS = {
    "reuters.com": ["https://www.reuters.com/arc/outboundfeeds/rss/?outputType=xml"],
    "nytimes.com": ["https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
                    "https://rss.nytimes.com/services/xml/rss/nyt/Europe.xml"],
    "ft.com": ["https://www.ft.com/world?format=rss", "https://www.ft.com/rss/home"],
    "telegraph.co.uk": ["https://www.telegraph.co.uk/news/rss.xml"],
    "economist.com": ["https://www.economist.com/europe/rss.xml",
                      "https://www.economist.com/latest/rss.xml"],
    "bloomberg.com": ["https://feeds.bloomberg.com/politics/news.rss"],
    "wsj.com": ["https://feeds.a.dj.com/rss/RSSWorldNews.xml"],
    "washingtonpost.com": ["https://feeds.washingtonpost.com/rss/world"],
    "edition.cnn.com": ["http://rss.cnn.com/rss/edition_world.rss"],
    "thetimes.com": ["https://www.thetimes.com/world/rss"],
}

CONTENT_TAGS = ("{http://purl.org/rss/1.0/modules/content/}encoded",
                "content", "description", "summary",
                "{http://www.w3.org/2005/Atom}content",
                "{http://www.w3.org/2005/Atom}summary")
TAGS = re.compile(r"<[^>]+>")


def fetch(url: str) -> tuple[object, bytes]:
    try:
        with request.urlopen(request.Request(url, headers=HEADERS), timeout=25) as r:
            return r.status, r.read()
    except error.HTTPError as e:
        return e.code, b""
    except Exception as e:                                   # noqa: BLE001
        return type(e).__name__, b""


def longest_text(item: ET.Element) -> int:
    best = 0
    for el in item.iter():
        if el.tag in CONTENT_TAGS and el.text:
            best = max(best, len(TAGS.sub("", el.text).strip()))
    return best


def main() -> None:
    print(f"{'домен':<22}{'статус':<10}{'записів':>8}{'макс.текст':>12}  вердикт")
    for dom, urls in FEEDS.items():
        best_n, best_len, status = 0, 0, None
        for u in urls:
            st, body = fetch(u)
            status = status or st
            if not body:
                time.sleep(1)
                continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                time.sleep(1)
                continue
            items = [e for e in root.iter()
                     if e.tag.endswith("item") or e.tag.endswith("entry")]
            if items:
                status = st
                best_n = max(best_n, len(items))
                best_len = max(best_len, max((longest_text(i) for i in items), default=0))
            time.sleep(1)
            if best_len > 1500:
                break

        verdict = ("ПОВНИЙ ТЕКСТ" if best_len > 1500
                   else "анонс" if best_len > 200
                   else "заголовки" if best_n else "стрічки немає")
        print(f"{dom:<22}{str(status):<10}{best_n:>8}{best_len:>12,}  {verdict}")


if __name__ == "__main__":
    main()
