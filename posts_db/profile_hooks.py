"""Профіль classified_hooks_x_posts.csv — кандидат на корпус перших твітів X."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

SRC = Path(r"C:\Users\Admin\Downloads\classified_hooks_x_posts.csv")

LEAD_NUM = re.compile(r"^\s*\d{1,2}\s*/")           # "1/ ..." на початку
TRAIL_NUM = re.compile(r"\d{1,2}\s*/\s*$")          # "... 1/" у кінці
THREAD_HINT = re.compile(r"\bthread\b", re.I)


def main() -> None:
    df = pd.read_csv(SRC, dtype=str, keep_default_na=False,
                     na_values=["", "NA", "NaN"], encoding="utf-8-sig")
    print(f"Рядків: {len(df):,}   Колонок: {len(df.columns)}")
    print("Колонки:", list(df.columns))

    d = pd.to_datetime(df["created_at"], errors="coerce", utc=True, format="mixed")
    print(f"\nДіапазон: {d.min()}  →  {d.max()}   (не розпарсилось: {d.isna().sum()})")
    print("\nТвітів за роками:")
    for year, n in d.dt.year.value_counts().sort_index().items():
        print(f"   {int(year)}: {n:>7,}")

    t = df["text"].astype("string").fillna("")
    ln = t.str.len()
    print(f"\nДовжина тексту: min={ln.min()} med={ln.median():.0f} "
          f"p95={ln.quantile(0.95):.0f} max={ln.max()}")
    caps = ln.value_counts().head(3)
    print("   найчастіші довжини:", {int(k): int(v) for k, v in caps.items()})
    print(f"   рівно 200 знаків: {(ln == 200).sum()}  (ознака обрізання як у Post Metrics)")

    lead = t.str.match(LEAD_NUM)
    trail = t.str.contains(TRAIL_NUM, regex=True)
    print(f"\nНумерація на ПОЧАТКУ:  {lead.sum():>6,}  ({lead.mean():.1%})")
    print(f"Нумерація в КІНЦІ:     {trail.sum():>6,}  ({trail.mean():.1%})")
    print(f"Згадка 'thread':       {t.str.contains(THREAD_HINT, regex=True).sum():>6,}")

    print("\nНумерація за роками (частка від твітів року):")
    g = pd.DataFrame({"year": d.dt.year, "lead": lead, "trail": trail}).dropna(subset=["year"])
    agg = g.groupby("year").agg(n=("lead", "size"), lead=("lead", "sum"), trail=("trail", "sum"))
    agg["lead_%"] = (agg["lead"] / agg["n"] * 100).round(1)
    agg["trail_%"] = (agg["trail"] / agg["n"] * 100).round(1)
    print(agg.to_string())

    print(f"\nДублікати tweet_id: {df['tweet_id'].duplicated().sum():,}")
    print(f"Унікальних tweet_id: {df['tweet_id'].nunique():,}")

    if "classification" in df.columns:
        print("\nclassification:")
        for k, v in df["classification"].value_counts(dropna=False).head(12).items():
            print(f"   {str(k)[:40]:<42} {v:>6,}")
    if "classification_confidence" in df.columns:
        print("confidence:", dict(df["classification_confidence"].value_counts(dropna=False)))

    m = pd.to_numeric(df["impressions"], errors="coerce")
    print(f"\nimpressions: заповнено {m.notna().mean():.1%}, "
          f"median={m.median():.0f}, max={m.max():.0f}")


if __name__ == "__main__":
    main()
