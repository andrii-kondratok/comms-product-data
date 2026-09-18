"""Пошук сторінки Content Pulse для постів, у яких немає relation.

7 165 рядків Post Metrics не мають тексту поста, бо не привʼязані до Content Pulse.
Для 2022–2024 це безнадійно — сторінок тоді не існувало. Але за 2025 і без дати
сторінки є (4 096 за 2025), їх просто ніхто не звʼязав.

Шукаємо не семантикою, а тим самим порівнянням, що вже працює всередині сторінки:
`Name` — це справжній початок поста, обрізаний на 200 знаках. Тобто задача —
знайти сторінку, де цей текст трапляється дослівно.

Щоб не робити 2 500 × 6 000 порівнянь, спершу індекс по 5-словних шинглах:
кандидатами стають лише сторінки, що ділять із `Name` хоч один шингл.

Шукаємо по `body_for_data_analysis` зі знімка — у ньому знищена пунктуація, але
для порівняння це байдуже, бо normalize() її і так прибирає. Справжнє тіло
сторінки качаємо лише для підтверджених збігів.

Запуск:
    python postgres/link_orphan_posts.py              # сухий прогін
    python postgres/link_orphan_posts.py --apply      # проставити relation
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import enrich_post_metrics as E  # noqa: E402

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw" / "notion"
REPORT = PROC / "orphan_post_links.csv"

SHINGLE = 5
MIN_RATIO = 0.80
MAX_CANDIDATES = 8
MAX_POSITIONS = 6
RATE_DELAY = 0.34


def words_of(text: str) -> list:
    return E.normalize(text).split()


def build_index(pages: dict):
    """шингл → список (page_id, позиція). Позицій на шингл тримаємо небагато."""
    index = defaultdict(list)
    for pid, w in pages.items():
        for i in range(len(w) - SHINGLE + 1):
            key = " ".join(w[i:i + SHINGLE])
            lst = index[key]
            if len(lst) < 40:
                lst.append((pid, i))
    return index


def best_match(name_words: list, index, pages: dict):
    if len(name_words) < SHINGLE:
        return None, 0.0
    hits = Counter()
    positions = defaultdict(list)
    for i in range(len(name_words) - SHINGLE + 1):
        for pid, pos in index.get(" ".join(name_words[i:i + SHINGLE]), ()):
            hits[pid] += 1
            if len(positions[pid]) < MAX_POSITIONS:
                positions[pid].append(max(0, pos - i))

    name_norm = " ".join(name_words)
    best, ratio = None, 0.0
    for pid, _ in hits.most_common(MAX_CANDIDATES):
        w = pages[pid]
        for start in positions[pid]:
            cand = " ".join(w[start:start + len(name_words)])
            r = E.SequenceMatcher(None, name_norm, cand).ratio()
            if r > ratio:
                best, ratio = pid, r
    return best, ratio


def main() -> None:
    apply = "--apply" in sys.argv

    snaps = sorted(RAW.glob("content_pulse__*.jsonl"))
    if not snaps:
        raise SystemExit("Немає знімка Content Pulse")
    cp_words, cp_meta = {}, {}
    for line in snaps[-1].open(encoding="utf-8"):
        r = json.loads(line)
        v = r["properties"].get("body_for_data_analysis") or {}
        body = "".join(t.get("plain_text", "") for t in v.get("rich_text") or [])
        if not body.strip():
            continue
        w = words_of(body)
        if len(w) < SHINGLE:
            continue
        cp_words[r["notion_page_id"]] = w
        cp_meta[r["notion_page_id"]] = {
            "date": ((r["properties"].get("Date") or {}).get("date") or {}).get("start")
                    or r.get("created_time", ""),
            "url": r.get("url", ""),
        }
    print(f"Сторінок Content Pulse із тілом: {len(cp_words):,}")

    print("Будую індекс…", flush=True)
    index = build_index(cp_words)
    print(f"  шинглів: {len(index):,}")

    pm = E.query_post_metrics(E.load_token())
    orphans = []
    for r in pm:
        p = r["properties"]
        if (p.get(E.F_REL) or {}).get("relation"):
            continue
        name = E.title_of(p)
        if len(name) < 40:
            continue
        orphans.append({
            "pm_page_id": r["id"], "name": name,
            "post_date": ((p.get("Post date") or {}).get("date") or {}).get("start") or "",
            "platform": ((p.get("Platform") or {}).get("select") or {}).get("name") or "",
        })
    print(f"Постів без звʼязку: {len(orphans):,}")

    rows, stats = [], Counter()
    started = time.time()
    for i, o in enumerate(orphans, 1):
        pid, ratio = best_match(words_of(o["name"]), index, cp_words)
        meta = cp_meta.get(pid, {}) if pid else {}
        gap = ""
        if pid and o["post_date"] and meta.get("date"):
            # Тільки календарна дата: `Post date` буває без часу, а created_time —
            # з таймзоною, і віднімання naive від aware тихо падало в except.
            try:
                from datetime import date
                a = date.fromisoformat(o["post_date"][:10])
                b = date.fromisoformat(meta["date"][:10])
                gap = abs((a - b).days)
            except Exception:
                gap = ""
        ok = ratio >= MIN_RATIO
        stats["знайдено" if ok else "не знайдено"] += 1
        if ok and isinstance(gap, int) and gap > 14:
            stats["  └ дата розходиться >14 днів"] += 1
        rows.append({**o, "cp_page_id": pid or "", "ratio": round(ratio, 3),
                     "cp_date": meta.get("date", ""), "day_gap": gap,
                     "cp_url": meta.get("url", "")})
        if i % 500 == 0 or i == len(orphans):
            el = time.time() - started
            print(f"  {i:>6,}/{len(orphans):,} · знайдено {stats['знайдено']:,} "
                  f"· {el:.0f}с", flush=True)

    with REPORT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["pm_page_id", "name", "post_date", "platform",
                                           "cp_page_id", "ratio", "cp_date", "day_gap",
                                           "cp_url"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # розподіл схожості — щоб поріг не був вірою
    buckets = Counter()
    for r in rows:
        buckets[f"{min(int(r['ratio'] * 10) / 10, 0.99):.1f}"] += 1
    print("\nРозподіл схожості:")
    for k in sorted(buckets, reverse=True):
        print(f"  {k}+   {buckets[k]:>6,}")

    for k, v in stats.most_common():
        print(f"  {k:32} {v:,}")

    if not apply:
        print(f"\nСУХИЙ ПРОГІН. Звіт → {REPORT}")
        return

    # Дата — незалежний від тексту запобіжник, і він себе виправдав: спіймав три
    # збіги на повторюваному контенті (вишиванка, річниця, «наші студенти чудові»),
    # де текст майже дослівний, а пости різних років.
    good = [r for r in rows if r["ratio"] >= MIN_RATIO and r["cp_page_id"]
            and not (r["day_gap"] != "" and int(r["day_gap"]) > 14)]
    print(f"\nПроставляю relation для {len(good):,} рядків…", flush=True)
    written, errors = 0, 0
    token = E.load_token()
    for i, r in enumerate(good, 1):
        try:
            E.patch_page(r["pm_page_id"], token,
                         {E.F_REL: {"relation": [{"id": r["cp_page_id"]}]}})
            written += 1
        except Exception as e:                                   # noqa: BLE001
            errors += 1
            if errors <= 3:
                print("  помилка:", str(e)[:120])
        time.sleep(RATE_DELAY)
        if i % 200 == 0:
            print(f"  {i:,}/{len(good):,}", flush=True)
    print(f"Проставлено: {written:,} · помилок: {errors}")
    print("Далі: python postgres/enrich_post_metrics.py --apply")


if __name__ == "__main__":
    main()
