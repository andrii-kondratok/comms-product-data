"""Регулярка по всіх текстах постів, які є в нас: де ТМ посилався на статті.

Articles — молода база, тож «2 тис. пар» міряє вік бази, а не звичку офісу
давати джерело. Тут шукаємо посилання прямо в текстах постів за всі роки:
архіви твітів, FB-експорти, тіла Content Pulse, Post Metrics.

У твітах майже всі лінки — t.co. Розгортаємо їх одним HEAD-запитом до t.co
(звичайний редирект, без обходу чогось). Медіа-вкладення розгортаються в
x.com/…/photo|video — такі відкидаємо як «не статтю».

Вихід:
    data/processed/link_scan.csv        — один рядок на (пост, посилання)
    data/processed/tco_resolved.json    — кеш розгорнутих t.co, резюмиться

Запуск:  python posts_db/scan_links.py [--no-resolve]
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw" / "notion"
PA = Path(r"D:\Posts analysis")
DL = Path(r"C:\Users\Admin\Downloads")
OUT = PROC / "link_scan.csv"
TCO_CACHE = PROC / "tco_resolved.json"

csv.field_size_limit(10 ** 8)

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\u2026]+", re.I)
# Посилання без схеми: «ft.com/content/…», «www.reuters.com/world/…»
BARE_RE = re.compile(
    r"(?<![\w@/.])(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
    r"(?:com|co\.uk|org|net|ua|eu|media|press|info|de|fr|pl|io|news)/[^\s<>\"'\)\]]+", re.I)

SOCIAL = ("x.com", "twitter.com", "facebook.com", "fb.com", "fb.me", "fb.watch",
          "instagram.com", "threads.net", "threads.com", "t.me", "linkedin.com",
          "tiktok.com")
VIDEO = ("youtube.com", "youtu.be", "vimeo.com")
SHORT = ("t.co", "bit.ly", "ow.ly", "buff.ly", "tinyurl.com", "goo.gl", "dlvr.it",
         "trib.al", "is.gd", "lnkd.in", "shorturl.at", "cutt.ly", "rebrand.ly", "on.ft.com",
         "reut.rs", "nyti.ms", "wapo.st", "bloom.bg", "econ.st", "politi.co", "cnn.it",
         "bbc.in", "gu.com", "wp.me", "apple.news")
# скорочувачі видань — вже самі кажуть, яке видання
SHORT_OUTLET = {"on.ft.com": "ft.com", "reut.rs": "reuters.com", "nyti.ms": "nytimes.com",
                "wapo.st": "washingtonpost.com", "bloom.bg": "bloomberg.com",
                "econ.st": "economist.com", "politi.co": "politico.com", "cnn.it": "cnn.com",
                "bbc.in": "bbc.com", "gu.com": "theguardian.com", "trib.al": ""}
OWN = ("kse.ua", "kse.org.ua", "notion.so", "notion.site", "notion.com",
       "docs.google.com", "drive.google.com", "forms.gle", "prod-files-secure")


def host_of(url: str) -> str:
    h = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split("?")[0].lower()
    return h[4:] if h.startswith("www.") else h


def kind_of(host: str) -> str:
    if any(host == s or host.endswith("." + s) for s in SHORT):
        return "short"
    if any(host == s or host.endswith("." + s) for s in SOCIAL):
        return "social"
    if any(host == s or host.endswith("." + s) for s in VIDEO):
        return "video"
    if any(o in host for o in OWN):
        return "own"
    return "news"


def find_urls(text: str) -> list:
    text = text or ""
    urls = [u.rstrip(".,;:!?»\"'") for u in URL_RE.findall(text)]
    stripped = URL_RE.sub(" ", text)
    urls += ["https://" + u.rstrip(".,;:!?»\"'") for u in BARE_RE.findall(stripped)]
    return urls


def year_of(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"(20\d\d)", s)
    return m.group(1) if m else ""


# --- джерела текстів ------------------------------------------------------

def iter_texts():
    """(джерело, ключ поста, платформа, рік, текст)."""
    # 1. архів твітів — усі твіти, не лише стартові
    f = DL / "raw_tweets_and_threads.csv"
    if f.exists():
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            yield ("x_raw", r.get("thread_id"), "X", year_of(r.get("created_at")),
                   r.get("starter_text") or "")
    f = DL / "scrapper_clean_threads.csv"
    if f.exists():
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            yield ("x_scrapper", r.get("id") or r.get("tweet_id_str"), "X",
                   year_of(r.get("date") or r.get("created_at")), r.get("text") or "")
    f = PA / "twitter_mylovanov_posts.csv"
    if f.exists():
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            yield ("x_mylovanov", r.get("tweet_id"), "X", year_of(r.get("date")),
                   r.get("text") or "")

    # 2. Facebook
    f = PA / "all_posts.csv"
    if f.exists():
        for r in csv.DictReader(f.open(encoding="utf-8-sig")):
            yield ("fb_all_posts", r.get("url"), "FB", year_of(r.get("date")),
                   r.get("text") or "")
    for name in ("2022 - 2023.csv", "2024 - 2025.csv", "2025 - 2026.csv"):
        f = PA / name
        if f.exists():
            for r in csv.DictReader(f.open(encoding="utf-8-sig")):
                yield ("fb_export", r.get("Постійне посилання") or r.get("ID допису"), "FB",
                       year_of(r.get("Час публікації")),
                       (r.get("Назва") or "") + " " + (r.get("Коментар до даних") or ""))

    # 3. Content Pulse: справжні тіла сторінок + властивості зі знімка
    f = RAW / "content_pulse_bodies.jsonl"
    if f.exists():
        for line in f.open(encoding="utf-8"):
            r = json.loads(line)
            yield ("cp_body", r["page_id"], "", "", r.get("markdown") or "")
    snaps = sorted(RAW.glob("content_pulse__*.jsonl"))
    if snaps:
        for line in snaps[-1].open(encoding="utf-8"):
            r = json.loads(line)
            p = r["properties"]
            parts = []
            for fld in ("Source / Evidence", "Text", "CTA"):
                v = p.get(fld) or {}
                parts.append("".join(t.get("plain_text", "") for t in v.get("rich_text") or []))
            d = ((p.get("Date") or {}).get("date") or {}).get("start") or r.get("created_time")
            yield ("cp_props", r["notion_page_id"], "", year_of(d), " ".join(parts))

    # 4. Post Metrics (свіжий знімок)
    f = RAW / "_pm_live.json"
    if f.exists():
        for r in json.load(f.open(encoding="utf-8")):
            p = r["properties"]
            def rt(k):
                v = p.get(k) or {}
                return "".join(t.get("plain_text", "") for t in
                               (v.get("rich_text") or v.get("title") or []))
            d = ((p.get("Post date") or {}).get("date") or {}).get("start") or ""
            plat = ((p.get("Platform") or {}).get("select") or {}).get("name") or ""
            yield ("post_metrics", r["id"], plat, year_of(d),
                   " ".join([rt("Name"), rt("Post text"), rt("Source / Evidence (regex)")]))


# --- розгортання t.co -----------------------------------------------------

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = request.build_opener(NoRedirect)


def resolve_one(url: str) -> str:
    for attempt in range(3):
        try:
            req = request.Request(url, method="HEAD",
                                  headers={"User-Agent": "Mozilla/5.0"})
            OPENER.open(req, timeout=15)
            return ""
        except error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e.headers.get("Location") or ""
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return ""
        except Exception:                                    # noqa: BLE001
            time.sleep(1 + attempt)
    return ""


def resolve_all(short_urls: set) -> dict:
    cache = json.load(TCO_CACHE.open(encoding="utf-8")) if TCO_CACHE.exists() else {}
    todo = [u for u in short_urls if u not in cache]
    print(f"Скорочених посилань: {len(short_urls):,} · у кеші {len(cache):,} · "
          f"розгортаю {len(todo):,}", flush=True)
    started = time.time()
    with ThreadPoolExecutor(max_workers=12) as ex:
        for i, (u, loc) in enumerate(zip(todo, ex.map(resolve_one, todo)), 1):
            # t.co → bit.ly → стаття: другий крок, якщо перший теж скорочувач
            if loc and kind_of(host_of(loc)) == "short" and host_of(loc) != host_of(u):
                loc = resolve_one(loc) or loc
            cache[u] = loc
            if i % 1000 == 0 or i == len(todo):
                json.dump(cache, TCO_CACHE.open("w", encoding="utf-8"))
                el = time.time() - started
                print(f"  {i:,}/{len(todo):,} · {el / 60:.1f} хв · "
                      f"~{el / i * (len(todo) - i) / 60:.0f} хв лишилось", flush=True)
    json.dump(cache, TCO_CACHE.open("w", encoding="utf-8"))
    return cache


def main() -> None:
    resolve = "--no-resolve" not in sys.argv

    records = []
    seen_posts = Counter()
    for src, key, plat, yr, text in iter_texts():
        seen_posts[src] += 1
        for u in find_urls(text):
            records.append([src, key, plat, yr, u])
    print("Текстів переглянуто:", dict(seen_posts))
    print(f"Посилань знайдено: {len(records):,}")

    if resolve:
        shorts = {r[4] for r in records if kind_of(host_of(r[4])) == "short"
                  and host_of(r[4]) not in SHORT_OUTLET}
        cache = resolve_all(shorts)
    else:
        cache = {}

    rows = []
    for src, key, plat, yr, u in records:
        h = host_of(u)
        final = u
        if kind_of(h) == "short":
            if h in SHORT_OUTLET and SHORT_OUTLET[h]:
                final, h = u, SHORT_OUTLET[h]
            else:
                final = cache.get(u) or u
                h = host_of(final)
        k = kind_of(h)
        # t.co на медіа-вкладення твіта: x.com/…/photo/1, /video/1
        if k == "social" and re.search(r"/(photo|video)/\d", final):
            k = "tweet_media"
        rows.append({"source": src, "post_key": key, "platform": plat, "year": yr,
                     "url": u, "final_url": final, "host": h, "kind": k})

    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # --- звіт: скільки ПОСТІВ (не посилань) мають лінк на медіа
    posts_by = defaultdict(set)
    news_by = defaultdict(set)
    for src, key, plat, yr, text in []:
        pass
    total = defaultdict(set)
    for src, key, plat, yr, text in iter_texts():
        total[(src, yr)].add(key)
    for r in rows:
        if r["kind"] == "news":
            news_by[(r["source"], r["year"])].add(r["post_key"])
    print(f"\n{'джерело':14}{'рік':>6}{'постів':>9}{'з лінком на медіа':>20}{'%':>6}")
    for (src, yr) in sorted(total):
        t = len(total[(src, yr)])
        n = len(news_by[(src, yr)])
        if t:
            print(f"{src:14}{yr or '—':>6}{t:>9,}{n:>20,}{100 * n / t:>5.0f}%")

    print("\nЯкі посилання взагалі (після розгортання):")
    for k, v in Counter(r["kind"] for r in rows).most_common():
        print(f"  {k:12} {v:,}")
    print("\nТоп медіа:")
    for h, n in Counter(r["host"] for r in rows if r["kind"] == "news").most_common(25):
        print(f"  {h:30} {n:,}")
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
