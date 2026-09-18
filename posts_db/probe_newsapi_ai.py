"""Обережна розвідка NewsAPI.ai (Event Registry).

Безкоштовно дається 2 000 токенів, тож витрачаємо мінімум:
  1. getUsageInfo — скільки лишилось (має бути безкоштовно)
  2. articleMapper/getArticleUri — мапимо наш URL у їхній ідентифікатор
  3. article/getArticle з articleBodyLen=-1 — просимо ПОВНЕ тіло
  4. getUsageInfo ще раз — скільки з'їв прогін

Жорсткий ліміт викликів, щоб випадковий цикл не спалив квоту.

Запуск:  python posts_db/probe_newsapi_ai.py [url]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).parent))
from run_acceptance import UA, load_key  # noqa: E402

BASE = "https://eventregistry.org/api/v1"
MAX_CALLS = 8
_calls = 0

DEFAULT_URL = ("https://www.economist.com/europe/2026/09/10/"
               "the-afd-is-making-the-weather-in-german-politics")


def call(path: str, params: dict) -> dict:
    global _calls
    if _calls >= MAX_CALLS:
        raise SystemExit(f"Досягнуто ліміт {MAX_CALLS} викликів — зупиняюсь навмисно")
    _calls += 1
    url = f"{BASE}/{path}?{parse.urlencode(params)}"
    req = request.Request(url, headers=UA)
    try:
        with request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8")[:300]}
    except Exception as e:                                   # noqa: BLE001
        return {"_err": str(e)[:200]}


def usage(key: str, label: str) -> None:
    d = call("user/getUsageInfo", {"apiKey": key})
    if "_http" in d or "_err" in d:
        print(f"{label}: не вдалось — {d}")
        return
    u = d.get("usage", d)
    print(f"{label}: {json.dumps(u, ensure_ascii=False)[:220]}")


def main() -> None:
    key = load_key("NEWSAPI_AI_KEY")
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

    usage(key, "Квота ДО")

    print(f"\nМапимо URL:\n  {target[:95]}")
    m = call("articleMapper/getArticleUri", {"articleUrl": target, "apiKey": key})
    if "_http" in m or "_err" in m:
        print("  помилка:", m)
        return
    uri = m.get(target) or next(iter(m.values()), None)
    print(f"  → articleUri: {uri}")
    if not uri:
        print("  Статті немає в їхньому індексі.")
        usage(key, "\nКвота ПІСЛЯ")
        return

    print("\nПросимо ПОВНЕ тіло (articleBodyLen=-1):")
    a = call("article/getArticle", {
        "articleUri": uri, "apiKey": key,
        "resultType": "info", "articleBodyLen": "-1",
        "includeArticleTitle": "true", "includeArticleBody": "true",
        "includeArticleSourceInfo": "true",
    })
    if "_http" in a or "_err" in a:
        print("  помилка:", a)
        usage(key, "\nКвота ПІСЛЯ")
        return

    info = (a.get(uri) or {}).get("info") or {}
    body = info.get("body") or ""
    print(f"  джерело: {(info.get('source') or {}).get('title')}")
    print(f"  заголовок: {str(info.get('title'))[:80]}")
    print(f"  ТІЛО: {len(body):,} знаків")
    print(f"  перші 160: {body[:160]!r}")

    usage(key, "\nКвота ПІСЛЯ")
    print(f"\nВитрачено викликів: {_calls}")


if __name__ == "__main__":
    main()
