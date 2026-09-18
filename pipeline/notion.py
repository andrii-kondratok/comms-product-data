"""Мінімальний клієнт Notion REST: інкрементальний запит і тіло сторінки."""

from __future__ import annotations

import json
import time
from urllib import error, request

from . import config

VERSION = "2022-06-28"
DELAY = 0.35          # Notion тримає ~3 запити/с


def _call(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": VERSION, "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(float(e.headers.get("Retry-After", 2 ** attempt)))
                continue
            if e.code in (403, 404):
                return {"results": [], "has_more": False, "_error": e.code}
            raise RuntimeError(f"Notion {e.code}: {e.read().decode()[:300]}")
        except (error.URLError, TimeoutError, OSError):
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("Notion: вичерпано спроби")


def query_since(database_id: str, since_iso: str | None, limit: int | None = None):
    """Сторінки, змінені з `since_iso`, у порядку зростання last_edited_time.

    Порядок важливий: задача може зупинитись на ліміті й поставити водяний знак
    на останню оброблену сторінку — наступний запуск продовжить звідти.
    """
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    body: dict = {"page_size": 100,
                  "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]}
    if since_iso:
        body["filter"] = {"timestamp": "last_edited_time",
                          "last_edited_time": {"on_or_after": since_iso}}
    n, cursor = 0, None
    while True:
        if cursor:
            body["start_cursor"] = cursor
        d = _call("POST", url, body)
        for row in d.get("results", []):
            yield row
            n += 1
            if limit and n >= limit:
                return
        if not d.get("has_more"):
            return
        cursor = d.get("next_cursor")
        time.sleep(DELAY)


def children(block_id: str) -> list:
    out, cursor = [], None
    while True:
        url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        d = _call("GET", url)
        out.extend(d.get("results", []))
        if not d.get("has_more"):
            return out
        cursor = d.get("next_cursor")
        time.sleep(DELAY)


def block_text(b: dict) -> str:
    t = b.get("type")
    return "".join(x.get("plain_text", "") for x in ((b.get(t) or {}).get("rich_text") or []))


def article_body(page_id: str) -> dict:
    """У 🧾 Articles текст лежить у тоглах «Key points» і «Full text»."""
    out = {"key_points": None, "full_text": None}
    for b in children(page_id):
        if b.get("type") != "toggle":
            continue
        label = block_text(b).strip().lower()
        time.sleep(DELAY)
        text = "\n".join(filter(None, (block_text(k) for k in children(b["id"]))))
        if "full" in label:
            out["full_text"] = text or None
        elif "key" in label:
            out["key_points"] = text or None
    return out


# ------------------------------------------------------------- властивості

def plain(prop: dict | None) -> str | None:
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("rich_text", "title") and v:
        return "".join(x.get("plain_text", "") for x in v) or None
    if t == "url":
        return v
    return None


def scalar(prop: dict | None):
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "relation":
        return [x.get("id") for x in (v or []) if x] or None
    if t == "date":
        return (v or {}).get("start")
    return v
