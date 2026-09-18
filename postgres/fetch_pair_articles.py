"""Дотягування текстів статей для пар «стаття → пост».

Бере `training_pairs_todo.csv` — пости, у яких є посилання на статтю, але самої
статті з текстом у нас немає — і витягує текст власним екстрактором. Якість
домену вже виміряна в `posts_db/sources.csv`, тож за замовчуванням ходимо лише
туди, де вердикт `full`: решта або віддає тизер, або блокує анонімного читача.

Ходимо з браузерним UA: без нього Reuters, AP, Axios і Politico віддають 401/403,
і попередній замір показував по них нуль на цілком відкритих сайтах.

Резюмиться: результат пишеться по рядку одразу, повторний запуск пропускає готове.

Запуск:
    python postgres/fetch_pair_articles.py                 # домени з вердиктом full
    python postgres/fetch_pair_articles.py --untested      # ще й нетестовані домени
    python postgres/fetch_pair_articles.py --limit 50
    python postgres/fetch_pair_articles.py --todo data/processed/article_fetch_todo.csv --untested
"""

from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

import trafilatura

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
REG = ROOT / "posts_db" / "sources.csv"
TODO = PROC / "training_pairs_todo.csv"
OUT = ROOT / "data" / "raw" / "fetched_articles.jsonl"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
}
DELAY = 1.2            # пауза між запитами до одного домену
GOOD_CHARS = 1500      # нижче цього — тизер, а не стаття
OVER_CHARS = 80_000    # вище — парсер зачепив навігацію, а не текст


def fetch(url: str):
    try:
        req = request.Request(url, headers=HEADERS)
        with request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except error.HTTPError as e:
        return e.code, ""
    except Exception as e:                                   # noqa: BLE001
        return type(e).__name__, ""


def verdicts() -> dict:
    out = {}
    for r in csv.DictReader(REG.open(encoding="utf-8")):
        d = (r.get("domain") or "").lower()
        if d:
            out[d] = r.get("extract_verdict") or "unknown"
    return out


def main() -> None:
    args = sys.argv[1:]
    untested = "--untested" in args
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None

    reg = verdicts()
    todo_path = Path(args[args.index("--todo") + 1]) if "--todo" in args else TODO
    rows = list(csv.DictReader(todo_path.open(encoding="utf-8")))

    done = set()
    if OUT.exists():
        for line in OUT.open(encoding="utf-8"):
            try:
                done.add(json.loads(line)["url"])
            except Exception:
                pass

    todo, skipped = [], Counter()
    seen = set()
    for r in rows:
        url, dom = r["article_url"], r["article_domain"]
        if url in done or url in seen:
            skipped["вже_є"] += 1
            continue
        v = reg.get(dom)
        if v == "full" or (untested and v is None):
            seen.add(url)
            todo.append(r)
        else:
            skipped[v or "нетестований"] += 1

    if limit:
        todo = todo[:limit]
    print(f"До завантаження: {len(todo):,} статей")
    print("  пропущено:", dict(skipped))

    # Розкладаємо по доменах по колу, щоб не бити один сайт підряд.
    by_dom = {}
    for r in todo:
        by_dom.setdefault(r["article_domain"], []).append(r)
    order = []
    while any(by_dom.values()):
        for d in list(by_dom):
            if by_dom[d]:
                order.append(by_dom[d].pop())

    stats = Counter()
    started = time.time()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        for i, r in enumerate(order, 1):
            url = r["article_url"]
            status, html = fetch(url)
            text = ""
            if html:
                text = trafilatura.extract(
                    html, include_comments=False, include_tables=False,
                    favor_precision=True) or ""

            n = len(text)
            if n == 0:
                verdict = "blocked_or_empty"
            elif n > OVER_CHARS:
                verdict = "over_extracted"
            elif n < GOOD_CHARS:
                verdict = "teaser"
            else:
                verdict = "full"
            stats[verdict] += 1

            fh.write(json.dumps({
                "url": url, "domain": r["article_domain"],
                "http_status": status, "chars": n, "verdict": verdict,
                "text": text if verdict in ("full", "teaser") else "",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False) + "\n")
            fh.flush()

            if i % 25 == 0 or i == len(order):
                el = time.time() - started
                eta = el / i * (len(order) - i) / 60
                print(f"  {i:>5,}/{len(order):,} · full {stats['full']:,} · "
                      f"teaser {stats['teaser']:,} · порожньо "
                      f"{stats['blocked_or_empty']:,} · ~{eta:.0f} хв", flush=True)
            time.sleep(DELAY)

    print(f"\nГотово за {(time.time() - started) / 60:.1f} хв")
    for k, v in stats.most_common():
        print(f"  {k:20} {v:,}")
    print(f"  → {OUT}")


if __name__ == "__main__":
    main()
