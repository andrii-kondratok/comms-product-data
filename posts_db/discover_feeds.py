"""Пошук RSS-стрічок для всіх джерел реєстру.

Стрічка — санкціонований видавцем канал: він сам її публікує для автоматичного
зчитування. Тексту там здебільшого немає, але для дискаверу цього й не треба:
нам потрібно знати, ЩО вийшло, щоб зафіксувати повний пул кандидатів.

Шукаємо у два заходи:
  1. автооголошення в <head>: <link rel="alternate" type="application/rss+xml">
  2. типові шляхи, якщо перше не спрацювало

Результат пишеться в posts_db/sources.csv (колонка rss_url) і в
data/processed/feeds.csv із діагностикою.

Запуск:  python posts_db/discover_feeds.py
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib import error, parse, request
from xml.etree import ElementTree as ET

import pandas as pd

REG = Path(__file__).parent / "sources.csv"
OUT = Path(__file__).parent.parent / "data" / "processed"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
COMMON = ["/rss", "/feed", "/rss.xml", "/feed.xml", "/index.xml", "/atom.xml",
          "/rss/", "/feeds/all.xml"]
DELAY = 1.2

LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)


def get(url: str, timeout: int = 20) -> tuple[object, bytes]:
    try:
        with request.urlopen(request.Request(url, headers=HEADERS), timeout=timeout) as r:
            return r.status, r.read()
    except error.HTTPError as e:
        return e.code, b""
    except Exception as e:                                   # noqa: BLE001
        return type(e).__name__, b""


def feed_items(body: bytes) -> int:
    """Скільки записів у стрічці. Нуль означає, що це не робоча стрічка."""
    if not body:
        return 0
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return 0
    return sum(1 for e in root.iter()
               if e.tag.endswith("item") or e.tag.endswith("entry"))


def candidates_from_head(domain: str) -> list[str]:
    status, body = get(f"https://{domain}/")
    if not body:
        return []
    html = body.decode("utf-8", errors="replace")
    out = []
    for tag in LINK_RE.findall(html):
        m = HREF_RE.search(tag)
        if m:
            out.append(parse.urljoin(f"https://{domain}/", m.group(1)))
    # Найкоротші URL зазвичай і є головною стрічкою, а не рубрикою
    return sorted(dict.fromkeys(out), key=len)[:3]


def main() -> None:
    reg = pd.read_csv(REG)
    rows = []

    for i, d in enumerate(reg["domain"], 1):
        found, n_items, how = None, 0, None

        for url in candidates_from_head(d):
            st, body = get(url)
            n = feed_items(body)
            time.sleep(DELAY)
            if n:
                found, n_items, how = url, n, "head"
                break

        if not found:
            for path in COMMON:
                url = f"https://{d}{path}"
                st, body = get(url)
                n = feed_items(body)
                time.sleep(DELAY)
                if n:
                    found, n_items, how = url, n, "common_path"
                    break

        rows.append({"domain": d, "rss_url": found, "items": n_items, "how": how})
        mark = f"{n_items:>4} записів  {how}" if found else "не знайдено"
        print(f"  {i:>2}/{len(reg)} {d:<26} {mark}")

    feeds = pd.DataFrame(rows)
    feeds.to_csv(OUT / "feeds.csv", index=False)

    reg = reg.drop(columns=["rss_url"], errors="ignore").merge(
        feeds[["domain", "rss_url"]], on="domain", how="left")
    reg.to_csv(REG, index=False)

    ok = feeds["rss_url"].notna()
    print(f"\n{'─' * 66}")
    print(f"Стрічку знайдено: {ok.sum()} із {len(feeds)} доменів")
    print(f"Сумарно записів у стрічках: {feeds['items'].sum():,}")
    print("\nБез стрічки:")
    miss = feeds[~ok]["domain"].tolist()
    print("  " + (", ".join(miss) if miss else "—"))
    print(f"\nЗаписано → {OUT / 'feeds.csv'} і {REG}")


if __name__ == "__main__":
    main()
