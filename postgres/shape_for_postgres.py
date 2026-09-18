"""Готує CSV точно під колонки core.source і core.article для COPY."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

OUT = Path(__file__).parent.parent / "data" / "processed"
REG = Path(__file__).parent.parent / "posts_db" / "sources.csv"

VALID_CATEGORY = {"global", "ua_media", "ru_media", "analysis", "kse", "other"}
VALID_ACCESS = {"open", "metered", "hard_paywall", "unknown"}


def main() -> None:
    src = pd.read_csv(OUT / "core_source.csv")
    src["category"] = src["category"].where(src["category"].isin(VALID_CATEGORY), "other")
    src["access"] = src["access"].where(src["access"].isin(VALID_ACCESS), "unknown")
    src["license_class"] = src["access"].map({
        "open": "rss_public", "metered": "unknown",
        "hard_paywall": "unknown", "unknown": "unknown"})
    src["declared_in_digest"] = src["declared"].eq("yes")
    src["is_active"] = True
    src["rss_url"] = None
    cols = ["domain", "name", "category", "lang", "country", "access",
            "license_class", "rss_url", "declared_in_digest", "is_active", "notes"]
    src["name"] = src["name"].fillna(src["domain"])
    src[cols].to_csv(OUT / "pg_source.csv", index=False)

    art = pd.read_csv(OUT / "core_article.csv")
    art = art[art["url_canonical"].notna()].copy()
    # Один рядок на URL: 137 дублікатів зводимо, лишаючи найповніший текст
    art["_len"] = art["body_text"].str.len().fillna(0)
    art = (art.sort_values("_len", ascending=False)
              .drop_duplicates("url_canonical", keep="first"))
    art["retrieval_status"] = art["body_text"].notna().map(
        {True: "full_text", False: "pending"})
    art["paywalled"] = None
    out = art[["domain", "url_canonical", "title", "subtitle", "body_text",
               "key_points", "section", "status", "tier", "retrieval_status",
               "paywalled", "created_at", "notion_page_id"]]
    out.to_csv(OUT / "pg_article.csv", index=False)

    print(f"pg_source.csv  {len(src):,} джерел")
    print(f"pg_article.csv {len(out):,} статей "
          f"({out['body_text'].notna().sum():,} з текстом)")


if __name__ == "__main__":
    main()
