"""Докачування тіл статей із Notion.

У 🧾 Articles текст не в properties, а в тілі сторінки: два тогли —
«Key points» і «Full text». Тож на статтю йде ~3 запити: верхній рівень
плюс діти кожного тогла.

Робота довга (~2 900 статей), тому:
  * пише інкрементально, по рядку на статтю;
  * при повторному запуску пропускає вже завантажені — можна перервати будь-коли;
  * витримує 429 і 5xx із повторами.

Запуск:  python postgres/export_article_bodies.py [--limit N]
"""

from __future__ import annotations

import glob
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).parent))
from export_notion import NOTION_VERSION, load_token  # noqa: E402

RAW = Path(__file__).parent.parent / "data" / "raw" / "notion"
OUT = RAW / "article_bodies.jsonl"

RATE_DELAY = 0.34
MAX_RETRIES = 5


def get_json(url: str, token: str) -> dict:
    for attempt in range(MAX_RETRIES):
        req = request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
        })
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                time.sleep(float(e.headers.get("Retry-After", 2 ** attempt)))
                continue
            if e.code in (403, 404):      # сторінку видалили або закрили доступ
                return {"results": [], "has_more": False, "_error": e.code}
            raise
        except error.URLError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return {"results": [], "has_more": False, "_error": "retries"}


def block_text(block: dict) -> str:
    t = block.get("type")
    rt = (block.get(t) or {}).get("rich_text") or []
    return "".join(x.get("plain_text", "") for x in rt)


def children(block_id: str, token: str) -> list[dict]:
    """Усі діти блока, з пагінацією."""
    out, cursor = [], None
    while True:
        url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        d = get_json(url, token)
        out.extend(d.get("results", []))
        if not d.get("has_more"):
            return out
        cursor = d.get("next_cursor")
        time.sleep(RATE_DELAY)


def fetch_body(page_id: str, token: str) -> dict:
    top = children(page_id, token)
    intro, sections = [], {}
    for b in top:
        if b.get("type") == "toggle":
            label = block_text(b).strip().lower()
            time.sleep(RATE_DELAY)
            kids = children(b["id"], token)
            text = "\n".join(filter(None, (block_text(k) for k in kids)))
            key = ("full_text" if "full" in label
                   else "key_points" if "key" in label
                   else label[:40] or "other")
            sections[key] = text
        else:
            s = block_text(b)
            if s:
                intro.append(s)
    return {
        "intro": "\n".join(intro) or None,
        "key_points": sections.get("key_points"),
        "full_text": sections.get("full_text"),
        "other_sections": {k: v for k, v in sections.items()
                           if k not in ("full_text", "key_points")} or None,
        "top_blocks": len(top),
    }


def main() -> None:
    token = load_token()
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    src = sorted(glob.glob(str(RAW / "articles__*.jsonl")))
    if not src:
        raise SystemExit("Немає знімка articles. Спершу export_notion.py articles")
    pages = [json.loads(l)["notion_page_id"] for l in Path(src[-1]).open(encoding="utf-8")]

    done: set[str] = set()
    if OUT.exists():
        for line in OUT.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["notion_page_id"])
            except Exception:
                pass

    todo = [p for p in pages if p not in done]
    if limit:
        todo = todo[:limit]
    print(f"Усього статей: {len(pages):,} · уже є: {len(done):,} · качаю: {len(todo):,}")

    started, errors, chars = time.time(), 0, 0
    with OUT.open("a", encoding="utf-8") as fh:
        for i, pid in enumerate(todo, 1):
            try:
                body = fetch_body(pid, token)
            except Exception as e:                       # noqa: BLE001
                body = {"error": str(e)[:200]}
                errors += 1
            body["notion_page_id"] = pid
            body["fetched_at"] = datetime.now(timezone.utc).isoformat()
            fh.write(json.dumps(body, ensure_ascii=False) + "\n")
            fh.flush()
            chars += len(body.get("full_text") or "")

            if i % 25 == 0 or i == len(todo):
                el = time.time() - started
                eta = el / i * (len(todo) - i)
                print(f"  {i:>5,}/{len(todo):,} · помилок {errors} · "
                      f"{chars / max(i, 1):,.0f} знаків/статтю · "
                      f"лишилось ~{eta / 60:.0f} хв", flush=True)
            time.sleep(RATE_DELAY)

    print(f"\nГотово за {(time.time() - started) / 60:.1f} хв → {OUT.name}")


if __name__ == "__main__":
    main()
