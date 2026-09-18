"""Реєстр джерел: декларовані проти фактичних, і скільки з них за пейволом.

Читає sources.csv (ручний реєстр + знімок фактичного використання з бази 🧾 Articles
станом на 2026-09-11) і рахує те, що визначає вибір провайдера статей.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).parent
DB = HERE / "posts.duckdb"
REGISTRY = HERE / "sources.csv"

ACCESS_ORDER = ["open", "metered", "hard_paywall"]


def show(title: str, df: pd.DataFrame) -> None:
    print(f"\n{'─' * 74}\n{title}\n{'─' * 74}")
    print(df.to_string(index=False) if len(df) else "  (порожньо)")


def main() -> None:
    src = pd.read_csv(REGISTRY)
    total = src["articles_n"].sum()

    # Реєстр живе поруч із постами — щоб джойнити в одному місці
    con = duckdb.connect(str(DB))
    con.execute("DROP TABLE IF EXISTS source_registry")
    con.execute("CREATE TABLE source_registry AS SELECT * FROM src")
    con.close()

    by_access = (src.groupby("access")["articles_n"].agg(["sum", "count"])
                 .reindex(ACCESS_ORDER).fillna(0).astype(int)
                 .rename(columns={"sum": "статей", "count": "джерел"}))
    by_access["% статей"] = (by_access["статей"] / total * 100).round(1)
    show(f"Доступ до тексту — {total:,} статей у дайджесті", by_access.reset_index())

    print(f"\n  За пейволом (hard_paywall): "
          f"{by_access.loc['hard_paywall', 'статей']:,} статей "
          f"({by_access.loc['hard_paywall', '% статей']}%)")
    print(f"  Вільно парситься (open):    "
          f"{by_access.loc['open', 'статей']:,} статей "
          f"({by_access.loc['open', '% статей']}%)")

    by_cat = (src.groupby("category")["articles_n"].sum()
              .sort_values(ascending=False).reset_index()
              .rename(columns={"articles_n": "статей"}))
    by_cat["% "] = (by_cat["статей"] / total * 100).round(1)
    show("Категорії джерел", by_cat)

    undeclared = (src[(src["declared"] == "no") & (src["articles_n"] > 0)]
                  .sort_values("articles_n", ascending=False)
                  [["domain", "name", "category", "access", "articles_n"]])
    show(f"Використовуються, але НЕ в списку Digest Layout — {len(undeclared)} джерел, "
         f"{undeclared['articles_n'].sum():,} статей", undeclared)

    unused = (src[(src["declared"] == "yes") & (src["articles_n"] < 10)]
              .sort_values("articles_n")[["domain", "name", "articles_n"]])
    show("Задекларовані, але майже не використовуються", unused)

    top = (src.sort_values("articles_n", ascending=False).head(10)
           [["domain", "access", "articles_n"]])
    top["кумулятивно %"] = (top["articles_n"].cumsum() / total * 100).round(1)
    show("Топ-10 джерел — на них тестувати провайдерів", top)

    paywalled_top = top[top["access"] == "hard_paywall"]
    print(f"\n  З топ-10 за пейволом: {len(paywalled_top)} джерел, "
          f"{paywalled_top['articles_n'].sum():,} статей")


if __name__ == "__main__":
    main()
