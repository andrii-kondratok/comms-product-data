"""Привʼязка знайдених посилань до рядків Post Metrics.

`scan_links.py` знаходить посилання у твітах і FB-постах, але ключ там — id твіта
або треду. Тут переводимо його в рядок Post Metrics через `Post URL`
(x.com/Mylovanov/status/<id>), щоб рахувати пости, а не посилання, і бачити,
скільки нових пар «стаття → пост» це відкриває понад уже записане джерело.

Вихід: data/processed/post_article_links.csv — рядок Post Metrics × URL статті,
з позначкою, звідки взявся зв'язок.

Запуск:  python posts_db/links_to_posts.py
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw" / "notion"
DL = Path(r"C:\Users\Admin\Downloads")

csv.field_size_limit(10 ** 8)

STATUS = re.compile(r"/status(?:es)?/(\d+)")
# ідентифікатори FB-поста в різних формах посилань
FB_ID = re.compile(r"(pfbid[0-9A-Za-z]+|/posts/(\d+)|story_fbid=(\d+)|/videos/(\d+)|fbid=(\d+))")


def fb_key(url: str) -> str:
    m = FB_ID.search(url or "")
    if not m:
        return ""
    return next(g for g in m.groups() if g)


def norm(u: str) -> str:
    u = (u or "").split("#")[0]
    u = re.sub(r"[?&](utm_[^=&]+|fbclid|gclid|smid|mod|ref|s|t|st|reflink|"
               r"accessToken|giftCopy|share|sh|ito|at_[^=&]+)=[^&]*", "", u, flags=re.I)
    u = re.sub(r"[?&]+$", "", u).rstrip("/").lower()
    return re.sub(r"^https?://(www\.|m\.|amp\.)?", "", u)


def main() -> None:
    pm = json.load((RAW / "_pm_live.json").open(encoding="utf-8"))

    def prop(p, k):
        v = p.get(k) or {}
        return "".join(t.get("plain_text", "") for t in (v.get("rich_text") or v.get("title") or []))

    by_status, by_fb, pm_rows = {}, {}, {}
    for r in pm:
        p = r["properties"]
        url = (p.get("Post URL") or {}).get("url") or ""
        pm_rows[r["id"]] = {
            "year": (((p.get("Post date") or {}).get("date") or {}).get("start") or "")[:4],
            "platform": ((p.get("Platform") or {}).get("select") or {}).get("name") or "",
            "has_source": bool(prop(p, "Source / Evidence (regex)")),
            "cp": bool((p.get("Content Pulse item") or {}).get("relation")),
        }
        m = STATUS.search(url)
        if m:
            by_status[m.group(1)] = r["id"]
        k = fb_key(url)
        if k:
            by_fb[k] = r["id"]

    # В обох архівах ключ — id окремого твіта (у raw колонка лише зветься thread_id).
    # Посилання часто стоїть не в першому твіті треду, тож зводимо до кореня
    # через мапу з assemble_threads.py.
    tweet_root = {}
    for r in csv.DictReader((PROC / "tweet_thread_map.csv").open(encoding="utf-8")):
        tweet_root[r["tweet_id"]] = r["root_id"]

    # Посилання в X стоїть в окремому твіті «Source: <лінк>» одразу після `NX`.
    # Збирач закриває тред на `NX`, тож «Source:» стає окремою одиницею. Виміряно:
    # 4 354 таких твіти, медіана розриву 0 хв, 99% — до 2 хв. Чіпляємо до
    # попередньої одиниці, якщо розрив до години.
    label = re.compile(r"^\s*(source|sources|джерело|джерела|посилання|link|links|"
                       r"read more|детальніше|більше)\s*[:\-–—]?\s*$", re.I)
    units = []
    for r in csv.DictReader((PROC / "assembled_threads.csv").open(encoding="utf-8")):
        text = re.sub(r"https?://\S+", "", r["full_text"] or "").strip()
        units.append((r["created_at"], r["root_tweet_id"], bool(label.match(text))))
    from datetime import datetime
    units.sort()
    src_parent, prev_root, prev_time = {}, None, None
    for ts, rid, is_src in units:
        t = datetime.fromisoformat(ts)
        if is_src:
            if prev_root and (t - prev_time).total_seconds() <= 3600:
                src_parent[rid] = prev_root
        else:
            prev_root, prev_time = rid, t
    tweet_root = {tw: src_parent.get(rt, rt) for tw, rt in tweet_root.items()}
    print(f"Твітів «Source:» пришито до треду: {len(src_parent):,}")

    links = defaultdict(set)      # pm_id → {(url, via)}
    unmatched = Counter()
    for r in csv.DictReader((PROC / "link_scan.csv").open(encoding="utf-8")):
        if r["kind"] != "news":
            continue
        src, key = r["source"], (r["post_key"] or "").strip()
        pm_id = None
        if src in ("post_metrics",):
            pm_id = key
        elif src.startswith("x_"):
            pm_id = by_status.get(key) or by_status.get(tweet_root.get(key, ""))
        elif src.startswith("fb_"):
            pm_id = by_fb.get(fb_key(key))
        elif src.startswith("cp_"):
            pm_id = None       # CP-сторінки звʼязані з PM через relation — див. нижче
        if pm_id and pm_id in pm_rows:
            links[pm_id].add((r["final_url"], src))
        else:
            unmatched[src] += 1

    # CP → PM через relation
    cp_to_pm = defaultdict(list)
    for r in pm:
        for x in (r["properties"].get("Content Pulse item") or {}).get("relation") or []:
            cp_to_pm[x["id"]].append(r["id"])
    for r in csv.DictReader((PROC / "link_scan.csv").open(encoding="utf-8")):
        if r["kind"] == "news" and r["source"].startswith("cp_"):
            ids = cp_to_pm.get(r["post_key"], [])
            for pid in ids:
                links[pid].add((r["final_url"], r["source"]))
            if not ids:
                unmatched[r["source"]] += 1

    with (PROC / "post_article_links.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["pm_page_id", "year", "platform", "article_url", "via", "had_source_field"])
        for pid, s in links.items():
            meta = pm_rows[pid]
            for url, via in sorted(s):
                w.writerow([pid, meta["year"], meta["platform"], url, via, meta["has_source"]])

    # звіт по роках: пости з лінком на статтю — раніше (поле джерела) і тепер (будь-де)
    tot, before, now, new = Counter(), Counter(), Counter(), Counter()
    for pid, meta in pm_rows.items():
        y = meta["year"] or "—"
        tot[y] += 1
        vias = {v for _, v in links.get(pid, set())}
        if "post_metrics" in vias:
            before[y] += 1
        if vias:
            now[y] += 1
            if "post_metrics" not in vias:
                new[y] += 1
    print(f"{'рік':6}{'постів':>9}{'лінк був':>10}{'лінк тепер':>12}{'нових':>8}{'%':>6}")
    for y in sorted(tot):
        print(f"{y:6}{tot[y]:>9,}{before[y]:>10,}{now[y]:>12,}{new[y]:>8,}"
              f"{100 * now[y] / tot[y]:>5.0f}%")
    print(f"{'РАЗОМ':6}{sum(tot.values()):>9,}{sum(before.values()):>10,}"
          f"{sum(now.values()):>12,}{sum(new.values()):>8,}")
    print("\nНе привʼязались до Post Metrics (посилань):", dict(unmatched))


if __name__ == "__main__":
    main()
