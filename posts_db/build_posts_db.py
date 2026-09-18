"""Збирає базу опублікованих постів ТМ з наявних локальних експортів.

Запуск:  python posts_db/build_posts_db.py

Джерела (кожне зі своєю провенансною позначкою):
  * all_posts.csv                  — Facebook, повний текст, 2022-02 → 2026-02
  * classified_hooks_x_posts.csv   — X, повний текст ПЕРШИХ твітів, 2022-02 → 2026-07
  * twitter_mylovanov_posts.csv    — X, свіжий зріз 2026-05 → 2026-07 (перетин з попереднім)

Нічого не вигадує: якщо поля немає в джерелі, воно лишається NULL.
Повний текст тредів X з'явиться, коли буде пооб'єктна викачка — схема для нього готова.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).parent
DB_PATH = HERE / "posts.duckdb"
SCHEMA = HERE / "schema.sql"

SOURCES = [
    (Path(r"D:\Posts analysis\all_posts.csv"), "fb_export"),
    (Path(r"C:\Users\Admin\Downloads\classified_hooks_x_posts.csv"), "x_hooks"),
    (Path(r"D:\Posts analysis\twitter_mylovanov_posts.csv"), "x_recent"),
]

# Пріоритет джерела при конфлікті за той самий post_id (більше = важливіше).
KIND_PRIORITY = {"x_hooks": 30, "x_recent": 20, "fb_export": 10}

X_URL = re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)", re.I)
FB_STORY = re.compile(r"story_fbid=([A-Za-z0-9]+)", re.I)
FB_POSTS = re.compile(r"facebook\.com/[^/]+/posts/([A-Za-z0-9]+)", re.I)
FB_PERMA = re.compile(r"facebook\.com/(\d+)/posts/([A-Za-z0-9]+)", re.I)

LEAD_NUM = re.compile(r"^\s*\d{1,2}\s*/\s*")
TRAIL_NUM = re.compile(r"\s*\d{1,2}\s*/\s*$")
TRACKING = re.compile(r"[?&](utm_[^=]+|fbclid|gclid|s|t|ref|ref_src|ref_url)=[^&]*", re.I)


# ---------------------------------------------------------------- нормалізація

def platform_of(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    u = url.lower()
    if "x.com" in u or "twitter.com" in u:
        return "X"
    if "facebook.com" in u or "fb.com" in u:
        return "FB"
    return None


def native_id_of(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    for pat in (X_URL, FB_STORY, FB_PERMA, FB_POSTS):
        m = pat.search(url)
        if m:
            return m.group(m.lastindex or 1)
    return None


def canonical_url(url: str | None) -> str | None:
    """Прибирає трекінгові параметри і зводить хост до канонічного вигляду."""
    if not isinstance(url, str) or not url.strip():
        return None
    u = url.strip()
    u = TRACKING.sub("", u)
    u = u.replace("://twitter.com/", "://x.com/")
    u = u.replace("://www.x.com/", "://x.com/")
    u = re.sub(r"[?&]$", "", u)
    return u.rstrip("/")


def numbering_style(text: str | None) -> str:
    """Маркер епохи стилю: де стоїть нумерація треду."""
    if not isinstance(text, str) or not text.strip():
        return "none"
    if LEAD_NUM.match(text):
        return "leading"
    if TRAIL_NUM.search(text):
        return "trailing"
    return "none"


def clean_text(text: str | None) -> str | None:
    """Той самий текст без маркерів нумерації — те, що піде у fine-tune."""
    if not isinstance(text, str):
        return None
    out = LEAD_NUM.sub("", text)
    out = TRAIL_NUM.sub("", out)
    return out.strip()


def parse_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", utc=True, format="mixed")


# ---------------------------------------------------------------- читання джерел

def read_source(path: Path, kind: str) -> tuple[pd.DataFrame, list[tuple[int, str, str]]]:
    """Повертає нормалізований кадр і список проблем завантаження."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False,
                     na_values=["", "NA", "NaN"], encoding="utf-8-sig")
    issues: list[tuple[int, str, str]] = []

    if kind == "fb_export":
        text_col, date_col = "text", "date"
        metrics = {"likes": "likes", "comments": "comments",
                   "shares": "shares", "reach": "reach", "views": "views"}
        post_type = df.get("post_type")
    else:
        text_col, date_col = "text", "created_at"
        metrics = {"impressions": "impressions", "likes": "likes",
                   "comments": "comments", "retweets": "shares"}
        post_type = None

    out = pd.DataFrame()
    out["url"] = df["url"]
    out["url_canonical"] = df["url"].map(canonical_url)
    # Платформу беремо з URL; якщо URL відсутній або нерозпізнаний — з типу джерела.
    default_platform = "FB" if kind == "fb_export" else "X"
    out["platform"] = df["url"].map(platform_of).fillna(default_platform)

    # tweet_id у джерелі надійніший за розбір URL
    if "tweet_id" in df.columns:
        out["native_id"] = df["tweet_id"].fillna(df["url"].map(native_id_of))
    else:
        out["native_id"] = df["url"].map(native_id_of)

    ts = parse_ts(df[date_col])
    out["posted_at"] = ts
    out["posted_date"] = ts.dt.date

    hook = df[text_col].astype("string")
    out["hook_raw"] = hook
    out["hook_clean"] = hook.map(clean_text)
    out["numbering_style"] = hook.map(numbering_style)
    out["hook_chars"] = hook.str.len()
    out["has_newline"] = hook.str.contains("\n", regex=False).fillna(False)
    out["is_truncated_suspect"] = hook.str.len().eq(200).fillna(False)
    out["body_raw"] = pd.NA
    out["post_type"] = post_type.astype("string") if post_type is not None else pd.NA

    # Проблеми, які не мовчимо
    for idx in out.index[df["url"].map(platform_of).isna()]:
        issues.append((int(idx), "platform_assumed_from_source", str(df.at[idx, "url"])[:200]))
    for idx in out.index[out["native_id"].isna()]:
        issues.append((int(idx), "no_native_id", str(df.at[idx, "url"])[:200]))
    for idx in out.index[out["posted_at"].isna()]:
        issues.append((int(idx), "unparsed_date", str(df.at[idx, date_col])[:200]))
    for idx in out.index[out["hook_raw"].isna() | (out["hook_chars"].fillna(0) == 0)]:
        issues.append((int(idx), "empty_text", ""))

    # Пости без розпізнаного id не зливаються в купу: кожен дістає власний ключ,
    # прив'язаний до файлу й номера рядка, і лишається видимим у load_issue.
    fallback = pd.Series([f"{kind}#{i}" for i in out.index], index=out.index, dtype="object")
    out["post_id"] = (out["platform"].fillna("?").astype(str) + ":"
                      + out["native_id"].astype("object").fillna(fallback).astype(str))

    metric_frame = pd.DataFrame({"post_id": out["post_id"]})
    for src_col, metric_name in metrics.items():
        if src_col in df.columns:
            metric_frame[metric_name] = pd.to_numeric(df[src_col], errors="coerce")

    return out.assign(_kind=kind), issues, metric_frame


