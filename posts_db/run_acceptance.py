"""Прогін приймального тесту через API провайдера.

Бере 24 URL із provider_acceptance.csv, питає в провайдера кожен,
складає results.json для score_acceptance.py.

Ключі беруться з .env у корені проєкту:
    PERIGON_API_KEY=...
    NEWSCATCHER_API_KEY=...

Запуск:
    python posts_db/run_acceptance.py perigon
    python posts_db/run_acceptance.py newscatcher
    python posts_db/score_acceptance.py data/processed/acceptance_perigon.json perigon
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib import error, parse, request

import pandas as pd

ROOT = Path(__file__).parent.parent
CASES = Path(__file__).parent / "provider_acceptance.csv"
OUT = ROOT / "data" / "processed"
ENV = ROOT / ".env"

DELAY = 1.0          # запас під rate limit безкоштовних тарифів
TIMEOUT = 45


def load_key(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"Немає {name}. Додай у {ENV} рядок {name}=<ключ>")


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}


def get(url: str, headers: dict | None = None) -> dict:
    req = request.Request(url, headers={**UA, **(headers or {})})
    try:
        with request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8")[:300]}
    except Exception as e:                                    # noqa: BLE001
        return {"_error": str(e)[:200]}


def post(url: str, headers: dict, body: dict) -> dict:
    req = request.Request(url, data=json.dumps(body).encode("utf-8"),
                          headers={**UA, **headers,
                                   "Content-Type": "application/json"},
                          method="POST")
    try:
        with request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8")[:300]}
    except Exception as e:                                    # noqa: BLE001
        return {"_error": str(e)[:200]}


def pick_body(art: dict) -> str:
    """Провайдери звуть тіло статті по-різному — беремо найдовше з кандидатів."""
    keys = ("content", "body", "full_text", "articleBody", "text",
            "description", "summary", "excerpt")
    vals = [art.get(k) for k in keys if isinstance(art.get(k), str)]
    return max(vals, key=len) if vals else ""


# ─────────────────────────────────────────────────────── адаптери провайдерів

def probe_perigon(url: str, key: str, domain: str = "", date: str = "") -> dict:
    """Perigon: прямого фільтра за URL немає.

    Перевірено на практиці: параметри `url`, `link`, `articleUrl` не працюють —
    `url` дає 400, решта МОВЧКИ ігнорується і повертає невідфільтровану видачу.
    Тому звужуємо доменом і датою, а потрібний URL шукаємо вже у відповіді.
    """
    params = {"apiKey": key, "source": domain, "size": "100",
              "from": date, "to": date, "showReprints": "false"}
    data = get(f"https://api.perigon.io/v1/articles/all?{parse.urlencode(params)}")
    if data.get("_http_error") or data.get("_error"):
        return {"found": False, "body": "", "raw": data}
    want = url.rstrip("/").lower()
    for a in data.get("articles") or []:
        if (a.get("url") or "").rstrip("/").lower() == want:
            return {"found": True, "body": pick_body(a),
                    "word_count": a.get("enContentWordCount")}
    return {"found": False, "body": "", "seen": len(data.get("articles") or [])}


def probe_newscatcher(url: str, key: str, domain: str = "", date: str = "") -> dict:
    """NewsCatcher v3: пошук за посиланням. Потрібен саме x-api-token."""
    data = post("https://v3-api.newscatcherapi.com/api/search_by_link",
                {"x-api-token": key}, {"links": [url]})
    if data.get("_http_error") or data.get("_error"):
        return {"found": False, "body": "", "raw": data}
    arts = data.get("articles") or []
    if not arts:
        return {"found": False, "body": ""}
    return {"found": True, "body": pick_body(arts[0])}


PROVIDERS = {
    "perigon": ("PERIGON_API_KEY", probe_perigon),
    "newscatcher": ("NEWSCATCHER_API_KEY", probe_newscatcher),
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in PROVIDERS:
        raise SystemExit(f"Вкажи провайдера: {' | '.join(PROVIDERS)}")
    name = sys.argv[1]
    env_name, probe = PROVIDERS[name]
    key = load_key(env_name)

    cases = pd.read_csv(CASES)
    results: dict[str, dict] = {}
    errors = 0

    print(f"{name}: {len(cases)} URL\n")
    for i, row in cases.iterrows():
        r = probe(row["url"], key, row["domain"], str(row["created"]))
        results[row["url"]] = r
        if "raw" in r:
            errors += 1
            if errors <= 3:                     # перші помилки показуємо повністю
                print(f"  [!] {row['domain']}: {r['raw']}")
        status = ("повний" if len(r.get("body") or "") > 1200
                  else "короткий" if r.get("found") else "немає")
        print(f"  {i + 1:>2}/{len(cases)} {row['domain']:<22} {status:<9} "
              f"{len(r.get('body') or ''):>6} знаків")
        time.sleep(DELAY)

    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"acceptance_{name}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nЗаписано → {out}")
    if errors:
        print(f"Помилок API: {errors} — якщо всі, звір ендпоінт із документацією")
    print(f"Далі:  python posts_db/score_acceptance.py {out} {name}")


if __name__ == "__main__":
    main()
