"""Збір пулу кандидатів зі стрічок джерел.

Навіщо. Модель релевантності вчиться відрізняти «варте поста» від «не варте».
Перше в нас є — 2 868 статей у дайджесті. Другого немає взагалі: те, що редакція
переглянула й не взяла, ніде не фіксувалося. Класифікатор на самих позитивах
вивчить рівно одне: «релевантно все».

Відновити заднім числом це неможливо. Тому збираємо з сьогодні: стрічка дає
повний перелік того, що вийшло, дайджест — те, що взяли, різниця і є негативами.

Таблиця append-only. Рядки не видаляються і не переписуються: `outcome`
уточнюється окремим звіренням, коли стаття з'являється в дайджесті.

Запуск:  python posts_db/collect_candidates.py
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request
from xml.etree import ElementTree as ET

import duckdb
import pandas as pd

ROOT = Path(__file__).parent.parent
DB = Path(__file__).parent / "posts.duckdb"
FEEDS = ROOT / "data" / "processed" / "feeds.csv"
SITEMAPS = ROOT / "data" / "processed" / "sitemaps.csv"
ARTICLES = ROOT / "data" / "processed" / "pg_article.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/rss+xml,application/xml,text/xml,*/*",
}
DELAY = 1.0
# Загальний /sitemap.xml містить увесь сайт, включно зі статичними сторінками
# й архівом за роки. Кандидат — це те, що вийшло нещодавно і що редактор міг
# узяти сьогодні, тож із sitemap беремо лише свіже.
SITEMAP_FRESH_DAYS = 7
TRACKING = re.compile(
    r"[?&](utm_[^=&]+|fbclid|gclid|srnd|mod|ref|ref_src|ref_url|s|t|"
    r"mibextid|smid|smtyp|partner|ito|CMP)=[^&]*", re.I)
TAGS = re.compile(r"<[^>]+>")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_pool (
    candidate_id   BIGINT,
    run_id         TEXT NOT NULL,
    url_canonical  TEXT NOT NULL,
    domain         TEXT,
    title          TEXT,
    published_at   TIMESTAMP,
    outcome        TEXT NOT NULL,   -- seen | ingested
    seen_at        TIMESTAMP NOT NULL
)
"""


