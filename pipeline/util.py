"""Нормалізація адрес і HTTP без зовнішніх залежностей."""

from __future__ import annotations

import re
import time
from urllib import error, request

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,uk;q=0.8",
}

TRACKING = re.compile(
    r"[?&](utm_[^=&]+|fbclid|gclid|srnd|mod|ref|ref_src|ref_url|s|t|st|"
    r"mibextid|smid|smtyp|partner|ito|CMP|reflink|at_[^=&]+)=[^&]*", re.I)


def canonical(url: str) -> str | None:
    """Ключ статті: без трекерів, без www., без хвостового слеша."""
    if not isinstance(url, str) or not url.strip():
        return None
    u = TRACKING.sub("", url.strip().split("#")[0])
    u = re.sub(r"[?&]+$", "", u)
    u = re.sub(r"^https?://(?:www\.|m\.|amp\.)?", "https://", u, flags=re.I)
    return u.rstrip("/")


def looks_like_article(url: str) -> bool:
    """Головна сторінка чи розділ — не стаття. Стаття має щонайменше два сегменти
    шляху або довгий числовий id. Без цього фільтра головні сторінки потрапляли
    в чергу, і NewsCatcher валив усю пачку з 422."""
    path = re.sub(r"^https?://[^/]+", "", url or "").split("?")[0].strip("/")
    segs = [p for p in path.split("/") if p]
    return (len(segs) >= 2 or bool(re.search(r"\d{5,}", path))
            or (len(segs) == 1 and len(segs[0]) >= 25 and "-" in segs[0]))


def domain_of(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def match_source(domain: str, sources: dict) -> dict | None:
    """edition.cnn.com → cnn.com: шукаємо найдовший суфікс, що є в реєстрі."""
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        s = sources.get(".".join(parts[i:]))
        if s:
            return s
    return None


def http_get(url: str, headers: dict | None = None, timeout: int = 30):
    """(статус, байти). Помилку повертає статусом, а не винятком."""
    try:
        req = request.Request(url, headers=headers or BROWSER_HEADERS)
        with request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except error.HTTPError as e:
        return e.code, b""
    except Exception as e:                                       # noqa: BLE001
        return type(e).__name__, b""


def polite_sleep(seconds: float = 1.0) -> None:
    time.sleep(seconds)
