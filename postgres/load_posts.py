"""Розбір знімків Notion у нормалізовані таблиці постів.

Читає JSONL із data/raw/notion, зшиває з локальними експортами в posts_db/posts.duckdb
і будує `post_master` — по рядку на опублікований пост, з найповнішим доступним текстом.

Пріоритет тексту (перемагає найповніший, походження записується явно):
    1. post_metrics."Post text"            — повний тред, 2025–2026
    2. content_pulse.body_for_data_analysis — повний текст драфта
    3. локальний post.hook_raw              — перший твіт / допис FB, 2022–2026
    4. post_metrics."Name"                  — обрізок на 200 знаків, крайній засіб

Запуск:  python postgres/load_posts.py
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw" / "notion"
DB = ROOT / "posts_db" / "posts.duckdb"
OUT = ROOT / "data" / "processed"

X_URL = re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)", re.I)
FB_STORY = re.compile(r"story_fbid=([A-Za-z0-9]+)", re.I)
FB_PERMA = re.compile(r"facebook\.com/(?:\d+|[^/]+)/posts/([A-Za-z0-9]+)", re.I)

# Маркери нумерації треду шукаємо ПОРЯДКОВО, а не по краях усього тексту.
# У зібраному треді «1/» стоїть у кінці кожного твіта, тож перевірка кінця
# рядка знаходить їх усі, а перевірка кінця всього тексту — жодного.
LEAD_NUM = re.compile(r"^[ \t]*\d{1,2}[ \t]*/[ \t]*", re.M)
TRAIL_NUM = re.compile(r"[ \t]*\d{1,2}[ \t]*/[ \t]*$", re.M)


# ------------------------------------------------------------------ помічники

def latest(pattern: str) -> Path:
    files = sorted(glob.glob(str(RAW / f"{pattern}__*.jsonl")))
    if not files:
        raise SystemExit(f"Немає знімка {pattern} у {RAW}. Спершу export_notion.py")
    return Path(files[-1])


def plain(prop: dict | None) -> str | None:
    """Витягує текст із rich_text/title, склеюючи всі фрагменти."""
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("rich_text", "title") and v:
        s = "".join(x.get("plain_text", "") for x in v)
        return s or None
    return None


def scalar(prop: dict | None):
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "multi_select":
        return [x.get("name") for x in (v or [])] or None
    if t == "date":
        return (v or {}).get("start")
    if t == "relation":
        return [x.get("id") for x in (v or []) if x] or None
    if t == "unique_id":
        return (v or {}).get("number")
    return v


def native_id(url: str | None) -> tuple[str | None, str | None]:
    """Повертає (платформа, native_id)."""
    if not isinstance(url, str):
        return None, None
    for pat, plat in ((X_URL, "X"), (FB_STORY, "FB"), (FB_PERMA, "FB")):
        m = pat.search(url)
        if m:
            return plat, m.group(1)
    low = url.lower()
    if "x.com" in low or "twitter.com" in low:
        return "X", None
    if "facebook.com" in low:
        return "FB", None
    return None, None


def numbering_counts(text: str | None) -> tuple[int, int]:
    """Скільки маркерів нумерації на початку і в кінці рядків."""
    if not isinstance(text, str) or not text:
        return 0, 0
    return len(LEAD_NUM.findall(text)), len(TRAIL_NUM.findall(text))


def numbering_style(text: str | None) -> str:
    lead, trail = numbering_counts(text)
    if trail and trail >= lead:
        return "trailing"
    if lead:
        return "leading"
    return "none"


def clean(text: str | None) -> str | None:
    """Текст без маркерів нумерації — саме він іде у fine-tune.

    Знімає маркери в кінці й на початку КОЖНОГО рядка, інакше в зібраному
    треді всередині лишаються «1/», «2/», і модель вчиться їх відтворювати.
    """
    if not isinstance(text, str) or not text:
        return None
    out = TRAIL_NUM.sub("", text)
    out = LEAD_NUM.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() or None


# ------------------------------------------------------------------ вибір тексту

# Порядок лише для розв'язання нічиїх. Головний критерій — повнота тексту.
# Для FB локальний експорт іде першим свідомо: це реально опублікований допис,
# тоді як «Post text» у Notion для FB-рядків подекуди містить англомовний
# варіант із Content Pulse, тобто інший текст, а не повніший.
SOURCE_ORDER = {
    "local_export": 0,
    "notion_post_metrics": 1,
    "notion_content_pulse": 2,
    "notion_title_truncated": 9,
}

WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return WS.sub(" ", s).strip().lower()


def pick_text(row) -> pd.Series:
    """Обирає найповніший текст серед доступних варіантів.

    Фіксований пріоритет джерел був помилкою: він віддавав Notion навіть тоді,
    коли локальний експорт містив довший текст (881 такий випадок на FB).
    Але й «просто найдовший» не годиться — різні джерела подекуди тримають
    різні мовні версії того самого допису, і довший не означає правильніший.

    Тому: варіант, який є підрядком іншого, відкидаємо як обрізок; серед решти
    беремо найдовший. Якщо лишилося більше одного — це різний контент,
    а не обрізання, і ми позначаємо це прапорцем замість тихого вибору.
    """
    def take(col: str, origin: str) -> tuple[str, str] | None:
        v = row.get(col)
        return (v, origin) if isinstance(v, str) and v.strip() else None

    # Рівень 1 — опубліковане: зіскоблене з самої платформи.
    published = [c for c in (take("metrics_text", "notion_post_metrics"),
                             take("hook_raw", "local_export")) if c]
    # Рівень 2 — драфт із Content Pulse: те, що написали ДО правок при публікації.
    draft = take("body", "notion_content_pulse")
    stub = take("title_text", "notion_title_truncated")

    def longest_distinct(pool: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Відкидає варіанти, що цілком містяться в іншому — це обрізки."""
        out = [c for c in pool
               if not any(_norm(c[0]) != _norm(o) and _norm(c[0]) in _norm(o)
                          for o, _ in pool)]
        out = out or pool
        out.sort(key=lambda c: (-len(c[0]), SOURCE_ORDER.get(c[1], 5)))
        return out

    survivors = longest_distinct(published) if published else []
    n_cands = len(published) + (1 if draft else 0)

    if survivors:
        best_text, best_origin = survivors[0]
        # Якщо опубліковане цілком міститься в драфті — це наш захват обрізаний,
        # а не пост короткий. Тоді драфт відновлює втрачене.
        if draft and _norm(best_text) != _norm(draft[0]) \
                and _norm(best_text) in _norm(draft[0]):
            return pd.Series([draft[0], "content_pulse_repair", n_cands, False])
        return pd.Series([best_text, best_origin, n_cands, len(survivors) > 1])

    if draft:
        return pd.Series([draft[0], draft[1], n_cands, False])
    if stub:
        return pd.Series([stub[0], stub[1], 1, False])
    return pd.Series([None, None, 0, False])


