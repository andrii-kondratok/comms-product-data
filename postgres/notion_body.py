"""Рендер тіла сторінки Notion у Markdown-подібний текст.

`export_article_bodies.block_text` бере лише plain_text — цього досить для статей,
але для Content Pulse ні: там у тексті живуть посилання на джерела, а href
у plain_text не потрапляє. Тут рендеримо посилання як [текст](url), заголовки
як #, роздільники як ---, і рекурсивно спускаємось у дітей.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from export_article_bodies import RATE_DELAY, children  # noqa: E402

HEADING = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}
LIST = {"bulleted_list_item": "- ", "numbered_list_item": "1. ", "to_do": "- "}


def rich_text_to_md(rich: list[dict]) -> str:
    out = []
    for x in rich:
        txt = x.get("plain_text", "")
        href = x.get("href")
        if href and href != txt:
            out.append(f"[{txt}]({href})")
        elif href:
            out.append(txt)
        else:
            out.append(txt)
    return "".join(out)


def render(blocks: list[dict], token: str, depth: int = 0) -> str:
    """Блоки → текст. Спускаємось у дітей, бо пости лежать у тоглах і колонках."""
    lines: list[str] = []
    for b in blocks:
        t = b.get("type")
        body = (b.get(t) or {}) if isinstance(b.get(t), dict) else {}
        text = rich_text_to_md(body.get("rich_text") or [])

        if t == "divider":
            lines.append("---")
        elif t in HEADING:
            lines.append(f"{HEADING[t]} {text}")
        elif t in LIST:
            lines.append(f"{LIST[t]}{text}")
        elif t == "toggle":
            lines.append(f"### {text}" if text else "")
        elif t == "code":
            lines.append(text)
        elif text:
            lines.append(text)

        # Діти: пости часто лежать усередині тогла або колонки.
        if b.get("has_children") and depth < 4 and t not in ("child_page", "child_database"):
            time.sleep(RATE_DELAY)
            lines.append(render(children(b["id"], token), token, depth + 1))

    return "\n".join(l for l in lines if l is not None)


def page_markdown(page_id: str, token: str) -> str:
    return render(children(page_id, token), token)
