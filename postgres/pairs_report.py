"""Де ми втрачаємо пари «стаття → пост».

Одна воронка від усіх рядків Post Metrics до готової пари, і окремо — розріз
двох різних дір: немає тексту поста vs немає тексту статті. Це різні проблеми
з різними власниками, тому рахуються окремо.

Запуск:  python postgres/pairs_report.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import enrich_post_metrics as E  # noqa: E402
from build_training_pairs import MIN_POST_CHARS, is_news, norm_url  # noqa: E402

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw"

csv.field_size_limit(10 ** 8)


def main() -> None:
    pm = E.query_post_metrics(E.load_token())

    reg = {}
    for r in csv.DictReader((ROOT / "posts_db" / "sources.csv").open(encoding="utf-8")):
        d = (r.get("domain") or "").lower()
        if d:
            reg[d] = r.get("extract_verdict") or "unknown"

    fetched = {}
    fp = RAW / "fetched_articles.jsonl"
    if fp.exists():
        for line in fp.open(encoding="utf-8"):
            try:
                f = json.loads(line)
                fetched[norm_url(f["url"])] = f["verdict"]
            except Exception:
                pass

    paired = {r["pm_page_id"] for r in
              csv.DictReader((PROC / "training_pairs.csv").open(encoding="utf-8"))}

    f = Counter()          # воронка
    grid = Counter()       # є текст поста × є посилання
    why = Counter()        # чому пари немає
    doms = Counter()

    for r in pm:
        p = r["properties"]
        post = E.rt(p, E.F_TEXT)
        src = E.rt(p, E.F_SOURCE)
        rel = bool((p.get(E.F_REL) or {}).get("relation"))
        m = E.PLAIN_URL.search(src) if src else None
        url = m.group(0).rstrip(".,;:") if m else ""
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower() if url else ""
        news = bool(host) and is_news(host)

        has_post = len(post) >= MIN_POST_CHARS
        f["1_усього"] += 1
        grid[("пост є" if has_post else "поста немає",
              "лінк на медіа" if news
              else "лінк на соцмережу/своє" if url
              else "лінка немає")] += 1

        if news:
            f["2_є_лінк_на_медіа"] += 1
        if news and has_post:
            f["3_плюс_текст_поста"] += 1
        if r["id"] in paired:
            f["4_готова_пара"] += 1
            continue

        # чому не пара
        if not news:
            why["немає лінка на статтю" if not url else "лінк не на статтю"] += 1
            if not url and not rel:
                why["  └ з них: старий пост без звʼязку з Content Pulse"] += 1
            continue
        if not has_post:
            why["є стаття, але немає тексту поста"] += 1
            continue
        v = fetched.get(norm_url(url)) or reg.get(host)
        if v in ("blocked_or_empty",):
            why["стаття: антибот або пейвол"] += 1
        elif v in ("teaser", "partial"):
            why["стаття: віддає лише тизер"] += 1
        elif v is None:
            why["стаття: домен ще не тестований"] += 1
            doms[host] += 1
        else:
            why["стаття: не дотягнута з інших причин"] += 1

    print("ВОРОНКА")
    for k in sorted(f):
        print(f"  {k:24} {f[k]:>7,}")

    print("\nРОЗРІЗ: текст поста × посилання")
    rows = ["пост є", "поста немає"]
    cols = ["лінк на медіа", "лінк на соцмережу/своє", "лінка немає"]
    print(f"  {'':14}" + "".join(f"{c:>26}" for c in cols))
    for r_ in rows:
        print(f"  {r_:14}" + "".join(f"{grid[(r_, c)]:>26,}" for c in cols))

    print("\nЧОМУ НЕМАЄ ПАРИ")
    for k, v in why.most_common():
        print(f"  {k:46} {v:>7,}")

    if doms:
        print("\nНетестовані домени, топ-15:")
        for d, n in doms.most_common(15):
            print(f"    {d:30} {n}")


if __name__ == "__main__":
    main()