def canonical(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    u = TRACKING.sub("", url.strip())
    u = re.sub(r"[?&]+$", "", u)
    u = re.sub(r"^https?://(?:www\.)?", "https://", u, flags=re.I)
    return u.rstrip("/")


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/?#]+)", url, re.I)
    return m.group(1).lower() if m else ""


def text_of(el: ET.Element | None) -> str | None:
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
        link, title, pub = None, None, None
        for ch in item:
            tag = ch.tag.split("}")[-1].lower()
            if tag == "link":
                # RSS кладе URL у текст, Atom — в атрибут href
                link = link or (ch.text or "").strip() or ch.attrib.get("href")
            elif tag == "title":
                title = title or text_of(ch)
            elif tag in ("pubdate", "published", "updated", "date"):
                pub = pub or (ch.text or "").strip()
        cu = canonical(link or "")
        if cu:
            out.append({"url_canonical": cu, "domain": domain_of(cu),
                        "title": title, "published_raw": pub})
    return out


def parse_sitemap(body: bytes, depth: int = 0) -> list[dict]:
    """Новинний sitemap — той самий дискавері, тільки для пошукових роботів.
    Видання публікує його навмисно, тож це такий самий санкціонований канал,
    як і RSS. Індекс sitemap'ів розгортаємо на один рівень."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    # Індекс: <sitemap><loc>…</loc></sitemap> — беремо кілька перших дочірніх
    children = [e for e in root.iter() if e.tag.endswith("sitemap")]
    if children and depth == 0:
        out = []
        for c in children[:2]:
            loc = next((x.text for x in c if x.tag.endswith("loc") and x.text), None)
            if not loc:
                continue
            try:
                with request.urlopen(request.Request(loc, headers=HEADERS),
                                     timeout=25) as r:
                    out += parse_sitemap(r.read(), depth + 1)
            except Exception:                                # noqa: BLE001
                pass
            time.sleep(DELAY)
        return out

    out = []
    for url_el in root.iter():
        if not url_el.tag.endswith("url"):
            continue
        loc, title, pub = None, None, None
        for ch in url_el.iter():
            tag = ch.tag.split("}")[-1].lower()
            if tag == "loc" and not loc:
                loc = (ch.text or "").strip()
            elif tag == "title" and not title:
                title = text_of(ch)
            elif tag in ("publication_date", "lastmod") and not pub:
                pub = (ch.text or "").strip()
        cu = canonical(loc or "")
        if cu:
            out.append({"url_canonical": cu, "domain": domain_of(cu),
                        "title": title, "published_raw": pub})
    return out


def main() -> None:
    if not FEEDS.exists():
        raise SystemExit("Немає feeds.csv — спершу discover_feeds.py")

    feeds = pd.read_csv(FEEDS)
    feeds = feeds[feeds["rss_url"].notna()][["domain", "rss_url"]]
    feeds = feeds.rename(columns={"rss_url": "url"})
    feeds["kind"] = "rss"
    if SITEMAPS.exists():
        sm = pd.read_csv(SITEMAPS)[["domain", "sitemap_url"]]
        sm = sm.rename(columns={"sitemap_url": "url"})
        sm["kind"] = "sitemap"
        # Sitemap лише для тих, у кого немає стрічки — не дублюємо джерело
        sm = sm[~sm["domain"].isin(set(feeds["domain"]))]
        feeds = pd.concat([feeds, sm], ignore_index=True)
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    rows, failed = [], []
    for i, f in enumerate(feeds.itertuples(), 1):
        try:
            with request.urlopen(request.Request(f.url, headers=HEADERS),
                                 timeout=30) as r:
                body = r.read()
            items = parse_feed(body) if f.kind == "rss" else parse_sitemap(body)
        except (error.HTTPError, error.URLError, Exception):     # noqa: BLE001
            items = []
            failed.append(f.domain)
        for it in items:
            it["run_id"] = run_id
            it["seen_at"] = now
            rows.append(it)
        print(f"  {i:>2}/{len(feeds)} {f.domain:<26} {f.kind:<8} {len(items):>4} записів")
        time.sleep(DELAY)

    if not rows:
        raise SystemExit("Зі стрічок нічого не зібралось")

    df = pd.DataFrame(rows).drop_duplicates("url_canonical")
    df["published_at"] = pd.to_datetime(df["published_raw"], errors="coerce",
                                        utc=True, format="mixed")

    # Відсіюємо стару й недатовану периферію з загальних sitemap
    before_filter = len(df)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=SITEMAP_FRESH_DAYS)
    from_sitemap = df["domain"].isin(set(feeds[feeds["kind"] == "sitemap"]["domain"]))
    stale = from_sitemap & (df["published_at"].isna() | (df["published_at"] < cutoff))
    # Статична сторінка теж має свіжий lastmod — CMS оновлює його при обході.
    # Відрізняємо за глибиною шляху: /emba це розділ, /world/стаття-22445 це стаття.
    depth = df["url_canonical"].str.count("/") - 2
    shallow = from_sitemap & (depth < 2)
    df = df[~(stale | shallow)]
    if before_filter != len(df):
        print(f"Відсіяно як застаріле або недатоване: {before_filter - len(df):,}")

    # Що з цього вже в дайджесті — це позитиви, решта поки кандидати
    arts = pd.read_csv(ARTICLES)
    known = set(arts["url_canonical"].map(
        lambda u: canonical(u) if isinstance(u, str) else None).dropna())
    df["outcome"] = df["url_canonical"].isin(known).map(
        {True: "ingested", False: "seen"})

    con = duckdb.connect(str(DB))
    con.execute(SCHEMA)
    before = con.execute("SELECT count(*) FROM candidate_pool").fetchone()[0]

    # Не дублюємо те, що вже бачили: URL лишається в пулі один раз
    seen = con.execute("SELECT url_canonical FROM candidate_pool").df()
    new = df[~df["url_canonical"].isin(set(seen["url_canonical"]))].copy()
    new["candidate_id"] = range(before + 1, before + 1 + len(new))

    cols = ["candidate_id", "run_id", "url_canonical", "domain", "title",
            "published_at", "outcome", "seen_at"]
    con.register("new_df", new[cols])
    con.execute(f"INSERT INTO candidate_pool SELECT {', '.join(cols)} FROM new_df")

    total = con.execute("SELECT count(*) FROM candidate_pool").fetchone()[0]
    print(f"\n{'─' * 66}")
    print(f"Зі стрічок: {len(df):,} унікальних URL")
    print(f"Нових у пулі: {len(new):,}   ·   усього в пулі: {total:,}")
    if failed:
        print(f"Не відповіли: {len(failed)} — {', '.join(failed[:8])}")

    print("\nСпіввідношення позитивів і кандидатів у цьому зборі:")
    print(df["outcome"].value_counts().to_string())

    print("\nТоп джерел за обсягом стрічки:")
    print(df["domain"].value_counts().head(10).to_string())
    con.close()


if __name__ == "__main__":
    main()
