"""Розбір знімка 🧾 Articles у розкладку core.source / core.article.

Окремо від load_posts.py, бо це інша сутність і інший темп оновлення.

Додатково виводить `tier` — цільову змінну для моделі релевантності:
    2  стаття дала пост          (є X draft / FB draft, або статус «Post draft created»)
    1  стаття потрапила в дайджест (усе, що взагалі є в цій базі)
    0  була в пулі, не взяли      (тут не буває — джерело ops.candidate_pool)

Запуск:  python postgres/load_articles.py
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
RAW = ROOT / "data" / "raw" / "notion"
OUT = ROOT / "data" / "processed"
REGISTRY = ROOT / "posts_db" / "sources.csv"

TRACKING = re.compile(r"[?&](utm_[^=]+|fbclid|gclid|srnd|mod|syn-[^=]+|ref|ref_src)=[^&]*", re.I)

# Статуси Articles → чи вдалося дістати текст
STATUS_TO_RETRIEVAL = {
    "Linked": "pending",
    "Intake Agent Hydrated": "pending",
    "Full Text": "full_text",
    "Key Points Hydrated": "full_text",
    "Digest Hydrated": "full_text",
    "Draft": "full_text",
    "Post draft created": "full_text",
    "Rejected": "pending",
}


def plain(prop: dict | None) -> str | None:
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("rich_text", "title") and v:
        return "".join(x.get("plain_text", "") for x in v) or None
    return None


def scalar(prop: dict | None):
    if not prop:
        return None
    t = prop.get("type")
    v = prop.get(t)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "relation":
        return [x.get("id") for x in (v or []) if x] or None
    if t in ("created_time", "last_edited_time", "url", "checkbox"):
        return v
    return v


def canonical(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    u = TRACKING.sub("", url.strip())
    u = re.sub(r"[?&]+$", "", u)
    return u.rstrip("/")


def domain_of(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    m = re.match(r"https?://(?:www\.)?([^/?#]+)", url.strip(), re.I)
    return m.group(1).lower() if m else None


def latest(pattern: str) -> Path:
    files = sorted(glob.glob(str(RAW / f"{pattern}__*.jsonl")))
    if not files:
        raise SystemExit(f"Немає знімка {pattern}")
    return Path(files[-1])


def load_bodies() -> dict[str, dict]:
    p = RAW / "article_bodies.jsonl"
    if not p.exists():
        print("  (тіл статей ще немає — запусти export_article_bodies.py)")
        return {}
    bodies = {}
    for line in p.open(encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        bodies[r["notion_page_id"]] = r
    return bodies


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bodies = load_bodies()

    rows = []
    for line in latest("articles").open(encoding="utf-8"):
        r = json.loads(line)
        p = r["properties"]
        src_url = plain(p.get("Source URL")) or scalar(p.get("Working URL"))
        url_c = canonical(src_url)
        status = scalar(p.get("Status"))
        x_draft = scalar(p.get("X draft"))
        fb_draft = scalar(p.get("FB draft"))
        body = bodies.get(r["notion_page_id"], {})

        produced_post = bool(x_draft or fb_draft) or status == "Post draft created" \
            or bool(scalar(p.get("X"))) or bool(scalar(p.get("FB")))

        rows.append({
            "notion_page_id": r["notion_page_id"],
            "url_canonical": url_c,
            "domain": domain_of(url_c),
            "title": plain(p.get("Extracted Title")),
            "subtitle": plain(p.get("Subtitle")),
            "section": scalar(p.get("Section")),
            "status": status,
            "retrieval_note": plain(p.get("Retrieval Note")),
            "retrieval_status": STATUS_TO_RETRIEVAL.get(status, "pending"),
            "digest_id": (scalar(p.get("Digest")) or [None])[0],
            "x_draft_id": (x_draft or [None])[0],
            "fb_draft_id": (fb_draft or [None])[0],
            "tier": 2 if produced_post else 1,
            "created_at": scalar(p.get("Created")),
            "updated_at": scalar(p.get("Updated")),
            "key_points": body.get("key_points"),
            "body_text": body.get("full_text"),
            "archived": r.get("archived"),
        })

    df = pd.DataFrame(rows)
    df["body_chars"] = df["body_text"].str.len()
    # Текст або є, або його нема — статус має відповідати фактові, а не надії
    df.loc[df["body_text"].isna() & (df["retrieval_status"] == "full_text"),
           "retrieval_status"] = "pending"

    print(f"Статей: {len(df):,}")
    print(f"  з канонічним URL:  {df['url_canonical'].notna().sum():,}")
    print(f"  дублікатів URL:    {df['url_canonical'].duplicated().sum():,}")
    print(f"  з тілом статті:    {df['body_text'].notna().sum():,} "
          f"(медіана {df['body_chars'].median():.0f} знаків)")
    print(f"  tier=2 (дали пост): {(df['tier'] == 2).sum():,}")

    print("\nЗа доменами (топ-12):")
    print(df["domain"].value_counts().head(12).to_string())

    print("\nЗа статусом:")
    print(df["status"].value_counts(dropna=False).to_string())

    # Реєстр джерел: усе, що трапилось, навіть якщо ще не в sources.csv
    reg = pd.read_csv(REGISTRY) if REGISTRY.exists() else pd.DataFrame(columns=["domain"])
    counts = df["domain"].value_counts().rename_axis("domain").reset_index(name="articles_n")
    src = counts.merge(reg.drop(columns=["articles_n"], errors="ignore"),
                       on="domain", how="left")
    src["access"] = src["access"].fillna("unknown")
    src["category"] = src["category"].fillna("other")
    src["declared"] = src["declared"].fillna("no")
    new = src[src["name"].isna()]
    if len(new):
        print(f"\nНові домени, яких нема в sources.csv — {len(new)}:")
        print(new[["domain", "articles_n"]].to_string(index=False))

    df.to_csv(OUT / "core_article.csv", index=False)
    src.to_csv(OUT / "core_source.csv", index=False)
    print(f"\nЗаписано → {OUT / 'core_article.csv'} · {OUT / 'core_source.csv'}")


if __name__ == "__main__":
    main()
