"""Профайлер джерел для бази старих постів.

Нічого не змінює. Читає всі відомі експорти, друкує стан даних:
обсяги, діапазони дат, порожнечі, дублікати, аномалії довжини тексту.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

SRC = Path(r"D:\Posts analysis")

X_URL = re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)", re.I)
FB_STORY = re.compile(r"story_fbid=([A-Za-z0-9]+)", re.I)
FB_POSTS = re.compile(r"facebook\.com/[^/]+/posts/([A-Za-z0-9]+)", re.I)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def platform_of(url: str) -> str:
    if not isinstance(url, str):
        return "unknown"
    u = url.lower()
    if "x.com" in u or "twitter.com" in u:
        return "X"
    if "facebook.com" in u or "fb.com" in u:
        return "FB"
    return "unknown"


def native_id(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    for pat in (X_URL, FB_STORY, FB_POSTS):
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def describe(df: pd.DataFrame, name: str, text_col: str, date_col: str, url_col: str | None) -> None:
    rule(f"{name}  —  {len(df):,} рядків, {len(df.columns)} колонок")
    print("Колонки:", list(df.columns))

    if date_col in df.columns:
        d = pd.to_datetime(df[date_col], errors="coerce", utc=True, format="mixed")
        print(f"\nДати: {d.min()}  →  {d.max()}   (не розпарсилось: {d.isna().sum():,})")
        by_year = d.dt.year.value_counts().sort_index()
        print("Постів за роками:")
        for year, n in by_year.items():
            if pd.notna(year):
                print(f"   {int(year)}: {n:>7,}")

    if text_col in df.columns:
        t = df[text_col].astype("string")
        lengths = t.str.len()
        print(f"\nТекст: порожніх {t.isna().sum() + (t.fillna('').str.strip() == '').sum():,}")
        print(f"   довжина: min={lengths.min()}  med={lengths.median()}  "
              f"p95={lengths.quantile(0.95):.0f}  max={lengths.max()}")
        # Ознака обрізання: аномальна концентрація на рівно одній довжині
        top_len = lengths.value_counts().head(5)
        print("   найчастіші довжини (ознака обрізання, якщо одна домінує):")
        for ln, n in top_len.items():
            print(f"      {int(ln):>5} знаків: {n:>6,} постів")

    if url_col and url_col in df.columns:
        u = df[url_col]
        plats = u.map(platform_of).value_counts()
        print("\nПлатформи:", dict(plats))
        ids = u.map(native_id)
        print(f"   URL без розпізнаного id: {ids.isna().sum():,}")
        dupes = ids.dropna().duplicated().sum()
        print(f"   дублікати за native id: {dupes:,}")
        print(f"   дублікати за повним URL: {u.duplicated().sum():,}")


def main() -> int:
    if not SRC.exists():
        print(f"Немає теки {SRC}", file=sys.stderr)
        return 1

    # --- 1. all_posts.csv: зведений FB + X
    p = SRC / "all_posts.csv"
    all_posts = pd.read_csv(p, dtype=str, keep_default_na=False, na_values=["", "NA", "NaN"])
    describe(all_posts, "all_posts.csv", "text", "date", "url")

    if "source" in all_posts.columns:
        print("\nПоле source:", dict(all_posts["source"].value_counts(dropna=False)))
    if "post_type" in all_posts.columns:
        print("post_type (топ-10):", dict(all_posts["post_type"].value_counts(dropna=False).head(10)))

    rule("all_posts.csv — заповненість метрик за платформою")
    all_posts["_plat"] = all_posts["url"].map(platform_of)
    metric_cols = [c for c in ("likes", "comments", "shares", "reach", "views") if c in all_posts.columns]
    fill = all_posts.groupby("_plat")[metric_cols].apply(lambda g: g.notna().mean().round(3))
    print(fill.to_string())

    # --- 2. twitter_mylovanov_posts.csv: окремий експорт X
    p = SRC / "twitter_mylovanov_posts.csv"
    if p.exists():
        tw = pd.read_csv(p, dtype=str, keep_default_na=False, na_values=["", "NA", "NaN"])
        describe(tw, "twitter_mylovanov_posts.csv", "text", "created_at", "url")

    # --- 3. Meta-експорти Facebook
    for fname in ("2022 - 2023.csv", "2024 - 2025.csv", "2025 - 2026.csv"):
        fp = SRC / fname
        if not fp.exists():
            continue
        fb = pd.read_csv(fp, dtype=str, keep_default_na=False,
                         na_values=["", "NA", "NaN"], encoding="utf-8-sig")
        rule(f"{fname}  —  {len(fb):,} рядків")
        print("Колонки:", list(fb.columns)[:14], "..." if len(fb.columns) > 14 else "")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
