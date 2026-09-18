"""Пари «стаття → пост» з явних посилань.

Звʼязок береться не здогадкою, а з посилання, яке поставив сам офіс:
  * поле `Source / Evidence (regex)` у Post Metrics — `link_method=source_field`;
  * посилання в тексті поста, зокрема окремий твіт «Source: <t.co>» після треду,
    знайдене й розгорнуте в `posts_db/scan_links.py` → `link_method=url_in_post`.

Пости беремо не лише з Post Metrics: 2 336 постів із посиланням є в архіві твітів,
а в Post Metrics їх немає. Текст такого поста — зібраний тред з архіву.

За замовчуванням лише пости з 2024 року та без дати: до 2024 статей майже не
цитували (2–3% постів), а стиль був інший.

Вихід:
    data/processed/training_pairs.csv        — пари з повним текстом обох сторін
    data/processed/training_pairs_todo.csv   — пари, де бракує тексту статті

Запуск:  python postgres/build_training_pairs.py [--since 2024]
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

ROOT = Path(__file__).parent.parent
PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw" / "notion"

csv.field_size_limit(10 ** 8)

SOCIAL = ("facebook", "x.com", "twitter", "t.me", "instagram",
          "youtube", "youtu.be", "tiktok")
OWN = ("kse.", "notion.")
MIN_ARTICLE_CHARS = 500
MIN_POST_CHARS = 80
# Верхні межі правдоподібності. Пост на 49 тис. знаків — це не пост, а вся сторінка
# Content Pulse: межа не спрацювала, бо на сторінці не було ні заголовка, ні маркера
# кінця треду. Такі рядки лишаємо у файлі, але позначаємо, щоб не вчитись на них.
MAX_POST_CHARS = {"X": 6_000, "FB": 15_000}
MAX_ARTICLE_CHARS = 50_000
TCO = re.compile(r"\s*https?://t\.co/\S+")


def norm_url(u: str) -> str:
    u = (u or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return re.sub(r"^https?://(www\.)?", "", u)


def is_news(host: str) -> bool:
    return not any(s in host for s in SOCIAL) and not any(o in host for o in OWN)


def main() -> None:
    args = sys.argv[1:]
    since = args[args.index("--since") + 1] if "--since" in args else "2024"

    print("Читаю Post Metrics…", flush=True)
    pm = E.query_post_metrics(E.load_token())

    # --- статті: база редакції + дотягнуте екстрактором
    articles, bodies = {}, {}
    for r in csv.DictReader((PROC / "core_article.csv").open(encoding="utf-8")):
        if r.get("url_canonical"):
            articles[norm_url(r["url_canonical"])] = r
    for line in (RAW / "article_bodies.jsonl").open(encoding="utf-8"):
        try:
            b = json.loads(line)
            bodies[b["notion_page_id"]] = b.get("full_text") or ""
        except Exception:
            pass
    fetched, fetched_verdict = {}, {}
    fpath = ROOT / "data" / "raw" / "fetched_articles.jsonl"
    if fpath.exists():
        for line in fpath.open(encoding="utf-8"):
            try:
                f = json.loads(line)
                fetched_verdict[norm_url(f["url"])] = f.get("verdict")
                if f.get("verdict") == "full" and f.get("text"):
                    fetched[norm_url(f["url"])] = f["text"]
            except Exception:
                pass
    # NewsCatcher v3: беремо лише там, де текст повний. У платних виданнях API віддає
    # початок статті: контроль проти повних копій редакції — NYT 27%, Telegraph 15%,
    # Economist 6%, Foreign Affairs 2%, а NYT ще й закінчується «Subscribe». Поле
    # word_count це не ловить — воно рахує слова вже обрізаного тексту.
    nc_preview = ("nytimes.com", "telegraph.co.uk", "thetimes.com", "ft.com",
                  "economist.com", "washingtonpost.com", "bloomberg.com", "wsj.com",
                  "foreignaffairs.com", "politico.eu", "lemonde.fr", "foxnews.com", "scmp.com")
    nc = {}
    ncpath = ROOT / "data" / "raw" / "newscatcher_v3.jsonl"
    if ncpath.exists():
        for line in ncpath.open(encoding="utf-8"):
            try:
                f = json.loads(line)
            except Exception:
                continue
            host = norm_url(f["requested"]).split("/")[0]
            if (f.get("found") and f.get("chars", 0) >= 1500 and not f.get("paid_content")
                    and not any(host == d or host.endswith("." + d) for d in nc_preview)
                    and "subscribe" not in (f.get("content") or "")[-300:].lower()):
                nc[norm_url(f["requested"])] = f["content"]
    reg = {}
    for r in csv.DictReader((ROOT / "posts_db" / "sources.csv").open(encoding="utf-8")):
        if r.get("domain"):
            reg[r["domain"].lower()] = r.get("extract_verdict") or ""

    def article_text(url_n: str):
        art = articles.get(url_n)
        text = bodies.get(art["notion_page_id"], "") if art else ""
        if len(text) >= MIN_ARTICLE_CHARS:
            return text, "notion_articles", art
        if url_n in fetched:
            return fetched[url_n], "own_extractor", art
        if url_n in nc:
            return nc[url_n], "newscatcher_v3", art
        return "", "", art

    # --- пости
    posts = {}         # key → dict
    pm_status = {}
    for r in pm:
        p = r["properties"]
        url = (p.get("Post URL") or {}).get("url") or ""
        m = re.search(r"/status/(\d+)", url)
        if m:
            pm_status[m.group(1)] = r["id"]
        posts["PM:" + r["id"]] = {
            "pm_page_id": r["id"],
            "platform": ((p.get("Platform") or {}).get("select") or {}).get("name") or "",
            "post_date": ((p.get("Post date") or {}).get("date") or {}).get("start") or "",
            "post_url": url,
            "text": E.rt(p, E.F_TEXT),
            "text_src": "notion_post_metrics",
            "source_field": E.rt(p, E.F_SOURCE),
            "status": m.group(1) if m else "",
        }
    threads = {}
    for r in csv.DictReader((PROC / "assembled_threads.csv").open(encoding="utf-8")):
        threads[r["root_tweet_id"]] = r
    for key, post in posts.items():
        # у Post Metrics тексту немає, а в архіві тред є — беремо з архіву
        if len(post["text"]) < MIN_POST_CHARS and post["status"] in threads:
            post["text"] = TCO.sub("", threads[post["status"]]["full_text"] or "").strip()
            post["text_src"] = "x_archive"

    # --- посилання: знайдені скрізь + поле джерела
    links = json.load((PROC / "all_post_article_links.json").open(encoding="utf-8"))
    for key, post in posts.items():
        m = E.PLAIN_URL.search(post["source_field"] or "")
        if m:
            u = m.group(0).rstrip(".,;:")
            host = re.sub(r"^https?://(www\.)?", "", u).split("/")[0].lower()
            if is_news(host):
                links.setdefault(key, [])
                if norm_url(u) not in links[key]:
                    links[key].append(norm_url(u))
    for key in list(links):
        if key.startswith("X:") and key not in posts:
            rid = key[2:]
            t = threads.get(rid)
            if not t:
                continue
            posts[key] = {
                "pm_page_id": "", "platform": "X",
                "post_date": t["created_at"], "post_url": f"https://x.com/Mylovanov/status/{rid}",
                "text": TCO.sub("", t["full_text"] or "").strip(), "text_src": "x_archive",
                "source_field": "", "status": rid,
            }

    pairs, todo, stats = [], [], Counter()
    for key, urls in links.items():
        post = posts.get(key)
        if not post:
            stats["пост_не_знайдено"] += 1
            continue
        year = (post["post_date"] or "")[:4]
        if year and year < since:
            stats[f"до_{since}"] += 1
            continue
        src_n = norm_url(E.PLAIN_URL.search(post["source_field"]).group(0)) \
            if E.PLAIN_URL.search(post["source_field"] or "") else ""
        for url_n in urls:
            host = url_n.split("/")[0]
            row = {
                "pm_page_id": post["pm_page_id"],
                "post_key": key,
                "platform": post["platform"],
                "post_date": post["post_date"],
                "post_url": post["post_url"],
                "post_chars": len(post["text"]),
                "post_text_src": post["text_src"],
                "article_url": "https://" + url_n,
                "article_domain": host,
                "link_method": "source_field" if url_n == src_n else "url_in_post",
            }
            if len(post["text"]) < MIN_POST_CHARS:
                stats["немає_тексту_поста"] += 1
                todo.append({**row, "missing": "post_text"})
                continue
            text, text_src, art = article_text(url_n)
            if not text:
                v = fetched_verdict.get(url_n) or reg.get(host) or "untested"
                stats[f"немає_статті:{v}"] += 1
                todo.append({**row, "missing": f"article:{v}"})
                continue
            flags = []
            # Тред з архіву, що починається не з 1/: в архіві немає поля «відповідь на»,
            # а офіс публікує кілька тредів одночасно, тож за часом і нумерацією початок
            # не відновлюється однозначно. Звʼязок зі статтею правильний, текст — ні.
            if post["text_src"] == "x_archive":
                t = threads.get(post["status"]) or {}
                fn = t.get("first_num") or ""
                if fn not in ("", "nan") and float(fn) != 1:
                    flags.append("post_fragment")
            if len(post["text"]) > MAX_POST_CHARS.get(post["platform"] or "X", 15_000):
                flags.append("post_too_long")
            if len(text) > MAX_ARTICLE_CHARS:
                flags.append("article_too_long")
            if flags:
                stats["позначено_на_перевірку"] += 1
            stats["готова_пара"] += 1
            pairs.append({**row,
                          "article_notion_id": art["notion_page_id"] if art else "",
                          "article_title": (art or {}).get("title", ""),
                          "article_chars": len(text),
                          "article_text_src": text_src,
                          "quality_flag": ";".join(flags),
                          "post_text": post["text"],
                          "article_text": text})

    cols = ["pm_page_id", "post_key", "platform", "post_date", "post_url", "post_chars",
            "post_text_src", "article_url", "article_domain", "article_notion_id",
            "article_title", "article_chars", "article_text_src", "link_method",
            "quality_flag", "post_text", "article_text"]
    with (PROC / "training_pairs.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(pairs)
    tcols = ["pm_page_id", "post_key", "platform", "post_date", "post_url", "post_chars",
             "article_url", "article_domain", "link_method", "missing"]
    with (PROC / "training_pairs_todo.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=tcols, extrasaction="ignore")
        w.writeheader()
        w.writerows(todo)

    for k, v in stats.most_common():
        print(f"  {k:36} {v:,}")
    print(f"\n  готових пар: {len(pairs):,}")
    for label, fld in (("платформа", "platform"), ("звідки текст поста", "post_text_src"),
                       ("звідки текст статті", "article_text_src"),
                       ("як знайдено звʼязок", "link_method")):
        print(f"  {label}: {dict(Counter(p[fld] for p in pairs))}")
    print(f"\n  → {PROC / 'training_pairs.csv'}")


if __name__ == "__main__":
    main()
