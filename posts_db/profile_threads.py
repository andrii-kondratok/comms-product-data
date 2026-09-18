"""Профіль raw_tweets_and_threads.csv — перевірка, чи thread_id справді групує треди."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

SRC = Path(r"C:\Users\Admin\Downloads\raw_tweets_and_threads.csv")

# Маркер часто стоїть ПЕРЕД доданим посиланням чи медіа: «... 5/ https://t.co/abc».
# Тому спершу зрізаємо хвостові t.co-лінки, і лише потім шукаємо номер.
TCO_TAIL = re.compile(r"(?:\s+https?://t\.co/\w+)+\s*$")
# «X» замість слеша — авторська позначка останнього твіта треду («6X»).
TRAIL = re.compile(r"(?:^|\s)(\d{1,2})\s*([/X])\s*$")
LEAD = re.compile(r"^\s*(\d{1,2})\s*/\s")


def strip_tail_links(s: str) -> str:
    return TCO_TAIL.sub("", s)


def main() -> None:
    df = pd.read_csv(SRC, dtype=str, keep_default_na=False,
                     na_values=[""], encoding="utf-8-sig")
    print(f"Записів: {len(df):,}   Колонок: {len(df.columns)}")
    print("Колонки:", list(df.columns))

    d = pd.to_datetime(df["created_at"], errors="coerce", utc=True,
                       format="%a %b %d %H:%M:%S %z %Y")
    print(f"\nДіапазон: {d.min()} → {d.max()}  (не розпарсилось: {d.isna().sum():,})")
    print("\nТвітів за роками:")
    for y, n in d.dt.year.value_counts().sort_index().items():
        print(f"   {int(y)}: {n:>7,}")

    n_rows, n_threads = len(df), df["thread_id"].nunique()
    print(f"\nthread_id: {n_threads:,} унікальних на {n_rows:,} рядків "
          f"→ {n_rows / n_threads:.2f} рядка на thread_id")

    sizes = df["thread_id"].value_counts()
    print("Розподіл розміру групи:")
    print(sizes.value_counts().sort_index().head(10).to_string())

    # Чи справді це тред? Якщо thread_id працює, всередині групи мають бути
    # послідовні номери 1/, 2/, 3/ — а не по одному твіту на групу.
    txt = df["starter_text"].astype("string").fillna("")
    stripped = txt.map(strip_tail_links)
    trail = stripped.str.extract(TRAIL)
    df["_trail"] = trail[0]
    df["_end_marker"] = trail[1]
    df["_lead"] = txt.str.extract(LEAD, expand=False)
    has_num = df["_trail"].notna() | df["_lead"].notna()
    print(f"\nТвітів із маркером нумерації: {has_num.sum():,} ({has_num.mean():.1%})")
    print(f"  на початку: {df['_lead'].notna().sum():,}   "
          f"у кінці: {df['_trail'].notna().sum():,}")
    print(f"  позначка кінця треду 'X': {(df['_end_marker'] == 'X').sum():,}")

    # Скільки твітів із маркером «1» — стільки й має бути справжніх тредів
    starters = (df["_trail"] == "1") | (df["_lead"] == "1")
    print(f"  помічені як перший ('1/'): {starters.sum():,}")
    if starters.sum():
        print(f"  → оцінка середньої довжини треду: "
              f"{has_num.sum() / starters.sum():.1f} твітів")

    print("\nПеревірка гіпотези: чи thread_id однаковий у твітах одного треду?")
    numbered = df[has_num].copy()
    numbered["_dt"] = d[has_num]
    numbered = numbered.sort_values("_dt")
    # Твіти, опубліковані в межах 10 хвилин один від одного, майже напевно один тред
    numbered["_gap"] = numbered["_dt"].diff().dt.total_seconds()
    close = numbered[numbered["_gap"].between(0, 600)]
    same_id = (close["thread_id"].values == close["thread_id"].shift().values).sum()
    print(f"  сусідніх твітів у межах 10 хв: {len(close):,}")
    print(f"  з них мають ОДНАКОВИЙ thread_id: {same_id:,} "
          f"({same_id / max(len(close), 1):.1%})")

    print(f"\nДублікати thread_id як id твіта: "
          f"{df['thread_id'].duplicated().sum():,}")
    print(f"Порожній текст: {(txt.str.len() == 0).sum():,}")
    print(f"Відповіді іншим (@...): {txt.str.startswith('@').sum():,}")
    print(f"Довжина тексту: медіана {txt.str.len().median():.0f}, "
          f"max {txt.str.len().max():,}")


if __name__ == "__main__":
    main()
