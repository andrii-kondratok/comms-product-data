"""Складання тредів X із пооб'єктного архіву твітів.

Вхід:  raw_tweets_and_threads.csv — 56 972 твіти, 2014–2026.
       Колонка thread_id насправді містить id самого твіта, не треду:
       56 972 унікальних значень на 56 972 рядки, і сусідні твіти одного
       треду ніколи не мають спільного значення. Тобто групувати нема по чому.

Рішення: групуємо за авторською нумерацією, яку ТМ ставить у кінці кожного
твіта — «1/», «2/» … і «6X» на останньому. Це фактично conversation_id,
записаний людиною. 71.5% твітів його мають.

Правила складання:
    * тред починається твітом із маркером «1»
    * продовжується, поки номер зростає на 1 і розрив у часі < MAX_GAP
    * завершується маркером «X» або падінням номера
    * твіт без маркера — самостійний пост
    * твіт, що починається з «@», — відповідь комусь, у корпус не йде

Перевірка: id першого твіта зібраного треду має збігатися з Post URL
у Post Metrics. Це незалежний зовнішній якір, а не самоперевірка.

Запуск:  python posts_db/assemble_threads.py
"""

from __future__ import annotations

import re
from pathlib import Path

import duckdb
import pandas as pd

# Два архіви від колеги. Другий чистіший (удвічі менше пропусків у нумерації),
# але вужчий; у першому є 4 170 твітів, яких немає в другому. Об'єднання
# покриває більше, ніж будь-який окремо.
SOURCES = [
    (Path(r"C:\Users\Admin\Downloads\scrapper_clean_threads.csv"),
     {"id": "tweet_id", "text": "text", "created_at": "created_at",
      "likes": "likes", "retweets": "retweets"}),
    (Path(r"C:\Users\Admin\Downloads\raw_tweets_and_threads.csv"),
     {"thread_id": "tweet_id", "starter_text": "text", "created_at": "created_at",
      "starter_likes": "likes", "starter_retweets": "retweets"}),
]
DB = Path(__file__).parent / "posts.duckdb"
OUT = Path(__file__).parent.parent / "data" / "processed"

# 99.8% продовжень треду йдуть протягом 10 хвилин (медіана — 1 секунда).
# Беремо 15 хв: ширше не дає нічого, вужче починає рвати.
MAX_GAP_SECONDS = 900

TCO_TAIL = re.compile(r"(?:\s+https?://t\.co/\w+)+\s*$")
TRAIL = re.compile(r"(?:^|\s)(\d{1,2})\s*([/X])\s*$")
LEAD = re.compile(r"^\s*(\d{1,2})\s*/\s")


def marker(text: str) -> tuple[int | None, bool]:
    """Повертає (номер твіта в треді, чи це останній твіт)."""
    m = TRAIL.search(TCO_TAIL.sub("", text or ""))
    if m:
        return int(m.group(1)), m.group(2) == "X"
    m = LEAD.match(text or "")
    if m:
        return int(m.group(1)), False
    return None, False


