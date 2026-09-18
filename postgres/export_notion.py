"""Вивантаження баз Notion у сирий шар.

Пише JSONL — по рядку на сторінку Notion, з повними properties як вони прийшли.
Це і є `raw.notion_export` зі схеми: знімок того, що ми читали в конкретний момент.
Notion правлять руками, тож без знімка через місяць нічого не доведеш.

Розбір у `core.post` — окремим кроком (load_posts.py), щоб помилка розбору
не змушувала качати все заново.

Запуск:
    python postgres/export_notion.py                 # усі бази
    python postgres/export_notion.py content_pulse   # одну

Токен береться з D:\\Posts analysis\\.env (NOTION_TOKEN) або зі змінної оточення.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

ENV_FILE = Path(r"D:\Posts analysis\.env")
OUT_DIR = Path(__file__).parent.parent / "data" / "raw" / "notion"

NOTION_VERSION = "2022-06-28"
PAGE_SIZE = 100
RATE_DELAY = 0.35          # Notion тримає ~3 запити/с
MAX_RETRIES = 5

DATABASES = {
    "content_pulse": "bd9d1fbd-7688-825d-84d5-8102fcb47d15",
    "post_metrics":  "570dde03-224a-4f17-8fd3-bbab048e8ca0",
    "articles":      "7c06f91f-100f-4202-8f74-135e547aae44",
    "post_outputs":  "ea32ae25-8bda-4c67-9eb2-943ef758c691",
    "signal_desk":   "7b688077-bb34-4317-a190-a200b1735e9f",
}


def load_token() -> str:
    token = os.environ.get("NOTION_TOKEN")
    if token:
        return token
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("NOTION_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("Немає NOTION_TOKEN ні в оточенні, ні в .env")


def post_json(url: str, token: str, body: dict) -> dict:
    """POST із повторами на 429 та 5xx — Notion регулярно віддає і те, і те."""
    payload = json.dumps(body).encode("utf-8")
    for attempt in range(MAX_RETRIES):
        req = request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                wait = float(e.headers.get("Retry-After", 2 ** attempt))
                print(f"    HTTP {e.code}, чекаю {wait:.0f}с "
                      f"(спроба {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise SystemExit(f"Notion API {e.code}: {e.read().decode('utf-8')[:400]}")
        except (error.URLError, TimeoutError, OSError) as e:
            # Notion регулярно обриває довгі читання по таймауту — це не помилка даних.
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise SystemExit(f"Мережа: {e}")
    raise SystemExit("Вичерпано спроби")


def export_database(name: str, database_id: str, token: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"{name}__{stamp}.jsonl"

    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    cursor, total, pages = None, 0, 0
    started = time.time()

    with out.open("w", encoding="utf-8") as fh:
        while True:
            body: dict = {"page_size": PAGE_SIZE}
            if cursor:
                body["start_cursor"] = cursor

            data = post_json(url, token, body)
            results = data.get("results", [])
            for row in results:
                fh.write(json.dumps({
                    "notion_page_id": row.get("id"),
                    "created_time": row.get("created_time"),
                    "last_edited_time": row.get("last_edited_time"),
                    "archived": row.get("archived"),
                    "url": row.get("url"),
                    "properties": row.get("properties"),
                }, ensure_ascii=False) + "\n")

            total += len(results)
            pages += 1
            print(f"  {name}: {total:>6,} рядків ({pages} сторінок)", end="\r", flush=True)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            time.sleep(RATE_DELAY)

    secs = time.time() - started
    print(f"  {name}: {total:>6,} рядків за {secs:.0f}с → {out.name}" + " " * 16)

    # Маніфест: що, коли, скільки. Провенанс окремо від даних.
    manifest = out.with_suffix(".manifest.json")
    manifest.write_text(json.dumps({
        "database": name,
        "database_id": database_id,
        "notion_version": NOTION_VERSION,
        "rows": total,
        "api_pages": pages,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "file": out.name,
        "seconds": round(secs, 1),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    token = load_token()
    wanted = sys.argv[1:] or list(DATABASES)

    unknown = [w for w in wanted if w not in DATABASES]
    if unknown:
        raise SystemExit(f"Невідомі бази: {unknown}. Доступні: {list(DATABASES)}")

    print(f"Вивантаження в {OUT_DIR}\n")
    for name in wanted:
        try:
            export_database(name, DATABASES[name], token)
        except SystemExit as e:
            print(f"  {name}: ПОМИЛКА — {e}")


if __name__ == "__main__":
    main()
