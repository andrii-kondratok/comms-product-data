"""Чому складання рве треди: розподіл розривів між сусідніми номерами."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

SRC = Path(r"C:\Users\Admin\Downloads\raw_tweets_and_threads.csv")
TCO_TAIL = re.compile(r"(?:\s+https?://t\.co/\w+)+\s*$")
TRAIL = re.compile(r"(?:^|\s)(\d{1,2})\s*([/X])\s*$")
LEAD = re.compile(r"^\s*(\d{1,2})\s*/\s")


def marker(text: str):
    m = TRAIL.search(TCO_TAIL.sub("", text or ""))
    if m:
        return int(m.group(1)), m.group(2) == "X"
    m = LEAD.match(text or "")
    return (int(m.group(1)), False) if m else (None, False)


def main() -> None:
    df = pd.read_csv(SRC, dtype=str, keep_default_na=False, na_values=[""],
                     encoding="utf-8-sig")
    df = df.rename(columns={"thread_id": "tweet_id", "starter_text": "text"})
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True,
                                      format="%a %b %d %H:%M:%S %z %Y")
    df = df.sort_values("created_at").reset_index(drop=True)
    p = df["text"].map(marker)
    df["num"] = [x[0] for x in p]
    df["is_reply"] = df["text"].str.startswith("@")

    print(f"Усього твітів: {len(df):,}  ·  з маркером: {df['num'].notna().sum():,}")
    print("\nРозподіл номерів у маркерах:")
    print(df["num"].value_counts().sort_index().head(12).to_string())

    # Дивимось лише на послідовності з маркерами, ігноруючи все між ними
    m = df[df["num"].notna() & ~df["is_reply"]].copy()
    m["prev_num"] = m["num"].shift()
    m["gap_s"] = m["created_at"].diff().dt.total_seconds()
    m["rows_between"] = m.index.to_series().diff() - 1

    step1 = m[m["num"] == m["prev_num"] + 1]
    print(f"\nПар із номером +1: {len(step1):,}")
    print("Розрив у часі між ними (секунди):")
    print(step1["gap_s"].describe(percentiles=[.5, .75, .9, .95, .99]).round(0).to_string())
    print("\nСкільки твітів БЕЗ маркера стоїть між ними:")
    print(step1["rows_between"].describe(percentiles=[.5, .9, .95, .99]).round(1).to_string())

    for limit in (600, 1800, 3600, 7200, 86400):
        share = (step1["gap_s"] <= limit).mean()
        print(f"  розрив ≤ {limit:>6}с  покриває {share:.1%} продовжень")

    # Чи є випадки, де номер зростає не на 1 — пропущені твіти в архіві
    jump = m[(m["num"] > m["prev_num"] + 1) & (m["prev_num"].notna())]
    print(f"\nСтрибків номера (>+1): {len(jump):,} — ознака твітів, "
          f"яких немає в архіві")


if __name__ == "__main__":
    main()