def strip_marker(text: str) -> str:
    """Текст твіта без маркера нумерації, але з усіма посиланнями."""
    body = TCO_TAIL.sub("", text or "")
    body = TRAIL.sub("", body)
    body = LEAD.sub("", body)
    return body.strip()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    parts = []
    for path, mapping in SOURCES:
        if not path.exists():
            print(f"[skip] немає {path.name}")
            continue
        raw = pd.read_csv(path, dtype=str, keep_default_na=False,
                          na_values=[""], encoding="utf-8-sig")
        part = raw[list(mapping)].rename(columns=mapping)
        part["src"] = path.stem
        parts.append(part)
        print(f"[read] {path.name:<32} {len(part):>7,} твітів")

    df = pd.concat(parts, ignore_index=True)
    before = len(df)
    # Перший у списку джерел виграє при збігу id
    df = df.drop_duplicates("tweet_id", keep="first")
    print(f"[union] {before:,} → {len(df):,} унікальних твітів")

    # Більшість рядків у твіттерівському форматі, але частина — в ISO
    ts = pd.to_datetime(df["created_at"], utc=True,
                        format="%a %b %d %H:%M:%S %z %Y", errors="coerce")
    missed = ts.isna()
    if missed.any():
        ts.loc[missed] = pd.to_datetime(df.loc[missed, "created_at"], utc=True,
                                        format="mixed", errors="coerce")
        print(f"[dates] другим форматом розібрано {missed.sum() - ts.isna().sum():,}")
    df["created_at"] = ts
    dropped = df["created_at"].isna().sum()
    if dropped:
        print(f"[dates] відкинуто через нерозбірну дату: {dropped:,}")
    df = df.dropna(subset=["created_at"]).sort_values("created_at").reset_index(drop=True)

    parsed = df["text"].map(marker)
    df["num"] = [p[0] for p in parsed]
    df["is_end"] = [p[1] for p in parsed]
    df["is_reply_to_other"] = df["text"].str.startswith("@")
    df["clean"] = df["text"].map(strip_marker)

    threads: list[dict] = []
    current: list[int] = []
    # твіт → корінь треду: посилання на джерело часто стоїть не в першому твіті,
    # а привʼязувати його треба до поста, тобто до кореня
    tweet_root: list[tuple[str, str]] = []

    def flush() -> None:
        if not current:
            return
        rows = df.loc[current]
        root = rows.iloc[0]["tweet_id"]
        tweet_root.extend((t, root) for t in rows["tweet_id"])
        nums = rows["num"].dropna().astype(int).tolist()
        # Пропущені твіти в архіві: між 3/ і 5/ бракує 4/.
        missing = (nums[-1] - nums[0] + 1 - len(nums)) if len(nums) > 1 else 0
        threads.append({
            "root_tweet_id": rows.iloc[0]["tweet_id"],
            "created_at": rows.iloc[0]["created_at"],
            "tweet_count": len(rows),
            "first_num": nums[0] if nums else None,
            "last_num": nums[-1] if nums else None,
            "missing_tweets": max(missing, 0),
            "starts_at_one": bool(nums and nums[0] == 1),
            "hook_text": rows.iloc[0]["clean"],
            "full_text": "\n\n".join(rows["clean"].tolist()),
            "likes": pd.to_numeric(rows["likes"], errors="coerce").max(),
            "retweets": pd.to_numeric(rows["retweets"], errors="coerce").max(),
            "closed_by_x": bool(rows.iloc[-1]["is_end"]),
            "assembly": "numbered" if len(rows) > 1 else "single",
        })
        current.clear()

    prev_num, prev_time = None, None
    for i, row in df.iterrows():
        if row["is_reply_to_other"]:
            flush()
            prev_num, prev_time = None, None
            continue

        num = row["num"]
        gap = ((row["created_at"] - prev_time).total_seconds()
               if prev_time is not None else None)
        in_window = gap is not None and gap <= MAX_GAP_SECONDS

        # Номер зростає — продовження треду, навіть якщо не рівно на 1:
        # 7 379 стрибків у даних означають твіти, яких немає в архіві,
        # і рвати на них тред гірше, ніж зібрати його з позначкою про пропуск.
        if current and in_window and (
                (num is not None and prev_num is not None and num > prev_num)
                or num is None):
            current.append(i)
            if num is not None:
                prev_num = num
            prev_time = row["created_at"]
            if row["is_end"]:
                flush()
                prev_num, prev_time = None, None
            continue

        flush()
        current.append(i)
        prev_num = num
        prev_time = row["created_at"]
        if num is None or row["is_end"]:
            flush()
            prev_num, prev_time = None, None
    flush()

    th = pd.DataFrame(threads)
    print(f"Твітів на вході:       {len(df):,}")
    print(f"  відповідей іншим:    {df['is_reply_to_other'].sum():,} (відкинуто)")
    print(f"Зібрано одиниць:       {len(th):,}")
    print(f"  тредів (>1 твіта):   {(th['tweet_count'] > 1).sum():,}")
    print(f"  одиночних постів:    {(th['tweet_count'] == 1).sum():,}")
    print(f"  закритих маркером X: {th['closed_by_x'].sum():,}")
    print(f"  середня довжина:     {th[th['tweet_count'] > 1]['tweet_count'].mean():.1f}")
    print(f"  медіана знаків:      {th['full_text'].str.len().median():,.0f}")

    # ── Незалежна перевірка: чи збігаються корені з відомими постами ──
    con = duckdb.connect(str(DB), read_only=True)
    known = con.execute("""
        SELECT DISTINCT native_id FROM post_master
        WHERE platform = 'X' AND native_id IS NOT NULL
    """).df()["native_id"].astype(str)
    con.close()
    known_set = set(known)

    th["root_is_known_post"] = th["root_tweet_id"].isin(known_set)
    multi = th[th["tweet_count"] > 1]
    print(f"\nПеревірка по Post Metrics ({len(known_set):,} відомих постів X):")
    print(f"  коренів, що збіглися з відомим постом: "
          f"{th['root_is_known_post'].sum():,} / {len(th):,} "
          f"({th['root_is_known_post'].mean():.1%})")
    print(f"  серед тредів: {multi['root_is_known_post'].sum():,} / {len(multi):,} "
          f"({multi['root_is_known_post'].mean():.1%})")

    # Скільки НЕ-коренів помилково збіглися — ознака того, що склейка рве треди
    all_ids = set(df["tweet_id"])
    roots = set(th["root_tweet_id"])
    inner = all_ids - roots
    false_roots = len(inner & known_set)
    print(f"  відомих постів, що опинились ВСЕРЕДИНІ треду: {false_roots:,}"
          f"  ← мало би бути близько нуля")

    print("\nЗібрано за роками:")
    by_year = th.groupby(th["created_at"].dt.year).agg(
        одиниць=("root_tweet_id", "size"),
        тредів=("tweet_count", lambda s: (s > 1).sum()),
        сер_твітів=("tweet_count", "mean"))
    print(by_year.round(1).to_string())

    th.to_csv(OUT / "assembled_threads.csv", index=False)
    pd.DataFrame(tweet_root, columns=["tweet_id", "root_id"]).to_csv(
        OUT / "tweet_thread_map.csv", index=False)
    print(f"\nЗаписано → {OUT / 'assembled_threads.csv'}")


if __name__ == "__main__":
    main()
