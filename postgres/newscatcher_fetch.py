"""Дотягування текстів статей через NewsCatcher News API v3 (тріал).

Два прогони одним скриптом:
  --missing   статті з `training_pairs_todo.csv`, яких нам бракує (пейвол, антибот, тизер)
  --control   статті, чий повний текст у нас уже є (з 🧾 Articles або екстрактора) —
              щоб виміряти, яку частку тексту віддає API, а не лише «знайшов/не знайшов»

`search_by_link` приймає до 100 посилань за запит. Посилання лише на статті:
домашня сторінка в пачці валить увесь запит з 422 «search is too broad».

Ключ — у `.env.local` (NEWSCATCHER_V3_KEY), у код і логи не пишеться.

Вихід: data/raw/newscatcher_v3.jsonl — рядок на кожне запитане посилання, резюмиться.

Запуск:  python postgres/newscatcher_fetch.py --missing [--limit 300]
         python postgres/newscatcher_fetch.py --control --limit 300
"""

from __future__ import annotations

import csv
import json
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
OUT = ROOT / "data" / "raw" / "newscatcher_v3.jsonl"
API = "https://v3-api.newscatcherapi.com/api/search_by_link"
BATCH = 100

csv.field_size_limit(10 ** 8)


def key() -> str:
    for line in (ROOT / ".env.local").read_text(encoding="utf-8").splitlines():
        if line.startswith("NEWSCATCHER_V3_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("Немає NEWSCATCHER_V3_KEY у .env.local")


def norm(u: str) -> str:
    u = (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return re.sub(r"^https?://(www\.|m\.|amp\.)?", "", u)


def call(links: list, token: str):
    # Без from_ пошук за посиланням дивиться лише на останні 30 днів — у плейбуку
    # цього немає. Березневі статті: 0 із 30 без дати, 27 із 30 з from_.
    body = json.dumps({"links": links, "page_size": BATCH,
                       "from_": "2024-01-01", "to_": "now"}).encode()
    for attempt in range(4):
        req = request.Request(API, data=body, method="POST", headers={
            "x-api-token": token, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0"})
        t = time.time()
        try:
            with request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read()), time.time() - t
        except error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, msg, time.time() - t
        except Exception as e:                                   # noqa: BLE001
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return type(e).__name__, str(e)[:200], time.time() - t
    return "retries", "", 0.0


def original_urls() -> dict:
    """Нормалізований URL → адреса, як вона була в пості.

    `search_by_link` шукає точний збіг: `reuters.com/…` без `www.` не знаходиться,
    а в наших файлах пар посилання вже нормалізовані. Перший прогін через це дав
    0 знайдених із 2 205.
    """
    out = {}
    for r in csv.DictReader((PROC / "link_scan.csv").open(encoding="utf-8")):
        if r["kind"] == "news":
            out.setdefault(norm(r["final_url"]), r["final_url"].split("#")[0])
    pm = ROOT / "data" / "raw" / "notion" / "_pm_live.json"
    if pm.exists():
        for r in json.load(pm.open(encoding="utf-8")):
            v = (r["properties"].get("Source / Evidence (regex)") or {}).get("rich_text") or []
            m = re.search(r"https?://\S+", "".join(t.get("plain_text", "") for t in v))
            if m:
                out.setdefault(norm(m.group(0)), m.group(0).rstrip(".,;:"))
    return out


# Схоже на статтю: у шляху є сегмент із дефісом, цифрами або довгий
ARTICLE_PATH = re.compile(r"^https?://[^/]+/.*([a-z0-9]+-[a-z0-9]+|\d{4,}|[a-z0-9_]{12,})", re.I)


def missing_urls() -> list:
    orig = original_urls()
    rows = csv.DictReader((PROC / "training_pairs_todo.csv").open(encoding="utf-8"))
    seen, out = set(), []
    for r in rows:
        if not r["missing"].startswith("article:"):
            continue
        n = norm(r["article_url"])
        if n in seen:
            continue
        seen.add(n)
        u = orig.get(n, r["article_url"])
        if ARTICLE_PATH.search(u):
            out.append((u, r["missing"].split(":", 1)[1]))
    return out


def control_urls() -> list:
    orig = original_urls()
    rows = csv.DictReader((PROC / "training_pairs.csv").open(encoding="utf-8"))
    seen, out = set(), []
    for r in rows:
        n = norm(r["article_url"])
        if n in seen:
            continue
        seen.add(n)
        out.append((orig.get(n, r["article_url"]), "control:" + r["article_text_src"]))
    return out


def main() -> None:
    args = sys.argv[1:]
    mode = "control" if "--control" in args else "missing"
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    token = key()

    urls = control_urls() if mode == "control" else missing_urls()
    if mode == "control":
        random.seed(20260916)
        random.shuffle(urls)

    done = set()
    if OUT.exists():
        for line in OUT.open(encoding="utf-8"):
            try:
                done.add(norm(json.loads(line)["requested"]))
            except Exception:
                pass
    urls = [u for u in urls if norm(u[0]) not in done]
    if limit:
        urls = urls[:limit]
    print(f"Режим {mode}: до запиту {len(urls):,} посилань, пачками по {BATCH}", flush=True)

    stats, lat, codes = Counter(), [], Counter()
    with OUT.open("a", encoding="utf-8") as fh:
        for i in range(0, len(urls), BATCH):
            batch = urls[i:i + BATCH]
            status, data, dt = call([u for u, _ in batch], token)
            # Одне «не статейне» посилання валить усю пачку з 422 — викидаємо його
            # і повторюємо, не більше 10 разів на пачку.
            for _ in range(10):
                if status != 422 or not isinstance(data, str):
                    break
                m = re.search(r"Link '([^']+)'", data)
                if not m:
                    break
                bad = m.group(1)
                kept = [b for b in batch if not b[0].startswith(bad)]
                if len(kept) == len(batch):
                    break
                stats["викинуто_422"] += len(batch) - len(kept)
                batch = kept
                status, data, dt = call([u for u, _ in batch], token)
            codes[status] += 1
            lat.append(dt)
            found = {}
            if status == 200 and isinstance(data, dict):
                for a in data.get("articles", []):
                    for cand in (a.get("link"), a.get("canonical_url")):
                        if cand:
                            found[norm(cand)] = a
            elif status != 200:
                print(f"  пачка {i // BATCH + 1}: HTTP {status} — {str(data)[:160]}", flush=True)
            now = datetime.now(timezone.utc).isoformat()
            for u, why in batch:
                a = found.get(norm(u))
                stats["знайдено" if a else "не знайдено"] += 1
                fh.write(json.dumps({
                    "requested": u, "mode": mode, "why": why, "http_status": status,
                    "found": bool(a),
                    "domain": (a or {}).get("domain_url") or norm(u).split("/")[0],
                    "content": (a or {}).get("content") or "",
                    "chars": len((a or {}).get("content") or ""),
                    "word_count": (a or {}).get("word_count"),
                    "paid_content": (a or {}).get("paid_content"),
                    "title": (a or {}).get("title"),
                    "published_date": (a or {}).get("published_date"),
                    "fetched_at": now,
                }, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  {min(i + BATCH, len(urls)):>5,}/{len(urls):,} · знайдено "
                  f"{stats['знайдено']:,} · {dt:.1f}с на пачку", flush=True)
            time.sleep(1)

    print(f"\nЗапитів: {len(lat)} · коди: {dict(codes)} · "
          f"середня затримка {sum(lat) / max(len(lat), 1):.1f}с")
    for k, v in stats.most_common():
        print(f"  {k:14} {v:,}")


if __name__ == "__main__":
    main()
