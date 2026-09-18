"""Notion → база, інкрементально за last_edited_time.

  Post Metrics  → core.post + core.post_metric (метрики — часовий ряд, не перезапис)
  🧾 Articles   → core.article: сторінка редакції + її текст із тоглів
  Content Pulse → raw.notion_export (драфти; розбір — окремим кроком)

Кожна сторінка спершу йде в raw.notion_export з хешем стану: той самий стан
двічі не пишеться, а змінений — лишає слід. Водяний знак — last_edited_time
останньої обробленої сторінки; порядок зростання дає продовжити з місця зупинки.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .. import articles, candidates, config, db, notion
from ..util import canonical

# Скільки сторінок за запуск. Тіла статей — 3 запити на сторінку, тому менше.
LIMITS = {"post_metrics": 3000, "content_pulse": 2000, "articles": 150}
OVERLAP = timedelta(minutes=2)   # Notion округлює last_edited_time до хвилини

STATUS_TO_RETRIEVAL = {
    "Full Text": "full_text", "Key Points Hydrated": "full_text",
    "Digest Hydrated": "full_text", "Draft": "full_text", "Post draft created": "full_text",
}
METRICS = {"Views": "views", "Likes": "likes", "Comments": "comments", "Shares": "shares"}


def _raw(con, datasource: str, row: dict, body: str | None = None) -> bool:
    payload = json.dumps(row["properties"], sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256((payload + (body or "")).encode()).hexdigest()
    r = con.execute("""INSERT INTO raw.notion_export
                       (datasource, notion_page_id, properties, page_body,
                        last_edited_time, payload_hash)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (datasource, notion_page_id, payload_hash) DO NOTHING
                       RETURNING export_id""",
                    (datasource, row["id"], payload, body, row["last_edited_time"], h)).fetchone()
    return r is not None


def _native_id(url: str) -> str | None:
    m = re.search(r"/status/(\d+)", url or "") or \
        re.search(r"(pfbid[0-9A-Za-z]+|story_fbid=(\d+)|/posts/(\d+)|/videos/(\d+))", url or "")
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def sync_post(con, row: dict) -> None:
    p = row["properties"]
    url = notion.scalar(p.get("Post URL")) or ""
    platform = notion.scalar(p.get("Platform")) or "X"
    native = _native_id(url)
    if native:
        clash = con.execute("""SELECT 1 FROM core.post WHERE platform=%s AND native_id=%s
                               AND notion_page_id IS DISTINCT FROM %s""",
                            (platform, native, row["id"])).fetchone()
        if clash:            # два рядки Post Metrics на той самий пост — ключ лишаємо першому
            native = None
    text = notion.plain(p.get("Post text"))
    cp = (notion.scalar(p.get("Content Pulse item")) or [None])[0]
    r = con.execute("""
        INSERT INTO core.post (platform, native_id, url_canonical, posted_at, hook_raw, body_raw,
                               content_category, content_category_src, source_system,
                               notion_page_id, text_source, source_url, content_pulse_page_id,
                               notion_last_edited_at)
        VALUES (%(platform)s, %(native)s, %(url)s, %(posted)s, %(hook)s, %(body)s,
                %(cat)s, 'notion_post_metrics', 'notion_post_metrics',
                %(page)s, %(tsrc)s, %(src)s, %(cp)s, %(edited)s)
        ON CONFLICT (notion_page_id) WHERE notion_page_id IS NOT NULL DO UPDATE SET
            native_id   = coalesce(core.post.native_id, EXCLUDED.native_id),
            url_canonical = EXCLUDED.url_canonical,
            posted_at   = EXCLUDED.posted_at,
            hook_raw    = EXCLUDED.hook_raw,
            body_raw    = coalesce(EXCLUDED.body_raw, core.post.body_raw),
            content_category = EXCLUDED.content_category,
            text_source = coalesce(EXCLUDED.text_source, core.post.text_source),
            source_url  = EXCLUDED.source_url,
            content_pulse_page_id = EXCLUDED.content_pulse_page_id,
            notion_last_edited_at = EXCLUDED.notion_last_edited_at,
            updated_at  = now()
        RETURNING post_id""",
        {"platform": platform if platform in ("X", "FB") else "X", "native": native,
         "url": url or None, "posted": notion.scalar(p.get("Post date")),
         "hook": notion.plain(p.get("Name")), "body": text,
         "cat": notion.scalar(p.get("Category")), "page": row["id"],
         "tsrc": "notion_post_metrics" if text else None,
         "src": notion.plain(p.get("Source / Evidence (regex)")), "cp": cp,
         "edited": row["last_edited_time"]}).fetchone()

    observed = notion.scalar(p.get("Metrics updated")) or row["last_edited_time"]
    for prop, metric in METRICS.items():
        v = notion.scalar(p.get(prop))
        if isinstance(v, (int, float)):
            con.execute("""INSERT INTO core.post_metric
                           (post_id, metric, value, observed_at, source_system)
                           VALUES (%s,%s,%s,%s,'notion_post_metrics')
                           ON CONFLICT DO NOTHING""", (r["post_id"], metric, v, observed))


def sync_article(con, sources: dict, row: dict, run_id) -> bool:
    """True — підтягнули текст редакції."""
    p = row["properties"]
    url = notion.plain(p.get("Source URL")) or notion.scalar(p.get("Working URL"))
    if not url or not canonical(url):
        return False
    status = notion.scalar(p.get("Status"))
    aid = articles.ensure(con, sources, url=url, run_id=run_id,
                          title=notion.plain(p.get("Extracted Title")))
    con.execute("""UPDATE core.article SET notion_page_id=%s, digest_status=%s,
                   subtitle=coalesce(subtitle,%s), updated_at=now() WHERE article_id=%s""",
                (row["id"], status, notion.plain(p.get("Subtitle")), aid))
    cur = con.execute("SELECT retrieval_status FROM core.article WHERE article_id=%s",
                      (aid,)).fetchone()
    # Текст редакції беремо, лише якщо Notion каже, що він є, а в нас його ще немає
    if STATUS_TO_RETRIEVAL.get(status) == "full_text" and cur["retrieval_status"] != "full_text":
        body = notion.article_body(row["id"])
        if body.get("full_text"):
            articles.record(con, aid, method="notion_editor", text=body["full_text"],
                            run_id=run_id, verified=True)
            return True
    return False


def run(ctx) -> dict:
    con = ctx.con
    if not config.NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN не задано")
    sources = candidates.load_sources(con)
    stats = {}
    for name, db_id in config.NOTION_DB.items():
        key = f"{name}.last_edited_time"
        since = db.get_watermark(con, ctx.job, key)
        if since:
            since = (datetime.fromisoformat(since.replace("Z", "+00:00")) - OVERLAP).isoformat()
        n = changed = texts = 0
        last = None
        for row in notion.query_since(db_id, since, LIMITS[name]):
            n += 1
            last = row["last_edited_time"]
            if row.get("archived"):
                continue
            is_new = _raw(con, name, row)
            if not is_new:
                continue
            changed += 1
            if name == "post_metrics":
                sync_post(con, row)
            elif name == "articles":
                texts += sync_article(con, sources, row, ctx.run_id)
        if last:
            db.set_watermark(con, ctx.job, key, last)
        stats[name] = {"read": n, "changed": changed, **({"texts": texts} if texts else {})}
    return stats
