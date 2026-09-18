"""NewsCatcher News API v3: пошук і пошук за посиланням, з обліком кожного виклику.

Що кусається (виміряно на тріалі 16–17.09.2026):
  * search_by_link без from_ дивиться лише 30 днів і мовчки каже «не знайдено»;
  * потрібен точний збіг адреси: без www. стаття не знаходиться;
  * одне посилання на домашню сторінку валить усю пачку з 422;
  * у платних виданнях content — лише початок статті, а word_count рахує
    слова вже обрізаного тексту, тож обрізаність із відповіді не видно.
"""

from __future__ import annotations

import json
import re
import time
from urllib import error, request

from . import config, db

BASE = "https://v3-api.newscatcherapi.com"
PROVIDER = "newscatcher_v3"

# Видання, де API віддає прев'ю, а не статтю. Контроль проти повних копій
# редакції: NYT 27%, Telegraph 15%, Economist 6%, Foreign Affairs 2%.
PREVIEW_ONLY = ("nytimes.com", "telegraph.co.uk", "thetimes.com", "ft.com",
                "economist.com", "washingtonpost.com", "bloomberg.com", "wsj.com",
                "foreignaffairs.com", "politico.eu", "lemonde.fr", "foxnews.com", "scmp.com")
FULL_MIN_CHARS = 1500


def is_preview_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in PREVIEW_ONLY)


def usable_text(article: dict, domain: str) -> str | None:
    """Текст, який можна вважати повним. Інакше None."""
    c = article.get("content") or ""
    if (len(c) >= FULL_MIN_CHARS and not article.get("paid_content")
            and not is_preview_domain(domain) and "subscribe" not in c[-300:].lower()):
        return c
    return None


def _post(endpoint: str, body: dict, ctx) -> tuple:
    if not config.NEWSCATCHER_V3_KEY:
        raise RuntimeError("NEWSCATCHER_V3_KEY не задано")
    data = json.dumps(body).encode()
    status, payload, t0 = None, None, time.time()
    for attempt in range(4):
        req = request.Request(BASE + endpoint, data=data, method="POST", headers={
            "x-api-token": config.NEWSCATCHER_V3_KEY, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"})
        t0 = time.time()
        try:
            with request.urlopen(req, timeout=90) as r:
                status, payload = r.status, json.loads(r.read())
                break
        except error.HTTPError as e:
            status, payload = e.code, e.read().decode("utf-8", "replace")[:500]
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            break
        except Exception as e:                                   # noqa: BLE001
            status, payload = type(e).__name__, str(e)[:300]
            time.sleep(3 * (attempt + 1))
    returned = len(payload.get("articles") or []) if isinstance(payload, dict) else None
    if isinstance(payload, dict) and payload.get("clusters"):
        returned = sum(len(c.get("articles") or []) for c in payload["clusters"])
    log_body = {k: v for k, v in body.items() if k != "links"}
    if "links" in body:
        log_body["links_n"] = len(body["links"])
    db.log_api_call(ctx.con, provider=PROVIDER, endpoint=endpoint, run_id=ctx.run_id,
                    request=log_body, http_status=status,
                    items_requested=len(body.get("links") or []) or None,
                    items_returned=returned, latency_ms=int((time.time() - t0) * 1000),
                    error=None if status == 200 else str(payload)[:500])
    return status, payload


def budget_left(ctx) -> int:
    return config.NEWSCATCHER_MAX_CALLS_PER_DAY - db.api_calls_today(ctx.con, PROVIDER)


def search(ctx, body: dict) -> list:
    """Статті з /api/search; якщо увімкнено кластеризацію — розгорнуті з кластерів."""
    status, d = _post("/api/search", body, ctx)
    if status != 200 or not isinstance(d, dict):
        ctx.log.warning("search: HTTP %s %s", status, str(d)[:200])
        return []
    if d.get("clusters"):
        out = []
        for c in d["clusters"]:
            for a in c.get("articles") or []:
                a["_cluster_id"] = c.get("cluster_id")
                a["_cluster_size"] = c.get("cluster_size")
                out.append(a)
        return out
    return d.get("articles") or []


def by_links(ctx, links: list) -> dict:
    """{запитане посилання: стаття}. Пачками по 100, погані посилання викидає."""
    out = {}
    for i in range(0, len(links), 100):
        batch = list(links[i:i + 100])
        for _ in range(10):
            if not batch:
                break
            status, d = _post("/api/search_by_link",
                              {"links": batch, "page_size": 100,
                               "from_": "2024-01-01", "to_": "now"}, ctx)
            if status == 422 and isinstance(d, str):
                m = re.search(r"Link '([^']+)'", d)
                if m:
                    batch = [b for b in batch if not b.startswith(m.group(1))]
                    continue
            break
        if status == 200 and isinstance(d, dict):
            index = {}
            for a in d.get("articles") or []:
                for k in (a.get("link"), a.get("canonical_url")):
                    if k:
                        index[_n(k)] = a
            for u in batch:
                if _n(u) in index:
                    out[u] = index[_n(u)]
        time.sleep(1)
    return out


def _n(u: str) -> str:
    u = (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return re.sub(r"^https?://(www\.|m\.|amp\.)?", "", u)
