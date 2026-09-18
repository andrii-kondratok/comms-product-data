"""Збагачення Post Metrics: `Post text` і `Source / Evidence (regex)`.

Джерело правди — сторінка Content Pulse, звʼязана через relation `Content Pulse item`.
Один рядок Post Metrics = один опублікований пост; X-тред `1/ … 7X` лишається цілим.

Чому не по `body_for_data_analysis`: ця властивість у 5 830 з 5 995 рядків
позбавлена пунктуації і посилань («https www youtube com …»), тобто ні для тексту
поста, ні для витягу URL непридатна. Тіло читаємо з блоків сторінки.

Межі поста визначаємо не лише розміткою: усі рядки Post Metrics, звʼязані з тією
самою сторінкою, спершу шукають свій початок у тілі, потім кожен пост ріжеться до
початку наступного сусіда. Це закриває обидва регресійні кейси — два треди на одній
сторінці (McFaul) і тред, що починається не з 1/.

Ідемпотентно: непорожні поля не чіпаються, якщо не задано --overwrite.

Запуск:
    python postgres/enrich_post_metrics.py                 # сухий прогін
    python postgres/enrich_post_metrics.py --sample 40     # сухий на вибірці
    python postgres/enrich_post_metrics.py --apply
    python postgres/enrich_post_metrics.py --apply --overwrite
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).parent))
from export_notion import NOTION_VERSION, load_token, post_json  # noqa: E402
from notion_body import page_markdown  # noqa: E402

PM_DB = "570dde03-224a-4f17-8fd3-bbab048e8ca0"
F_TEXT = "Post text"
F_SOURCE = "Source / Evidence (regex)"
F_REL = "Content Pulse item"

MIN_RATIO = 0.80
SOURCE_WINDOW = 400
RATE_DELAY = 0.34
REPORT = Path(__file__).parent.parent / "data" / "processed" / "post_metrics_enrichment.csv"
CACHE = Path(__file__).parent.parent / "data" / "raw" / "notion" / "content_pulse_bodies.jsonl"

# --- розмітка -------------------------------------------------------------

# Заголовок секції платформи. Редактори пишуть по-різному: «X», «## X posted»,
# «Facebook (reels)», «**Twitter**». Довжину обмежуємо, щоб не зʼїсти рядок поста,
# який випадково починається з «X …».
PLATFORM_LINE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*{1,2}|_{1,2})?\s*"
    r"(X|Х|Twitter|Facebook|FB)"
    r"(?:\s*/\s*(?:X|Х|Twitter|Facebook|FB))?"
    r"(?:[ \t]+(?:posted|post|version|текст))?"
    r"(?:[ \t]*\([^)]{0,20}\))?"
    r"\s*(?:\*{1,2}|_{1,2})?\s*:?\s*$", re.I)

EMPTY_HEADING = re.compile(r"^#{1,6}\s*$")

POST_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{1,2}|_{1,2})?"
    r"(?:(?:Twitter|Facebook|FB|X)[ \t]+)?"
    r"(?:Post|Пост)(?:[ \t]*#?[ \t]*\d+)?"
    r"[ \t]*(?:\*{1,2}|_{1,2})?[ \t]*[:.,\-–—]?[ \t]*$", re.I)

# Хвостовий маркер твіта: «1/», «6X», «7 X».
MARKER = re.compile(r"(\d{1,2})\s*([/Xх])\s*$", re.I)

NOISE_HEADING = re.compile(
    r"^#{1,6}\s*(video|photo|image|media|gif|photo for donation|відео|фото)\s*$", re.I)
BOILERPLATE = re.compile(
    r"(Thank you for reading this post|Дякуємо, що прочитали).*", re.S | re.I)

SOURCE_LABEL = re.compile(
    r"\b(?:source|sources|джерело|джерела|посилання|link)\b\s*:", re.I)
MARKDOWN_URL = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)", re.I)
PLAIN_URL = re.compile(r"https?://[^\s<>\]\)]+", re.I)
# Донат-футер і власні лендінги — це не джерело поста.
SKIP_HOSTS = ("foundation.kse.ua", "forms.kse.ua", "mba.kse.ua")
# Без явної позначки `Source:` беремо лише зовнішні посилання: сам пост у соцмережі
# та згадки профілів джерелом не є.
SKIP_HOSTS_UNLABELED = SKIP_HOSTS + (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "t.me", "linkedin.com",
    # власні сайти й реєстрації на події — це промо, а не джерело коментаря
    "kse.ua", "kse.org.ua", "notion.site",
    "docs.google.com", "drive.google.com", "forms.gle", "trippus.net")

PLATFORM_ALIAS = {"twitter": "X", "x": "X", "х": "X", "fb": "FB", "facebook": "FB"}


def clean_body(md: str) -> str:
    """Прибрати службові заголовки тоглів, роздільники й донат-футер."""
    md = BOILERPLATE.sub("", md)
    md = re.sub(r"<br\s*/?>", "\n", md, flags=re.I)
    md = re.sub(r"</?span[^>]*>", "", md, flags=re.I)
    lines = []
    for line in md.split("\n"):
        s = line.strip()
        if not s or s == "---" or NOISE_HEADING.match(s) or EMPTY_HEADING.match(s):
            continue
        if any(h in s for h in SKIP_HOSTS) and len(s) < 200:
            continue
        lines.append(line.rstrip())
    return "\n".join(lines)


def split_platforms(body: str) -> dict:
    """Секції платформ. До першого заголовка текст вважаємо X — так на сторінках."""
    out = defaultdict(list)
    current = "X"
    for line in body.split("\n"):
        m = PLATFORM_LINE.match(line) if len(line.strip()) <= 40 else None
        if m:
            current = PLATFORM_ALIAS[m.group(1).lower()]
            continue
        out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items() if "".join(v).strip()}


def candidate_starts(section: str) -> list:
    """Зміщення, з яких може починатись пост.

    Розмітка ненадійна: заголовок `Post N` є лише на частині сторінок, а межа треду
    часто позначена тільки хвостовим `6X`. Тому кандидатом вважаємо початок будь-якого
    непорожнього рядка — схожість із `Name` відсіює зайве сама. Порядок у сотні
    кандидатів на сторінку, SequenceMatcher на 150 знаках, тож це дешево.
    """
    starts, offset = [], 0
    for line in section.split("\n"):
        if line.strip():
            starts.append(offset)
        offset += len(line) + 1
    return starts or [0]


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)       # [текст](url) → текст
    text = re.sub(r"\b\d+\s*[x/]\s*$", "", text, flags=re.M)
    return re.sub(r"[^\wа-яіїєґ]+", " ", text, flags=re.I).strip()


def similarity(name: str, candidate: str) -> float:
    left, right = normalize(name), normalize(candidate)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right[:len(left)]).ratio()


def best_start(name: str, section: str):
    best, ratio = -1, 0.0
    for off in candidate_starts(section):
        r = similarity(name, section[off:off + len(name) * 3 + 200])
        if r > ratio:
            best, ratio = off, r
    return best, ratio


ANY_HEADING = re.compile(r"^#{1,6}\s+\S")


def trim_to_thread_end(chunk: str) -> str:
    """Обрізати по закривному маркеру треду (`6X`) або по наступному заголовку.

    Другий випадок — редакційні тогли на кшталт `### info`, де лежать чернетки й
    нотатки. Без цього один пост ловив усю сторінку: найдовший був 28 000 знаків,
    із яких текстом поста були перші 2 000.
    """
    lines, cut = chunk.split("\n"), None
    for i, line in enumerate(lines):
        s = line.strip()
        m = MARKER.search(s)
        if m and m.group(2).lower() in ("x", "х"):
            cut = i + 1
            break
        if i > 0 and ANY_HEADING.match(s) and not POST_HEADING.match(s):
            cut = i
            break
    return "\n".join(lines[:cut]).strip() if cut else chunk.strip()


def extract_labeled_url(md: str) -> str:
    for label in SOURCE_LABEL.finditer(md or ""):
        tail = md[label.end():label.end() + SOURCE_WINDOW]
        matches = []
        m1 = MARKDOWN_URL.search(tail)
        m2 = PLAIN_URL.search(tail)
        if m1:
            matches.append((m1.start(), m1.group(1)))
        if m2:
            matches.append((m2.start(), m2.group(0)))
        for _, url in sorted(matches):
            url = url.replace("\\_", "_").rstrip(".,;:")
            if not any(h in url for h in SKIP_HOSTS):
                return url
    return ""


def first_external_url(md: str) -> str:
    """Запасний варіант, коли позначки `Source:` немає: перше зовнішнє посилання."""
    found = [(m.start(), m.group(1)) for m in MARKDOWN_URL.finditer(md or "")]
    found += [(m.start(), m.group(0)) for m in PLAIN_URL.finditer(md or "")]
    for _, url in sorted(found):
        url = url.replace("\\_", "_").rstrip(".,;:")
        if not any(h in url for h in SKIP_HOSTS_UNLABELED):
            return url
    return ""


def pick_source(chunk: str, section: str, body: str, fallback: str = "") -> tuple:
    """URL джерела і спосіб, яким він знайдений — спосіб пишемо у звіт, не в Notion."""
    for scope, name in ((chunk, "label_post"), (section, "label_section"), (body, "label_page")):
        url = extract_labeled_url(scope)
        if url:
            return url, name
    if fallback:
        return fallback, "cp_property"
    url = first_external_url(chunk) or first_external_url(section)
    return (url, "unlabeled") if url else ("", "")


def strip_source_line(chunk: str) -> str:
    """`Source: …` — редакційна помітка, у тексті поста їй не місце."""
    return "\n".join(l for l in chunk.split("\n")
                     if not SOURCE_LABEL.match(l.strip())).strip()


# --- Notion ---------------------------------------------------------------

def rt(props: dict, field: str) -> str:
    v = props.get(field) or {}
    return "".join(t.get("plain_text", "") for t in v.get("rich_text") or []).strip()


def title_of(props: dict, field: str = "Name") -> str:
    v = props.get(field) or {}
    return "".join(t.get("plain_text", "") for t in v.get("title") or []).strip()


def chunks(text: str, size: int = 1900) -> list:
    return [{"text": {"content": text[i:i + size]}}
            for i in range(0, len(text), size)] or [{"text": {"content": ""}}]


def patch_page(page_id: str, token: str, props: dict) -> None:
    payload = json.dumps({"properties": props}).encode("utf-8")
    for attempt in range(5):
        req = request.Request(
            f"https://api.notion.com/v1/pages/{page_id}", data=payload, method="PATCH",
            headers={"Authorization": f"Bearer {token}",
                     "Notion-Version": NOTION_VERSION,
                     "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=60) as resp:
                resp.read()
                return
        except error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(float(e.headers.get("Retry-After", 2 ** attempt)))
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}")
        except error.URLError:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise


def has_pending(token: str) -> bool:
    """Дешева перевірка, чи взагалі є що робити: один запит замість 155.

    Потрібна для запуску за розкладом — більшість запусків нічого не знаходить,
    а повне читання бази коштує ~хвилину і 155 звернень до API.
    """
    d = post_json(f"https://api.notion.com/v1/databases/{PM_DB}/query", token, {
        "page_size": 1,
        "filter": {"and": [
            # без звʼязку з Content Pulse брати текст нема звідки — таких 7 141
            {"property": F_REL, "relation": {"is_not_empty": True}},
            {"or": [
                {"property": F_TEXT, "rich_text": {"is_empty": True}},
                {"property": F_SOURCE, "rich_text": {"is_empty": True}},
            ]},
        ]},
    })
    return bool(d.get("results"))


def query_post_metrics(token: str) -> list:
    url = f"https://api.notion.com/v1/databases/{PM_DB}/query"
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        d = post_json(url, token, body)
        rows.extend(d.get("results", []))
        if not d.get("has_more"):
            return rows
        cursor = d["next_cursor"]
        time.sleep(RATE_DELAY)


def load_cp_source_property() -> dict:
    """`Source / Evidence` зі знімка Content Pulse — запасний варіант, коли в тілі
    сторінки посилання немає. Знімок може бути несвіжим, тож лише як fallback."""
    snaps = sorted((CACHE.parent).glob("content_pulse__*.jsonl"))
    if not snaps:
        return {}
    out = {}
    for line in snaps[-1].open(encoding="utf-8"):
        try:
            r = json.loads(line)
            v = (r["properties"].get("Source / Evidence") or {}).get("rich_text") or []
            txt = "".join(x.get("plain_text", "") for x in v)
            m = PLAIN_URL.search(txt)
            if m and not any(h in m.group(0) for h in SKIP_HOSTS):
                out[r["notion_page_id"]] = m.group(0).rstrip(".,;:")
        except Exception:
            pass
    return out


def load_cache() -> dict:
    if not CACHE.exists():
        return {}
    out = {}
    for line in CACHE.open(encoding="utf-8"):
        try:
            r = json.loads(line)
            out[r["page_id"]] = r["markdown"]
        except Exception:
            pass
    return out


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args
    overwrite = "--overwrite" in args
    sample = int(args[args.index("--sample") + 1]) if "--sample" in args else None

    token = load_token()
    if not overwrite and not has_pending(token):
        print("Порожніх полів немає — нічого робити.")
        return
    print("Читаю Post Metrics…", flush=True)
    rows = query_post_metrics(token)
    print(f"  рядків: {len(rows):,}")

    todo = []
    for r in rows:
        p = r["properties"]
        rel = [x["id"] for x in (p.get(F_REL) or {}).get("relation") or []]
        if not rel:
            continue
        need_text = overwrite or not rt(p, F_TEXT)
        need_src = overwrite or not rt(p, F_SOURCE)
        todo.append({
            "page_id": r["id"], "cp": rel[0],
            "name": title_of(p),
            "platform": ((p.get("Platform") or {}).get("select") or {}).get("name") or "X",
            "need_text": need_text, "need_src": need_src,
            "write": need_text or need_src,
            "post_url": (p.get("Post URL") or {}).get("url") or "",
        })

    # У групу беремо ВСІ рядки сторінки, навіть уже заповнені: межа поста — це
    # початок наступного сусіда, і без заповнених сусідів текст «протік» би далі.
    by_page = defaultdict(list)
    for t in todo:
        by_page[t["cp"]].append(t)
    pages = sorted(cp for cp, g in by_page.items() if any(t["write"] for t in g))
    if sample:
        pages = pages[:sample]
    n_write = sum(1 for p in pages for t in by_page[p] if t["write"])
    print(f"  до обробки: {n_write:,} рядків на {len(pages):,} сторінках Content Pulse "
          f"(разом із сусідами — {sum(len(by_page[p]) for p in pages):,})")

    cache = load_cache()
    cp_source = load_cp_source_property()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    stats = defaultdict(int)
    report = []
    started = time.time()
    cache_fh = CACHE.open("a", encoding="utf-8")

    for n, cp in enumerate(pages, 1):
        group = by_page[cp]
        try:
            md = cache.get(cp)
            if md is None:
                md = page_markdown(cp, token)
                cache[cp] = md
                cache_fh.write(json.dumps(
                    {"page_id": cp, "markdown": md,
                     "fetched_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False) + "\n")
                cache_fh.flush()
                time.sleep(RATE_DELAY)
        except Exception as e:                                   # noqa: BLE001
            for t in group:
                stats["error_fetch"] += 1
                report.append({**t, "outcome": "error_fetch", "ratio": "",
                               "detail": str(e)[:120]})
            continue

        body = clean_body(md)
        sections = split_platforms(body)
        if not sections:
            for t in group:
                stats["empty_body"] += 1
                report.append({**t, "outcome": "empty_body", "ratio": "", "detail": ""})
            continue

        # 1. кожен рядок шукає свій початок у своїй, а потім у будь-якій секції
        placed = []
        for t in group:
            want = PLATFORM_ALIAS.get((t["platform"] or "X").lower(), "X")
            order = ([want] if want in sections else []) + \
                    [s for s in sections if s != want]
            found = (None, -1, 0.0)
            for sec in order:
                off, ratio = best_start(t["name"], sections[sec])
                if ratio > found[2]:
                    found = (sec, off, ratio)
                if ratio >= MIN_RATIO and sec == want:
                    break
            placed.append((t, found[0], found[1], found[2]))

        # 2. межі: пост іде до початку наступного сусіда в тій самій секції
        by_sec = defaultdict(list)
        for x in placed:
            if x[3] >= MIN_RATIO:
                by_sec[x[1]].append(x)
        # Два рядки можуть вказати на той самий початок — наприклад, коли на сторінці
        # лежить один текст, а рядків Post Metrics на нього два. Тоді першому межа
        # ставилась на його ж початок, і текст виходив порожній. Лишаємо той рядок,
        # що збігся краще; решту не пишемо взагалі, бо це неоднозначність, а не факт.
        ends, ambiguous = {}, set()
        for sec, items in by_sec.items():
            items.sort(key=lambda x: (x[2], -x[3]))
            uniq = []
            for x in items:
                if uniq and uniq[-1][2] == x[2]:
                    ambiguous.add(x[0]["page_id"])
                    continue
                uniq.append(x)
            for i, x in enumerate(uniq):
                ends[x[0]["page_id"]] = (uniq[i + 1][2] if i + 1 < len(uniq)
                                         else len(sections[sec]))

        for t, sec, off, ratio in placed:
            if not t["write"]:          # сусід, потрібен лише для межі
                continue
            if ratio < MIN_RATIO:
                stats["low_match"] += 1
                report.append({**t, "outcome": "low_match",
                               "ratio": f"{ratio:.2f}", "detail": ""})
                continue

            if t["page_id"] in ambiguous:
                stats["ambiguous_start"] += 1
                report.append({**t, "outcome": "ambiguous_start",
                               "ratio": f"{ratio:.2f}", "detail": ""})
                continue

            chunk = trim_to_thread_end(sections[sec][off:ends[t["page_id"]]])
            text = strip_source_line(chunk)

            # Порожній шматок — це не «пост без тексту», а невдалий розбір.
            # Писати джерело для такого рядка не можна: пара виглядатиме готовою,
            # а тексту поста в ній не буде.
            if t["need_text"] and not text:
                stats["empty_chunk"] += 1
                report.append({**t, "outcome": "empty_chunk",
                               "ratio": f"{ratio:.2f}", "detail": ""})
                continue

            props, detail = {}, ""
            if t["need_text"] and text:
                props[F_TEXT] = {"rich_text": chunks(text)}
            if t["need_src"]:
                url, how = pick_source(chunk, sections[sec], body, cp_source.get(cp, ""))
                if url:
                    props[F_SOURCE] = {"rich_text": chunks(url)}
                    detail = url
                    stats["src_" + how] += 1
                else:
                    stats["no_source"] += 1

            if not props:
                stats["nothing_to_write"] += 1
                report.append({**t, "outcome": "nothing_to_write",
                               "ratio": f"{ratio:.2f}", "detail": detail})
                continue

            if apply:
                try:
                    patch_page(t["page_id"], token, props)
                    time.sleep(RATE_DELAY)
                except Exception as e:                           # noqa: BLE001
                    stats["error_write"] += 1
                    report.append({**t, "outcome": "error_write", "ratio": f"{ratio:.2f}",
                                   "detail": str(e)[:120]})
                    continue

            stats["updated" if apply else "would_update"] += 1
            if F_TEXT in props:
                stats["text_written"] += 1
            if F_SOURCE in props:
                stats["source_written"] += 1
            report.append({**t, "outcome": "updated" if apply else "would_update",
                           "ratio": f"{ratio:.2f}", "detail": detail, "chars": len(text)})

        if n % 25 == 0 or n == len(pages):
            el = time.time() - started
            eta = el / n * (len(pages) - n) / 60
            print(f"  {n:>5,}/{len(pages):,} сторінок · "
                  f"{stats['updated'] + stats['would_update']:,} рядків · "
                  f"low_match {stats['low_match']:,} · ~{eta:.0f} хв лишилось", flush=True)

    cache_fh.close()

    cols = ["page_id", "cp", "name", "platform", "need_text", "need_src",
            "post_url", "outcome", "ratio", "detail", "chars"]
    with REPORT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in report:
            w.writerow(r)

    print(f"\n{'РЕЖИМ ЗАПИСУ' if apply else 'СУХИЙ ПРОГІН — нічого не записано'}")
    for k in sorted(stats):
        print(f"  {k:20} {stats[k]:,}")
    print(f"  звіт → {REPORT}")


if __name__ == "__main__":
    main()