# ------------------------------------------------------------------ розбір

def read_post_metrics() -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, metrics = [], []
    for line in latest("post_metrics").open(encoding="utf-8"):
        r = json.loads(line)
        p = r["properties"]
        url = scalar(p.get("Post URL"))
        plat_url, nid = native_id(url)
        plat = scalar(p.get("Platform")) or plat_url
        pulse = scalar(p.get("Content Pulse item"))

        rows.append({
            "notion_page_id": r["notion_page_id"],
            "platform": plat,
            "native_id": nid,
            "url": url,
            "posted_at": scalar(p.get("Post date")),
            "metrics_text": plain(p.get("Post text")),
            "title_text": plain(p.get("Name")),
            "category": scalar(p.get("Category")),
            "strategic_goal": scalar(p.get("Strategic Goal")),
            "pulse_page_id": pulse[0] if pulse else None,
            "archived": r.get("archived"),
        })

        observed = scalar(p.get("Metrics updated")) or r.get("last_edited_time")
        for label, metric in (("Views", "views"), ("Likes", "likes"),
                              ("Comments", "comments"), ("Shares", "shares")):
            val = scalar(p.get(label))
            if val is not None:
                metrics.append({
                    "notion_page_id": r["notion_page_id"],
                    "metric": metric, "value": float(val),
                    "observed_at": observed, "source_system": "notion_post_metrics",
                })
    return pd.DataFrame(rows), pd.DataFrame(metrics)


