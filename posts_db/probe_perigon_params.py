"""Розвідка: як у Perigon шукати статтю за точним URL.

Документація описує topic/source, але не називає прямого фільтра за URL.
Перевіряємо кілька кандидатів на одній статті, щоб не витрачати
безкоштовний ліміт (150/міс) на 24 запити з хибним параметром.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).parent))
from run_acceptance import load_key  # noqa: E402

BASE = "https://api.perigon.io/v1/articles/all"
TEST_URL = "https://www.theguardian.com/world/2026/sep/11/ukraine-war-briefing-nato-foils-russian-undersea-cable-sabotage-drill"

CANDIDATES = [
    {"url": TEST_URL},
    {"link": TEST_URL},
    {"articleUrl": TEST_URL},
    # запасний варіант: звузити доменом і датою, далі шукати URL локально
    {"source": "theguardian.com", "from": "2026-09-11", "to": "2026-09-12", "size": "10"},
]


def call(params: dict, key: str) -> dict:
    q = parse.urlencode({**params, "apiKey": key})
    req = request.Request(f"{BASE}?{q}")
    try:
        with request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8"))
    except error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode("utf-8")[:250]}
    except Exception as e:                                   # noqa: BLE001
        return {"_err": str(e)[:200]}


def main() -> None:
    key = load_key("PERIGON_API_KEY")
    for params in CANDIDATES:
        label = ", ".join(f"{k}={str(v)[:45]}" for k, v in params.items())
        d = call(params, key)
        if "_http" in d or "_err" in d:
            print(f"[{label}]\n   ПОМИЛКА {d.get('_http', '')} {d.get('_body', d.get('_err', ''))}\n")
            continue
        arts = d.get("articles") or d.get("results") or []
        print(f"[{label}]\n   numResults={d.get('numResults')} статей={len(arts)}")
        if arts:
            a = arts[0]
            body_keys = [k for k in ("content", "body", "fullText", "articleBody",
                                     "summary", "description") if a.get(k)]
            print(f"   поля тіла: {body_keys}")
            for k in body_keys:
                print(f"      {k}: {len(str(a[k])):>6} знаків")
            print(f"   url у відповіді: {str(a.get('url'))[:90]}")
            print(f"   збіг із запитом: {a.get('url', '').rstrip('/') == TEST_URL.rstrip('/')}")
            print(f"   усі ключі: {sorted(a.keys())[:18]}")
        print()


if __name__ == "__main__":
    main()