# ---------------------------------------------------------------- збірка

def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA.read_text(encoding="utf-8"))

    now = datetime.now(timezone.utc)
    all_posts: list[pd.DataFrame] = []
    all_metrics: list[pd.DataFrame] = []

    for sid, (path, kind) in enumerate(SOURCES, start=1):
        if not path.exists():
            print(f"[skip] немає {path}")
            continue

        posts, issues, metrics = read_source(path, kind)
        posts["source_file_id"] = sid
        metrics["source_file_id"] = sid
        metrics["observed_at"] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)

        con.execute(
            "INSERT INTO source_file VALUES (?,?,?,?,?,?,?)",
            [sid, str(path), kind,
             datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
             len(posts), len(posts), now],
        )
        if issues:
            con.executemany(
                "INSERT INTO load_issue VALUES (?,?,?,?)",
                [(sid, i, code, detail) for i, code, detail in issues],
            )

        all_posts.append(posts)
        all_metrics.append(metrics)
        print(f"[read] {path.name:<38} {kind:<10} {len(posts):>6,} рядків, "
              f"{len(issues):>4} зауважень")

    posts = pd.concat(all_posts, ignore_index=True)
    posts["_prio"] = posts["_kind"].map(KIND_PRIORITY)

    # Дедуплікація: один пост = один рядок. Виграє джерело з вищим пріоритетом,
    # за рівності — довший текст (менше шансів, що обрізаний).
    before = len(posts)
    posts = (posts
             .sort_values(["post_id", "_prio", "hook_chars"], ascending=[True, False, False])
             .drop_duplicates("post_id", keep="first"))
    print(f"\n[dedup] {before:,} → {len(posts):,} (злито {before - len(posts):,})")

    posts["ingested_at"] = now
    cols = ["post_id", "platform", "native_id", "url", "url_canonical",
            "posted_at", "posted_date", "hook_raw", "hook_clean", "body_raw",
            "numbering_style", "hook_chars", "has_newline", "is_truncated_suspect",
            "post_type", "source_file_id", "ingested_at"]
    con.register("posts_df", posts[cols])
    con.execute(f"INSERT INTO post SELECT {', '.join(cols)} FROM posts_df")

    metrics = pd.concat(all_metrics, ignore_index=True)
    long = metrics.melt(
        id_vars=["post_id", "source_file_id", "observed_at"],
        var_name="metric", value_name="value",
    ).dropna(subset=["value"])
    long = long[long["post_id"].isin(set(posts["post_id"]))]
    long = long.drop_duplicates(["post_id", "metric", "source_file_id"])
    con.register("metrics_df", long[["post_id", "metric", "value", "source_file_id", "observed_at"]])
    con.execute("INSERT INTO post_metric SELECT * FROM metrics_df")

    con.execute("CREATE INDEX idx_post_date ON post(posted_date)")
    con.execute("CREATE INDEX idx_post_platform ON post(platform)")
    con.execute("CREATE INDEX idx_metric_name ON post_metric(metric)")

    print(f"\nБаза: {DB_PATH}")
    print(f"  post        {con.execute('SELECT count(*) FROM post').fetchone()[0]:>7,}")
    print(f"  post_metric {con.execute('SELECT count(*) FROM post_metric').fetchone()[0]:>7,}")
    print(f"  load_issue  {con.execute('SELECT count(*) FROM load_issue').fetchone()[0]:>7,}")
    con.close()


if __name__ == "__main__":
    main()