def read_content_pulse() -> pd.DataFrame:
    rows = []
    for line in latest("content_pulse").open(encoding="utf-8"):
        r = json.loads(line)
        p = r["properties"]
        title_prop = next((v for k, v in p.items() if v.get("type") == "title"), None)
        rows.append({
            "pulse_page_id": r["notion_page_id"],
            "pulse_id": scalar(p.get("ID")),
            "title": plain(title_prop),
            "body": plain(p.get("body_for_data_analysis")),
            "short_text": plain(p.get("Text")),
            "status": scalar(p.get("Status")),
            "channel": scalar(p.get("Channel")),
            "post_type": scalar(p.get("Post Type")),
            "strategic_goal": scalar(p.get("Strategic Goal")),
            "date": scalar(p.get("Date")),
            "post_url": scalar(p.get("Post URL")),
            "tm_approved": scalar(p.get("TM approved")),
            "source_article": scalar(p.get("Source Article 1")),
            "created_time": r.get("created_time"),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ збірка

def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pm, metrics = read_post_metrics()
    cp = read_content_pulse()
    print(f"Розібрано: post_metrics {len(pm):,} · content_pulse {len(cp):,} · "
          f"метрик {len(metrics):,}")

    # Текст із Content Pulse підтягуємо через relation, а не через здогадки
    merged = pm.merge(cp[["pulse_page_id", "body", "status", "post_type", "pulse_id"]],
                      on="pulse_page_id", how="left")

    con = duckdb.connect(str(DB))
    local = con.execute("""
        SELECT platform, native_id, hook_raw, hook_clean, posted_at AS local_posted_at,
               url_canonical AS local_url
        FROM post WHERE native_id IS NOT NULL
    """).df()
    print(f"Локальних постів у posts.duckdb: {len(local):,}")

    merged = merged.merge(local, on=["platform", "native_id"], how="outer",
                          suffixes=("", "_local"))

    merged[["text_best", "text_origin", "text_candidates",
            "text_conflict"]] = merged.apply(pick_text, axis=1)
    # apply з мішаними типами віддає arrow-backed колонку, на якій .map падає
    merged["text_best"] = merged["text_best"].astype("object")
    merged["text_origin"] = merged["text_origin"].astype("object")
    merged["text_chars"] = merged["text_best"].str.len()
    merged["numbering_style"] = merged["text_best"].map(numbering_style)
    counts = merged["text_best"].map(numbering_counts)
    merged["numbering_lead_n"] = counts.map(lambda c: c[0])
    merged["numbering_trail_n"] = counts.map(lambda c: c[1])
    merged["text_clean"] = merged["text_best"].map(clean)
    merged["posted_at"] = merged["posted_at"].fillna(merged.get("local_posted_at"))
    merged["url"] = merged["url"].fillna(merged.get("local_url"))
    # Обрізаний заголовок — не текст. Позначаємо, щоб не потрапив у навчання.
    merged["is_truncated"] = merged["text_origin"].eq("notion_title_truncated")

    cols = ["notion_page_id", "pulse_page_id", "pulse_id", "platform", "native_id",
            "url", "posted_at", "text_best", "text_clean", "text_origin", "text_chars",
            "numbering_style", "numbering_lead_n", "numbering_trail_n", "is_truncated",
            "text_candidates", "text_conflict", "category", "post_type", "status",
            "strategic_goal", "archived"]
    master = merged[[c for c in cols if c in merged.columns]].copy()
    master["strategic_goal"] = master["strategic_goal"].map(
        lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else None)

    con.execute("DROP TABLE IF EXISTS post_master")
    con.register("master_df", master)
    con.execute("CREATE TABLE post_master AS SELECT * FROM master_df")

    con.execute("DROP TABLE IF EXISTS notion_metric")
    con.register("metrics_df", metrics)
    con.execute("CREATE TABLE notion_metric AS SELECT * FROM metrics_df")

    con.execute("DROP TABLE IF EXISTS content_pulse")
    con.register("cp_df", cp.assign(
        channel=cp["channel"].map(lambda v: json.dumps(v, ensure_ascii=False)
                                  if isinstance(v, list) else None),
        strategic_goal=cp["strategic_goal"].map(lambda v: json.dumps(v, ensure_ascii=False)
                                                if isinstance(v, list) else None),
        source_article=cp["source_article"].map(lambda v: json.dumps(v) if isinstance(v, list) else None),
    ))
    con.execute("CREATE TABLE content_pulse AS SELECT * FROM cp_df")

    # Ті самі дані у CSV — готові до COPY у Postgres, коли підніметься сервер
    master.to_csv(OUT / "post_master.csv", index=False)
    metrics.to_csv(OUT / "post_metric.csv", index=False)
    print(f"\nЗаписано: post_master {len(master):,} · notion_metric {len(metrics):,}")
    print(f"CSV для Postgres → {OUT}")

    print("\nПоходження тексту:")
    rep = con.execute("""
        SELECT text_origin, count(*) AS постів,
               CAST(median(text_chars) AS INT) AS медіана,
               max(text_chars) AS макс
        FROM post_master GROUP BY 1 ORDER BY 2 DESC
    """).df()
    print(rep.to_string(index=False))

    print("\nПовний текст за роками:")
    rep2 = con.execute("""
        SELECT substr(posted_at,1,4) AS рік, platform,
               count(*) AS усього,
               count(*) FILTER (WHERE NOT is_truncated AND text_best IS NOT NULL) AS з_текстом
        FROM post_master WHERE posted_at IS NOT NULL
        GROUP BY 1,2 ORDER BY 1,2
    """).df()
    print(rep2.to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
