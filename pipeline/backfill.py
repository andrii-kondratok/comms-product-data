"""Разове завантаження того, що вже зібрано локально. Ідемпотентне: можна повторювати.

  1. реєстр джерел        posts_db/sources.csv + sitemaps + домени з пар
  2. статті з текстом      Notion Articles, власний витяг, NewsCatcher (перевірені домени)
  3. пости                 знімок Post Metrics + треди з архіву твітів
  4. пари стаття → пост    data/processed/training_pairs*.csv
  5. пул кандидатів        posts_db/posts.duckdb (збір з 14.09)
  6. водяні знаки          щоб sync_notion почав з моменту знімків, а не з нуля

Запуск: python -m pipeline backfill [--only sources,articles,...]
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from pathlib import Path

from . import articles, candidates, config, db, newscatcher
from .jobs import sync_notion
from .util import canonical, domain_of, match_source

log = logging.getLogger("pipeline.backfill")
csv.field_size_limit(10 ** 8)

ROOT = config.ROOT
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw"
ANTIBOT_VIA_API = ("reuters.com", "cnn.com", "apnews.com", "axios.com", "politico.com",
                   "moscowtimes.ru", "themoscowtimes.com", "news.sky.com")


def _cascade(domain: str, access: str, verdict: str) -> tuple[list, bool]:
    """(каскад, api_preview_only) з виміряного вердикту."""
    if newscatcher.is_preview_domain(domain):
        return [], True                      # пейвол: ні ми, ні API повного тексту не дають
    if any(domain == d or domain.endswith("." + d) for d in ANTIBOT_VIA_API):
        return ["newscatcher_v3", "own_extractor"], False
    if access == "antibot" or verdict == "blocked_or_empty":
        return ["newscatcher_v3", "own_extractor"], False
    return ["own_extractor"], False


def load_sources(con) -> int:
    sitemaps = {}
    f = PROC / "sitemaps.csv"
    if f.exists():
        for r in csv.DictReader(f.open(encoding="utf-8")):
            if r.get("sitemap_url"):
                sitemaps[r["domain"]] = r["sitemap_url"]
    n = 0
    for r in csv.DictReader((ROOT / "posts_db" / "sources.csv").open(encoding="utf-8")):
        dom = r["domain"].lower()
        casc, preview = _cascade(dom, r["access"], r["extract_verdict"])
        disc = (["rss"] if r["rss_url"] else []) + (["sitemap"] if dom in sitemaps else [])
        if r["declared"] == "yes":
            disc.append("newscatcher")
        con.execute("""
            INSERT INTO core.source (domain, name, category, lang, country, access_class,
                license_class, rss_url, sitemap_url, declared_in_digest, notes, discovery,
                fetch_cascade, extract_verdict, extract_ratio, access_src, api_preview_only)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (domain) DO UPDATE SET
                name=EXCLUDED.name, category=EXCLUDED.category, access_class=EXCLUDED.access_class,
                rss_url=EXCLUDED.rss_url, sitemap_url=EXCLUDED.sitemap_url,
                declared_in_digest=EXCLUDED.declared_in_digest, discovery=EXCLUDED.discovery,
                fetch_cascade=EXCLUDED.fetch_cascade, extract_verdict=EXCLUDED.extract_verdict,
                extract_ratio=EXCLUDED.extract_ratio, access_src=EXCLUDED.access_src,
                api_preview_only=EXCLUDED.api_preview_only, updated_at=now()""",
            (dom, r["name"], r["category"] if r["category"] in
             ("global", "ua_media", "ru_media", "analysis", "kse") else "other",
             r["lang"] or None, r["country"] or None,
             r["access"] if r["access"] in ("open", "antibot", "hard_paywall") else "unknown",
             "rss_public" if r["rss_url"] else "unknown", r["rss_url"] or None,
             sitemaps.get(dom), r["declared"] == "yes", r["notes"] or None, disc, casc,
             r["extract_verdict"] or None,
             float(r["extract_ratio"]) if r["extract_ratio"] not in ("", None) else None,
             r["access_src"] or None, preview))
        n += 1

    # Домени, на які посилаються пости, але яких немає в реєстрі дайджесту
    extra = Counter()
    for name in ("training_pairs.csv", "training_pairs_todo.csv"):
        f = PROC / name
        if f.exists():
            for r in csv.DictReader(f.open(encoding="utf-8")):
                extra[r["article_domain"]] += 1
    known = {r["domain"] for r in con.execute("SELECT domain FROM core.source")}
    for dom, cnt in extra.items():
        if cnt < 3 or dom in known or match_source(dom, {k: True for k in known}):
            continue
        casc, preview = _cascade(dom, "unknown", "")
        con.execute("""INSERT INTO core.source (domain, name, category, discovery,
                           fetch_cascade, api_preview_only, notes)
                       VALUES (%s,%s,'other','{}',%s,%s,'додано з посилань у постах')
                       ON CONFLICT (domain) DO NOTHING""", (dom, dom, casc, preview))
        n += 1
    return n


def _set_text(con, aid, text: str, method: str) -> bool:
    cur = con.execute("SELECT retrieval_status FROM core.article WHERE article_id=%s",
                      (aid,)).fetchone()
    if cur["retrieval_status"] == "full_text":
        return False
    h = articles.content_hash(text)
    dup = con.execute("SELECT 1 FROM core.article WHERE content_hash=%s AND article_id<>%s",
                      (h, aid)).fetchone()
    con.execute("""UPDATE core.article SET body_text=%s, content_hash=%s,
                   retrieval_status='full_text', retrieval_method=%s, text_verified=true,
                   updated_at=now() WHERE article_id=%s""",
                (text, None if dup else h, method, aid))
    return True


def load_articles(con) -> dict:
    sources = candidates.load_sources(con)
    stats = Counter()
    f = PROC / "pg_article.csv"
    for r in csv.DictReader(f.open(encoding="utf-8")):
        if not r["url_canonical"]:
            continue
        aid = articles.ensure(con, sources, url=r["url_canonical"], title=r["title"] or None,
                              published_at=candidates.parse_date(r["created_at"]))
        con.execute("""UPDATE core.article SET notion_page_id=coalesce(notion_page_id,%s),
                       digest_status=%s, subtitle=coalesce(subtitle,%s)
                       WHERE article_id=%s
                         AND NOT EXISTS (SELECT 1 FROM core.article x
                                         WHERE x.notion_page_id=%s AND x.article_id<>%s)""",
                    (r["notion_page_id"], r["status"], r["subtitle"] or None, aid,
                     r["notion_page_id"], aid))
        stats["notion_rows"] += 1
        if len(r["body_text"] or "") >= 500:
            stats["notion_text"] += _set_text(con, aid, r["body_text"], "notion_editor")

    f = RAW / "fetched_articles.jsonl"
    if f.exists():
        for line in f.open(encoding="utf-8"):
            x = json.loads(line)
            if x.get("verdict") == "full" and x.get("text"):
                aid = articles.ensure(con, sources, url=x["url"])
                stats["extractor_text"] += _set_text(con, aid, x["text"], "own_extractor")

    f = RAW / "newscatcher_v3.jsonl"
    if f.exists():
        for line in f.open(encoding="utf-8"):
            x = json.loads(line)
            dom = domain_of(canonical(x["requested"]) or "")
            text = newscatcher.usable_text({"content": x.get("content"),
                                            "paid_content": x.get("paid_content")}, dom) \
                if x.get("found") else None
            if text:
                aid = articles.ensure(con, sources, url=x["requested"])
                stats["newscatcher_text"] += _set_text(con, aid, text, "newscatcher_v3")
    return dict(stats)


def load_posts(con) -> dict:
    stats = Counter()
    snap = RAW / "notion" / "_pm_live.json"
    last = None
    for row in json.load(snap.open(encoding="utf-8")):
        sync_notion._raw(con, "post_metrics", row)
        sync_notion.sync_post(con, row)
        last = max(last or "", row["last_edited_time"])
        stats["post_metrics"] += 1
    if last:
        db.set_watermark(con, "sync_notion", "post_metrics.last_edited_time", last)

    # Тексти з архіву твітів: пости, яких немає в Post Metrics, і PM без тексту
    f = PROC / "training_pairs.csv"
    seen = set()
    for r in csv.DictReader(f.open(encoding="utf-8")):
        if r["post_text_src"] != "x_archive" or r["post_key"] in seen:
            continue
        seen.add(r["post_key"])
        native = r["post_url"].rsplit("/", 1)[-1]
        if r["pm_page_id"]:
            con.execute("""UPDATE core.post SET body_raw=%s, text_source='x_archive',
                           exclusion_reason=CASE WHEN %s LIKE '%%post_fragment%%'
                                                 THEN 'post_fragment' ELSE exclusion_reason END
                           WHERE notion_page_id=%s AND body_raw IS NULL""",
                        (r["post_text"], r["quality_flag"], r["pm_page_id"]))
        else:
            con.execute("""INSERT INTO core.post (platform, native_id, url_canonical, posted_at,
                               body_raw, source_system, text_source, exclusion_reason)
                           VALUES ('X',%s,%s,%s,%s,'x_archive','x_archive',%s)
                           ON CONFLICT (platform, native_id) DO NOTHING""",
                        (native, r["post_url"], r["post_date"] or None, r["post_text"],
                         "post_fragment" if "post_fragment" in r["quality_flag"] else None))
        stats["x_archive"] += 1
    return dict(stats)


def load_links(con) -> dict:
    sources = candidates.load_sources(con)
    stats = Counter()
    for name in ("training_pairs.csv", "training_pairs_todo.csv"):
        f = PROC / name
        if not f.exists():
            continue
        for r in csv.DictReader(f.open(encoding="utf-8")):
            if r["pm_page_id"]:
                p = con.execute("SELECT post_id FROM core.post WHERE notion_page_id=%s",
                                (r["pm_page_id"],)).fetchone()
            else:
                p = con.execute("SELECT post_id FROM core.post WHERE platform='X' AND native_id=%s",
                                (r["post_url"].rsplit("/", 1)[-1],)).fetchone()
            if not p:
                stats["post_missing"] += 1
                continue
            aid = articles.ensure(con, sources, url=r["article_url"])
            con.execute("""INSERT INTO core.article_post_link
                           (article_id, post_id, link_method, link_evidence, confidence, quality_flag)
                           VALUES (%s,%s,'explicit_url',%s,1.0,%s)
                           ON CONFLICT (article_id, post_id) DO NOTHING""",
                        (aid, p["post_id"], r["link_method"] if r["link_method"] in
                         ("source_field", "url_in_post") else "source_field",
                         r.get("quality_flag") or None))
            stats[name] += 1
    return dict(stats)


def load_candidates(con) -> dict:
    """Пул із локального збору (DuckDB), вивантажений у CSV: на сервері DuckDB немає."""
    f = PROC / "candidate_pool_export.csv"
    if not f.exists():
        return {"skipped": "немає candidate_pool_export.csv"}
    sources = candidates.load_sources(con)
    n = 0
    for r in csv.DictReader(f.open(encoding="utf-8")):
        cu = canonical(r["url_canonical"])
        if not cu:
            continue
        s = match_source(domain_of(cu), sources)
        seen = r["seen_at"] or None
        con.execute("""INSERT INTO ops.candidate_pool (url_canonical, url_original, source_id,
                           title, published_at, provider, providers, outcome,
                           first_seen_at, last_seen_at, seen_at)
                       VALUES (%s,%s,%s,%s,%s,'rss','{rss}',%s,%s,%s,%s)
                       ON CONFLICT (url_canonical) DO NOTHING""",
                    (cu, r["url_canonical"], s["source_id"] if s else None, r["title"] or None,
                     r["published_at"] or None,
                     r["outcome"] if r["outcome"] in ("seen", "ingested") else "seen",
                     seen, seen, seen))
        n += 1
    return {"candidates": n}


STEPS = {"sources": load_sources, "articles": load_articles, "posts": load_posts,
         "links": load_links, "candidates": load_candidates}


def main(only: list | None = None) -> None:
    for name, fn in STEPS.items():
        if only and name not in only:
            continue
        with db.connect() as con:
            res = fn(con)
            if name == "posts":
                # знімки Notion від 12.09 — звідти sync_notion і продовжить
                for k in ("articles", "content_pulse"):
                    if not db.get_watermark(con, "sync_notion", f"{k}.last_edited_time"):
                        db.set_watermark(con, "sync_notion", f"{k}.last_edited_time",
                                         "2026-09-12T00:00:00.000Z")
            con.commit()
        log.info("backfill %s: %s", name, res)
        print(f"  {name:11} {res}", flush=True)
